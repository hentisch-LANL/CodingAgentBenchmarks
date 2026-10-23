"""Parallel, resumable execution of many benchmark attempts via an agent pool.

A batch of benchmarks is the cross-product of matched task refs and the number
of attempts requested per task: each ``(task_ref, attempt)`` pair is one *job*.
Jobs are handed to an :mod:`automaton.pool` pool, which dispatches them (serially
in-process, or across MPI ranks) and gathers results.

Resumability is intrinsic to the attempt formalism: a batch targets exactly N
attempts per task. An attempt whose result is *conclusive* (``PASS``/``FAIL``)
is complete and kept; an attempt that is missing or ended inconclusively
(endpoint/harness/internal error, or never ran) is deleted and re-run — but the
batch never exceeds N attempts per task. There is no separate "redo" concept.

Result artifacts are written by :class:`abc_bench.runner.BenchmarkRunner` into
the organized results archive (a per-attempt ``attempt_NN`` subdirectory when
N > 1), *not* into the pool's checkpoint directories. The pool's checkpoint root
holds only its aggregated ``manifest.jsonl``; completion is judged from
abc_bench's own ``metadata.json`` (via ``attempt_is_complete``), so
``write_marker`` is disabled and custom ``done_check``/``clean_job`` hooks drive
resume.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from automaton.pool import AgentJob, JobResult, SerialAgentPool
from automaton.pool.base import AgentPool
from automaton import Agent

from .config import Config
from .container import ContainerManager
from .groups import GroupResolver
from .runner import (
    BenchmarkRunner,
    _agent_identity_hash,
    _attempt_dir_name,
    _safe_path_segment,
    attempt_is_complete,
)
from .task import Task


@dataclass(frozen=True)
class AttemptJob:
    """Payload describing one ``(task_ref, attempt)`` benchmark attempt.

    Attributes
    ----------
    task_ref : str
        Canonical ``group/task`` reference.
    attempt : int
        1-based attempt index for this task.
    attempts : int
        Total attempts requested per task in this batch.
    """

    task_ref: str
    attempt: int
    attempts: int


def build_attempt_jobs(task_refs: list[str], attempts: int) -> list[AttemptJob]:
    """Expand task refs into the cross-product of ``(task_ref, attempt)`` jobs.

    Jobs are ordered task-major then attempt-major, but the pool may dispatch
    them in any order.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    jobs: list[AttemptJob] = []
    for task_ref in task_refs:
        for attempt in range(1, attempts + 1):
            jobs.append(AttemptJob(task_ref=task_ref, attempt=attempt, attempts=attempts))
    return jobs


class BenchmarkPoolRunner:
    """Run a batch of benchmark attempts over an agent pool.

    Parameters
    ----------
    config, resolver
        As for :class:`~abc_bench.runner.BenchmarkRunner`.
    agent_id : str
        Agent to run every job with.
    results_dir : Path | None
        Organized results archive root (or from config/env). Per-attempt
        directories are created beneath it.
    timeout : int | None
        Per-benchmark timeout forwarded to each run.
    runner_kwargs : dict
        Extra keyword args forwarded to each :class:`BenchmarkRunner`
        (``dry_run``, ``stream_output``, ``clean_env``, memory limits,
        ``sandbox_network``).
    """

    def __init__(
        self,
        config: Config,
        resolver: GroupResolver,
        *,
        agent_id: str,
        results_dir: Path | None,
        timeout: int | None,
        runner_kwargs: dict[str, Any] | None = None,
    ):
        self.config = config
        self.resolver = resolver
        self.agent_id = agent_id
        self.results_dir = results_dir
        self.timeout = timeout
        self.runner_kwargs = dict(runner_kwargs or {})

    # -- attempt directory resolution ----------------------------------------

    def _archive_root(self) -> Path:
        import os

        results_dir = self.results_dir
        if results_dir is None:
            env_results_dir = os.environ.get("ABC_BENCH_RESULTS_DIR")
            results_dir = Path(env_results_dir) if env_results_dir else None
        configured = Path(results_dir) if results_dir is not None else self.config.results_dir
        return configured.resolve() if configured.is_absolute() else (Path.cwd() / configured).resolve()

    def _attempt_dir(self, job: AttemptJob, agent_archive_dir: str) -> Path:
        """Return the per-attempt directory for ``job`` in the organized archive.

        This mirrors :meth:`BenchmarkRunner._result_dir` up to (but not
        including) the timestamped run directory, so completion can be judged and
        stale attempts cleaned before a run creates the timestamped dir inside.
        """
        group_id, task_id = job.task_ref.split("/", 1)
        base = (
            self._archive_root()
            / agent_archive_dir
            / _safe_path_segment(group_id)
            / _safe_path_segment(task_id)
        )
        if job.attempts <= 1:
            return base
        return base / _attempt_dir_name(job.attempt)

    # -- pool hooks -----------------------------------------------------------

    def _agent_archive_dir(self) -> str:
        agent = Agent(self.agent_id, self.config)
        models = [model.config for model in agent.models]
        return f"{_safe_path_segment(self.agent_id)}_{_agent_identity_hash(agent.harness_id, models)}"

    def _is_attempt_complete(self, attempt_dir: Path) -> bool:
        """A done attempt has at least one timestamped run that is conclusive."""
        if not attempt_dir.is_dir():
            return False
        for run_dir in attempt_dir.iterdir():
            if run_dir.is_dir() and attempt_is_complete(run_dir):
                return True
        return False

    def make_pool(
        self,
        jobs: list[AttemptJob],
        checkpoint_root: Path,
        *,
        use_mpi: bool,
    ) -> AgentPool:
        """Construct the pool that will execute ``jobs``.

        The pool judges completion from abc_bench's own per-attempt results (not
        a ``DONE.json`` marker) and cleans an incomplete attempt directory before
        re-running it.
        """
        agent_archive_dir = self._agent_archive_dir()

        def done_check(job: AgentJob) -> bool:
            payload: AttemptJob = job.payload
            return self._is_attempt_complete(self._attempt_dir(payload, agent_archive_dir))

        def clean_job(job: AgentJob) -> None:
            payload: AttemptJob = job.payload
            attempt_dir = self._attempt_dir(payload, agent_archive_dir)
            # For a multi-attempt layout the attempt has its own directory we can
            # wipe wholesale. For a single attempt the "attempt dir" is the shared
            # task directory, so only remove inconclusive timestamped runs.
            if payload.attempts > 1:
                if attempt_dir.exists():
                    shutil.rmtree(attempt_dir)
            elif attempt_dir.is_dir():
                for run_dir in list(attempt_dir.iterdir()):
                    if run_dir.is_dir() and not attempt_is_complete(run_dir):
                        shutil.rmtree(run_dir)

        def runner(job: AgentJob) -> JobResult:
            payload: AttemptJob = job.payload
            bench_runner = BenchmarkRunner(self.config, self.resolver, **self.runner_kwargs)
            result = bench_runner.run(
                task_id=payload.task_ref,
                agent_id=self.agent_id,
                timeout=self.timeout,
                results_dir=self.results_dir,
                attempt=payload.attempt,
                attempts=payload.attempts,
            )
            totals = (result.usage_summary or {}).get("totals", {}) if result.usage_summary else {}
            summary = {
                "task_ref": payload.task_ref,
                "attempt": payload.attempt,
                "status": result.status,
                "result_dir": str(result.result_dir),
                "total_tokens": totals.get("total_tokens"),
                "tokens_per_second": totals.get("tokens_per_second"),
                "total_cost": totals.get("total_cost"),
            }
            # "success" for the pool means the attempt reached a conclusive
            # verdict; inconclusive attempts stay pending for a resume.
            from .runner import CONCLUSIVE_STATUSES

            return JobResult(
                job_id=job.job_id,
                success=result.status in CONCLUSIVE_STATUSES,
                output_dir=str(result.result_dir),
                summary=summary,
            )

        pool_cls: type[AgentPool]
        if use_mpi:
            from automaton.pool import MPIAgentPool

            pool_cls = MPIAgentPool
        else:
            pool_cls = SerialAgentPool

        return pool_cls(
            jobs,
            runner,
            checkpoint_root=checkpoint_root,
            done_check=done_check,
            clean_job=clean_job,
            write_marker=False,
        )


def build_required_containers(
    config: Config,
    resolver: GroupResolver,
    agent_id: str,
    task_refs: list[str],
) -> None:
    """Build every runtime container required by ``task_refs`` for ``agent_id``.

    Called on every rank before an MPI pool dispatches, so each node has its
    (node-local) container image ready. Serial runs call it once. Building is not
    parallel-safe within a node, so callers must not invoke this concurrently on
    the same host.
    """
    agent = Agent(agent_id, config)
    manager = ContainerManager(config.package_root, resolver)
    group_ids = sorted({task_ref.split("/", 1)[0] for task_ref in task_refs})
    for group_id in group_ids:
        runtime_ref = manager.runtime_container_ref(agent.harness_id, group_id)
        if not manager.container_exists(runtime_ref):
            manager.build_container(runtime_ref)
