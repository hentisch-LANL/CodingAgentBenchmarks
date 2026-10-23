"""Abstract agent pool: coordinate embarrassingly-parallel agent jobs.

An :class:`AgentPool` runs a set of jobs and gathers their outputs into a single
checkpoint root. It is deliberately *content-free* about what a job does: the
caller supplies a ``runner`` callable that, given an :class:`AgentJob`, performs
the work (typically building a :class:`~automaton.sandbox.Sandbox` run and
staging assets) and writes all artifacts into the job's ``output_dir``.

The base class owns:

- **Canonicalization** of jobs at ingest (freezing insertion order into stable
  job ids), so identity is fixed the moment jobs are handed to the pool.
- **Checkpoint-aware selection** of which jobs still need to run.
- **Safe execution** of a single job, including writing the definitive done
  marker on success only.

Scheduling across ranks/processes is left to concrete subclasses (see
:class:`~automaton.pool.mpi_pool.MPIAgentPool`).
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Callable, Iterable

from . import checkpoint
from .job import AgentJob, JobResult

#: Signature of a job runner. Given a job, do the work and optionally return a
#: :class:`JobResult`. If it returns ``None``, a success result is synthesized.
#: Raising is treated as failure.
Runner = Callable[[AgentJob], JobResult | None]

#: Signature of a completion predicate. Given a job, return True iff it is
#: already definitively done and should be skipped on resume. Callers supply
#: this to override the default :data:`DONE.json`-marker check with their own
#: completion semantics (e.g. inspecting a result file they wrote themselves).
DoneCheck = Callable[[AgentJob], bool]

#: Signature of a job cleaner. Given a job that is *not* done (and therefore
#: about to run), prepare a clean slate for it (e.g. remove a stale partial
#: output directory). Callers supply this alongside :data:`DoneCheck` to match
#: their own on-disk layout.
CleanJob = Callable[[AgentJob], None]


def _canonical_job_id(index: int, total: int) -> str:
    """Return a stable, zero-padded, filesystem-safe id for ``index``."""
    width = max(4, len(str(max(total - 1, 0))))
    return f"{index:0{width}d}"


class AgentPool(abc.ABC):
    """Abstract coordinator for a batch of parallel agent jobs.

    Parameters
    ----------
    jobs : Iterable[Any]
        Opaque, caller-defined job payloads. They are canonicalized into
        :class:`AgentJob` instances immediately, in the given order; identity
        (index/job_id) is frozen at this point.
    runner : Runner
        Callable that executes a single job and writes its artifacts into the
        job's ``output_dir``. May return a :class:`JobResult` or ``None``.
    checkpoint_root : Path
        Root directory holding per-job subdirectories and the manifest.
    resume : bool, optional
        If True (default), jobs with a valid done marker are skipped; any
        unfinished job directory is wiped and re-run. If False, all job
        directories are wiped and every job re-run.
    done_check : DoneCheck, optional
        Predicate deciding whether a job is already done (and skippable on
        resume). Defaults to the built-in :data:`DONE.json`-marker check
        (:func:`checkpoint.is_done`). Supply this to drive completion from your
        own on-disk records instead.
    clean_job : CleanJob, optional
        Callable that prepares a clean slate for a job that is about to run.
        Defaults to :func:`checkpoint.ensure_clean_job_dir` (wipe + recreate the
        job's ``output_dir``). Supply this to match a custom layout.
    write_marker : bool, optional
        If True (default), a :data:`DONE.json` marker is written on success by
        :meth:`execute_job`. Set False when the caller's own ``done_check``
        records completion elsewhere and the built-in marker is unwanted.
    """

    def __init__(
        self,
        jobs: Iterable[Any],
        runner: Runner,
        *,
        checkpoint_root: Path,
        resume: bool = True,
        done_check: DoneCheck | None = None,
        clean_job: CleanJob | None = None,
        write_marker: bool = True,
    ):
        self.runner = runner
        self.checkpoint_root = Path(checkpoint_root)
        self.resume = resume
        self._done_check = done_check
        self._clean_job = clean_job
        self._write_marker = write_marker

        payloads = list(jobs)
        total = len(payloads)
        self._jobs: list[AgentJob] = [
            AgentJob(
                index=index,
                job_id=_canonical_job_id(index, total),
                payload=payload,
                output_dir=checkpoint.job_dir(
                    self.checkpoint_root, _canonical_job_id(index, total)
                ),
            )
            for index, payload in enumerate(payloads)
        ]

    # -- introspection --------------------------------------------------------

    @property
    def jobs(self) -> list[AgentJob]:
        """The canonical, frozen list of jobs (insertion order)."""
        return list(self._jobs)

    def __len__(self) -> int:
        return len(self._jobs)

    # -- completion / cleanup hooks -------------------------------------------

    def _is_done(self, job: AgentJob) -> bool:
        """Return True iff ``job`` is already done, via the configured check."""
        if self._done_check is not None:
            return self._done_check(job)
        return checkpoint.is_done(job.output_dir)

    def _clean(self, job: AgentJob) -> None:
        """Prepare a clean slate for ``job`` via the configured cleaner."""
        if self._clean_job is not None:
            self._clean_job(job)
        else:
            checkpoint.ensure_clean_job_dir(job.output_dir)

    # -- checkpoint-aware selection ------------------------------------------

    def pending_jobs(self) -> list[AgentJob]:
        """Return jobs still needing execution, honoring the checkpoint store.

        With ``resume=True``, jobs judged done (by :meth:`_is_done`) are skipped
        and any unfinished remnant is cleaned. With ``resume=False``, every job
        is cleaned and returned. Never resume an unfinished agent — clean and
        start fresh.
        """
        pending: list[AgentJob] = []
        for job in self._jobs:
            if self.resume and self._is_done(job):
                continue
            self._clean(job)
            pending.append(job)
        return pending

    def completed_results(self) -> list[JobResult]:
        """Load recorded results for all already-done jobs.

        When the built-in marker is used, the recorded :class:`JobResult` is
        loaded from it. When a custom ``done_check`` reports done but no marker
        is present (e.g. ``write_marker=False``), a minimal success result is
        synthesized so the returned batch still covers every done job.
        """
        results: list[JobResult] = []
        for job in self._jobs:
            if not self._is_done(job):
                continue
            recorded = checkpoint.load_result(job.output_dir)
            if recorded is None:
                recorded = JobResult(
                    job_id=job.job_id,
                    success=True,
                    output_dir=str(job.output_dir),
                )
            results.append(recorded)
        return results

    # -- single-job execution -------------------------------------------------

    def execute_job(self, job: AgentJob) -> JobResult:
        """Run one job via the runner, writing a done marker on success only.

        A runner exception is caught and reported as a failed
        :class:`JobResult`; no done marker is written, so the job is retried on
        a subsequent resume.
        """
        job.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = self.runner(job)
        except Exception as exc:  # noqa: BLE001 - report any runner failure
            return JobResult(
                job_id=job.job_id,
                success=False,
                output_dir=str(job.output_dir),
                error=f"{type(exc).__name__}: {exc}",
            )

        if result is None:
            result = JobResult(
                job_id=job.job_id,
                success=True,
                output_dir=str(job.output_dir),
            )
        elif result.job_id != job.job_id:
            # Normalize identity/paths so the runner can't misroute a result.
            result = JobResult(
                job_id=job.job_id,
                success=result.success,
                output_dir=result.output_dir or str(job.output_dir),
                returncode=result.returncode,
                summary=result.summary,
                error=result.error,
            )

        if result.success and self._write_marker:
            checkpoint.write_done(job.output_dir, result)
        return result

    # -- scheduling (subclass responsibility) ---------------------------------

    @abc.abstractmethod
    def run(self) -> list[JobResult]:
        """Execute all pending jobs and return results for the full batch.

        Implementations must include results for jobs already completed in a
        prior run (via :meth:`completed_results`) so the return value covers the
        entire job set, not just the ones run this invocation.
        """
        raise NotImplementedError
