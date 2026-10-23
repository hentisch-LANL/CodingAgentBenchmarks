"""Benchmark orchestration for ABC Bench.

The heavy lifting of sandboxed execution lives in :mod:`automaton`. This module
orchestrates a benchmark run on top of it:

1. resolve the task, agent, harness, and runtime container,
2. stage the workspace, prompt, and harness assets into a per-run directory,
3. run the task's ``build_env.sh`` inside the container,
4. run the agent via :meth:`automaton.Sandbox.run_agent` (deliverable is left
   in the workspace),
5. run the task's hidden verification inside the container,
6. compute PASS/FAIL and write ``result.json``, ``metadata.json``, and
   ``usage.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automaton import Agent, BindMount, Harness, Sandbox

from .config import Config
from .container import ContainerManager
from .groups import GroupResolver
from .task import Task


CONTAINER_RUN_ROOT = Path("/abc-bench/run")
CONTAINER_WORKSPACE_ROOT = Path("/workspace")
CONTAINER_BASE_ROOT = Path("/abc-bench/base")

#: Benchmark statuses that represent a *conclusive* verdict on the task: the
#: agent either solved it (``PASS``) or demonstrably did not (``FAIL``, which
#: includes running out of context). Any other status is *inconclusive* — an
#: infrastructure/endpoint/harness problem or a dry run — and such an attempt is
#: not counted as complete: on a resume it is deleted and re-run from scratch.
CONCLUSIVE_STATUSES = frozenset({"PASS", "FAIL"})


def attempt_is_complete(attempt_dir: Path) -> bool:
    """Return True iff ``attempt_dir`` holds a conclusive, finished benchmark.

    A complete attempt has a readable ``metadata.json`` whose ``complete`` flag
    is true (equivalently, a conclusive ``status``). Missing, unreadable, or
    inconclusive results are treated as incomplete so a resume re-runs them.
    """
    metadata_path = attempt_dir / "metadata.json"
    try:
        with open(metadata_path) as handle:
            metadata = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(metadata, dict):
        return False
    if "complete" in metadata:
        return bool(metadata["complete"])
    # Backward compatibility with results written before the ``complete`` flag.
    return str(metadata.get("status", "")) in CONCLUSIVE_STATUSES


def _format_elapsed_seconds(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _safe_path_segment(text: str) -> str:
    """Convert a string to a filesystem-safe path segment.

    Examples
    --------
    >>> _safe_path_segment("model/name")
    'model-name'
    >>> _safe_path_segment("  spaces  ")
    'spaces'
    """
    safe = re.sub(r'[^\w\-.]', '-', text)
    safe = safe.strip('-.')
    safe = re.sub(r'-+', '-', safe)
    return safe if safe else 'unnamed'


def _date_label(dt: datetime) -> str:
    """Format datetime as a compact date label for filenames.

    Examples
    --------
    >>> from datetime import datetime
    >>> _date_label(datetime(2026, 6, 15, 12, 0, 0))
    '20260615T120000'
    """
    return dt.strftime('%Y%m%dT%H%M%S')


#: Prefix for per-attempt result subdirectories under a task directory.
_ATTEMPT_DIR_PREFIX = "attempt_"


def _attempt_dir_name(attempt: int) -> str:
    """Return the zero-padded per-attempt subdirectory name (1-based).

    Examples
    --------
    >>> _attempt_dir_name(1)
    'attempt_01'
    >>> _attempt_dir_name(12)
    'attempt_12'
    """
    return f"{_ATTEMPT_DIR_PREFIX}{attempt:02d}"


def _is_attempt_dir(name: str) -> bool:
    """Return True iff ``name`` looks like a per-attempt subdirectory."""
    if not name.startswith(_ATTEMPT_DIR_PREFIX):
        return False
    return name[len(_ATTEMPT_DIR_PREFIX):].isdigit()


def _model_identity_hash(model_config: dict[str, Any]) -> str:
    """Generate a short hash of model configuration for deduplication."""
    identity_str = f"{model_config.get('model', '')}@{model_config.get('base_url', '')}"
    hash_obj = hashlib.sha256(identity_str.encode())
    return hash_obj.hexdigest()[:8]


def _agent_identity_hash(harness_id: str, models: list[dict[str, Any]]) -> str:
    identity_str = "|".join(
        [harness_id]
        + [f"{model.get('model', '')}@{model.get('base_url', '')}" for model in models]
    )
    hash_obj = hashlib.sha256(identity_str.encode())
    return hash_obj.hexdigest()[:8]


class BenchmarkResult:
    """Result of a benchmark run."""

    def __init__(
        self,
        task_id: str,
        task_group: str,
        task_ref: str,
        agent_id: str,
        harness_id: str,
        model_names: list[str],
        status: str,
        result_dir: Path,
        verify_log: str | None = None,
        error: str | None = None,
        failure_phase: str | None = None,
        usage_summary: dict[str, Any] | None = None,
        model_apis: list[str | None] | None = None,
    ):
        self.task_id = task_id
        self.task_group = task_group
        self.task_ref = task_ref
        self.agent_id = agent_id
        self.harness_id = harness_id
        self.model_names = model_names
        self.status = status
        self.result_dir = result_dir
        self.verify_log = verify_log
        self.error = error
        self.failure_phase = failure_phase
        self.usage_summary = usage_summary or {}
        self.model_apis = list(model_apis) if model_apis is not None else []
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "task_group": self.task_group,
            "task_ref": self.task_ref,
            "agent": self.agent_id,
            "harness": self.harness_id,
            "models": self.model_names,
            "apis": self.model_apis,
            "status": self.status,
            "result_dir": str(self.result_dir),
            "verify_log": self.verify_log,
            "error": self.error,
            "failure_phase": self.failure_phase,
            "usage": self.usage_summary,
            "total_cost": float(self.usage_summary.get("totals", {}).get("total_cost", 0.0) or 0.0),
            "timestamp": self.timestamp,
        }

    def save(self, path: Path) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


def _print_run_summary(result: BenchmarkResult, started_at: datetime, ended_at: datetime) -> None:
    elapsed_seconds = (ended_at - started_at).total_seconds()
    totals = result.usage_summary.get("totals", {}) if result.usage_summary else {}
    total_cost = float(totals.get("total_cost", 0.0) or 0.0)
    total_tokens = int(totals.get("total_tokens", 0) or 0)
    tokens_per_second = float(totals.get("tokens_per_second", 0.0) or 0.0)
    print(f"\nFinal status: {result.status}")
    print(f"Walltime: {_format_elapsed_seconds(elapsed_seconds)}")
    if result.model_apis:
        api_label = ", ".join(
            f"{name}={api}" for name, api in zip(result.model_names, result.model_apis, strict=False)
        )
        print(f"APIs: {api_label}")
    print(f"Total tokens: {total_tokens}")
    print(f"Throughput: {tokens_per_second:.1f} tokens/s")
    print(f"Total cost: USD {total_cost:.6f}")


class BenchmarkRunner:
    """Orchestrates benchmark execution on top of :class:`automaton.Sandbox`.

    Parameters
    ----------
    config : Config
        Configuration object.
    resolver : GroupResolver
        Task-group resolver (CLI ``--task-dir`` > ``TASK_PATH`` > installed).
    dry_run : bool, optional
        If True, print commands without executing.
    stream_output : bool, optional
        If True, mirror container output to terminal.
    clean_env : bool, optional
        If True, remove task environment after verification.
    harness_memory_limit_bytes : int, optional
        Fixed harness memory cap in bytes.
    harness_memory_limit_fraction : float, optional
        Fraction of available memory to cap harness at. Default 0.9.
    sandbox_network : bool, optional
        Override loopback-only sandbox networking; defaults to the config value.

    Examples
    --------
    >>> config = Config()  # doctest: +SKIP
    >>> runner = BenchmarkRunner(config, config.group_resolver(), dry_run=True)  # doctest: +SKIP
    """

    def __init__(
        self,
        config: Config,
        resolver: GroupResolver,
        dry_run: bool = False,
        stream_output: bool = True,
        clean_env: bool = True,
        harness_memory_limit_bytes: int | None = None,
        harness_memory_limit_fraction: float | None = 0.9,
        sandbox_network: bool | None = None,
    ):
        self.config = config
        self.resolver = resolver
        self.package_root = config.package_root
        self.dry_run = dry_run
        self.stream_output = stream_output
        self.clean_env = clean_env
        self.harness_memory_limit_bytes = harness_memory_limit_bytes
        self.harness_memory_limit_fraction = harness_memory_limit_fraction
        self.sandbox_network = config.sandbox_network if sandbox_network is None else sandbox_network

    def _sandbox(self, host_root: Path) -> Sandbox:
        """Construct the Automaton sandbox for this run.

        Container mount roots and the proxy provider id/api-key env are kept at
        their ABC Bench values so existing harness scripts and container
        templates continue to match.
        """
        return Sandbox(
            host_root=host_root,
            dry_run=self.dry_run,
            stream_output=self.stream_output,
            harness_memory_limit_bytes=self.harness_memory_limit_bytes,
            harness_memory_limit_fraction=self.harness_memory_limit_fraction,
            proxy_api_key_env="ABC_BENCH_MODEL_PROXY_API_KEY",
            proxy_api_key_value="abc-bench-proxy",
            proxy_provider_id="abc-bench-proxy",
            allowed_provider_executed_tools=self.config.allowed_provider_executed_tools,
            container_run_root=CONTAINER_RUN_ROOT,
            container_workspace_root=CONTAINER_WORKSPACE_ROOT,
            container_base_root=CONTAINER_BASE_ROOT,
        )

    def run(
        self,
        task_id: str,
        agent_id: str,
        task_group: str | None = None,
        timeout: int | None = None,
        results_dir: Path | None = None,
        output_dir: Path | None = None,
        attempt: int | None = None,
        attempts: int = 1,
    ) -> BenchmarkResult:
        """Run a single benchmark and return its :class:`BenchmarkResult`.

        When ``attempts > 1`` the run is stored under a per-attempt subdirectory
        (``.../<task>/attempt_NN/<timestamp>_<agent>_<task>/``); ``attempt`` is
        the 1-based index of this attempt. A single attempt (the default) keeps
        the flat ``.../<task>/<timestamp>_<agent>_<task>/`` layout unchanged.
        These only apply to the organized ``results_dir`` layout, not to a
        standalone ``output_dir``.
        """
        task = Task(task_id, self.resolver, task_group=task_group)
        agent = Agent(agent_id, self.config)
        harness = Harness(agent.harness_id, self.package_root / "harnesses" / agent.harness_id)
        container_manager = ContainerManager(self.package_root, self.resolver)
        runtime_container_ref = container_manager.runtime_container_ref(agent.harness_id, task.group_id)
        runtime_container = container_manager.get_container_path(runtime_container_ref)
        agent_model_configs = [model.config for model in agent.models]
        agent_model_apis = [config.get("api") for config in agent_model_configs]

        task_errors = task.validate()
        if task_errors:
            raise ValueError("Task validation failed:\n" + "\n".join(task_errors))

        harness_errors = agent.validate()
        if harness_errors:
            raise ValueError("Agent validation failed:\n" + "\n".join(harness_errors))
        harness_errors = harness.validate()
        if harness_errors:
            raise ValueError("Harness validation failed:\n" + "\n".join(harness_errors))

        if not runtime_container.exists():
            raise FileNotFoundError(
                f"Harness runtime not found: {runtime_container}\n"
                f"Build it first with: abc-bench build-container {agent.harness_id}/{task.group_id}"
            )

        started_at = datetime.now(timezone.utc)
        result_dir, output_mode, agent_archive_dir = self._result_dir(
            task_group=task.group_id,
            task_id=task.task_id,
            agent_id=agent.agent_id,
            models=agent_model_configs,
            harness_id=agent.harness_id,
            started_at=started_at,
            results_dir=results_dir,
            output_dir=output_dir,
            attempt=attempt,
            attempts=attempts,
        )
        result_dir.mkdir(parents=True, exist_ok=False)

        print(f"Running benchmark: {task.task_ref} with {agent.agent_id}")
        print(f"Results directory: {result_dir}")
        container_paths = self._container_run_paths()
        bind_mounts = self._container_bind_mounts(result_dir)
        sandbox = self._sandbox(result_dir.parent)

        proxy_usage_summary: dict[str, Any] = {}

        try:
            workspace_dir = result_dir / "workspace"
            print(f"Copying workspace from {task.workspace_dir} to {workspace_dir}")
            shutil.copytree(
                task.workspace_dir,
                workspace_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            shutil.copy2(task.prompt_file, result_dir / "prompt.md")

            # Optional shared task-group prompt layer. When the group defines a
            # group_prompt.md, stage it so the harness can inject it between the
            # system prompt and the per-task prompt; otherwise drop the key so
            # the harness receives no --group-prompt and behaves as before.
            group_prompt_file = task.group_prompt_file
            if group_prompt_file is not None:
                shutil.copy2(group_prompt_file, result_dir / "group_prompt.md")
            else:
                container_paths.pop("group_prompt", None)

            staged_harness_dir = result_dir / "harness"
            shutil.copytree(
                harness.harness_dir,
                staged_harness_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            build_env_script = result_dir / "build_env.sh"
            shutil.copy2(task.build_env_script, build_env_script)
            build_env_script.chmod(0o755)

            sandbox_helper_script = result_dir / "sandbox_helper.py"
            sandbox_helper_script.write_text(sandbox.sandbox_helper_script())
            sandbox_helper_script.chmod(0o755)

            # 1. build_env.sh (non-isolated network, matching legacy behavior)
            print("Running build environment script...")
            try:
                sandbox.run_command(
                    runtime_container,
                    ["bash", str(container_paths["build_env"])],
                    cwd=container_paths["workspace"],
                    log_file=result_dir / "build_env.log",
                    env_vars={
                        "ABC_BENCH_WORKSPACE": str(container_paths["workspace"]),
                        "ABC_BENCH_TASK_ENV": str(container_paths["task_env"]),
                    },
                    bind_mounts=bind_mounts,
                    network=True,
                )
            except Exception as e:
                result = BenchmarkResult(
                    task_id=task.task_id,
                    task_group=task.group_id,
                    task_ref=task.task_ref,
                    agent_id=agent.agent_id,
                    harness_id=agent.harness_id,
                    model_names=agent.model_names,
                    status="HARNESS_ERROR",
                    result_dir=result_dir,
                    error=str(e),
                    failure_phase="build_env",
                    model_apis=agent_model_apis,
                )
                ended_at = datetime.now(timezone.utc)
                self._finalize(result, task, agent, agent_model_configs, result_dir,
                               output_mode, agent_archive_dir, timeout, started_at, ended_at)
                return result

            # 2. Run the agent (deliverable left in workspace)
            print("Running harness...")
            harness_output_dir = result_dir / "harness_output"
            harness_output_dir.mkdir(exist_ok=True)
            models_config_file = result_dir / "models_config.yaml"
            harness_config_file = result_dir / "harness_config.yaml"
            proxy_log = harness_output_dir / "model_proxy.log"
            usage_records_file = harness_output_dir / "usage.jsonl"

            execution = sandbox.run_agent(
                runtime_container,
                harness,
                agent,
                workspace=container_paths["workspace"],
                run_dir=result_dir,
                container_paths=container_paths,
                bind_mounts=bind_mounts,
                proxy_log=proxy_log,
                usage_records_file=usage_records_file,
                harness_output_dir=harness_output_dir,
                models_config_file=models_config_file,
                harness_config_file=harness_config_file,
                extra_env={
                    "ABC_BENCH_WORKSPACE": str(container_paths["workspace"]),
                    "ABC_BENCH_TASK_ENV": str(container_paths["task_env"]),
                },
                timeout=timeout,
                sandboxed=self.sandbox_network,
            )
            proxy_usage_summary = execution.usage_summary

            if execution.dry_run:
                result = BenchmarkResult(
                    task_id=task.task_id, task_group=task.group_id, task_ref=task.task_ref,
                    agent_id=agent.agent_id, harness_id=agent.harness_id,
                    model_names=agent.model_names, status="DRY-RUN",
                    result_dir=result_dir, model_apis=agent_model_apis,
                )
                ended_at = datetime.now(timezone.utc)
                self._finalize(result, task, agent, agent_model_configs, result_dir,
                               output_mode, agent_archive_dir, timeout, started_at, ended_at,
                               usage_summary=proxy_usage_summary)
                return result

            if not execution.success:
                status, failure_phase = self._classify_harness_failure(execution)
                print(f"Harness failed; recording benchmark result as {status}: {execution.failure_detail}")
                result = BenchmarkResult(
                    task_id=task.task_id, task_group=task.group_id, task_ref=task.task_ref,
                    agent_id=agent.agent_id, harness_id=agent.harness_id,
                    model_names=agent.model_names, status=status,
                    result_dir=result_dir, error=execution.failure_detail,
                    failure_phase=failure_phase, model_apis=agent_model_apis,
                )
            elif getattr(execution, "blocked_provider_executed_tools", None):
                # The harness exited cleanly, but the proxy refused a
                # provider-executed tool along the way (an agent can retry
                # without the tool and still exit 0). Do not grade this: a
                # containment failure must not be able to produce a PASS.
                blocked = ", ".join(execution.blocked_provider_executed_tools)
                detail = (
                    f"Model proxy blocked provider-executed tool(s): {blocked}. "
                    "Refusing to grade this run; fix the harness configuration or "
                    "allow the tool explicitly."
                )
                print(f"Harness succeeded but {detail}")
                result = BenchmarkResult(
                    task_id=task.task_id, task_group=task.group_id, task_ref=task.task_ref,
                    agent_id=agent.agent_id, harness_id=agent.harness_id,
                    model_names=agent.model_names, status="HARNESS_ERROR",
                    result_dir=result_dir, error=detail,
                    failure_phase="provider_executed_tool_blocked",
                    model_apis=agent_model_apis,
                )
            else:
                # 3. Stage + run verification against the deliverable
                print("Injecting verification...")
                verification_dir = result_dir / "verification"
                if verification_dir.exists():
                    shutil.rmtree(verification_dir)
                shutil.copytree(
                    task.verification_dir,
                    verification_dir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )

                print("Running verification...")
                verify_log = result_dir / "verify.log"
                verify_result = self._run_verification(
                    sandbox=sandbox,
                    container_path=runtime_container,
                    workspace=container_paths["workspace"],
                    verification_dir=container_paths["verification"],
                    log_file=verify_log,
                    bind_mounts=bind_mounts,
                )

                if self.clean_env:
                    self._clean_task_env(workspace_dir)

                result = BenchmarkResult(
                    task_id=task.task_id, task_group=task.group_id, task_ref=task.task_ref,
                    agent_id=agent.agent_id, harness_id=agent.harness_id,
                    model_names=agent.model_names, status=verify_result,
                    result_dir=result_dir, verify_log="verify.log",
                    failure_phase=None if verify_result == "PASS" else "verification",
                    model_apis=agent_model_apis,
                )

        except Exception as e:
            print(f"Error during benchmark run: {e}")
            result = BenchmarkResult(
                task_id=task.task_id, task_group=task.group_id, task_ref=task.task_ref,
                agent_id=agent.agent_id, harness_id=agent.harness_id,
                model_names=agent.model_names, status="ERROR",
                result_dir=result_dir, error=str(e), model_apis=agent_model_apis,
            )

        ended_at = datetime.now(timezone.utc)
        self._finalize(result, task, agent, agent_model_configs, result_dir,
                       output_mode, agent_archive_dir, timeout, started_at, ended_at,
                       usage_summary=proxy_usage_summary)
        return result

    # -- verification ---------------------------------------------------------

    def _run_verification(
        self,
        sandbox: Sandbox,
        container_path: Path,
        workspace: Path,
        verification_dir: Path,
        log_file: Path,
        bind_mounts: list[BindMount],
    ) -> str:
        """Run verification and return PASS or FAIL."""
        verify_script = verification_dir / "verify_solution.sh"
        env_vars = {
            "ABC_BENCH_WORKSPACE": str(workspace),
            "ABC_BENCH_TASK_ENV": str(workspace / "abc-bench-task-env"),
        }
        returncode = sandbox.run_command(
            container_path,
            ["bash", str(verify_script)],
            env_vars=env_vars,
            bind_mounts=bind_mounts,
            log_file=log_file,
            # Verification runs with the same loopback-only isolation as the
            # agent when sandbox networking is enabled.
            network=not self.sandbox_network,
            check=False,
        )
        if self.dry_run:
            return "DRY-RUN"
        return self._read_verify_result(log_file, returncode)

    @staticmethod
    def _classify_harness_failure(execution) -> tuple[str, str]:
        """Map a failed agent ExecutionResult to a benchmark (status, failure_phase).

        Context-window exhaustion is a legitimate benchmark FAIL (the agent could
        not fit the task within the model's context limits), not a harness/infra
        bug. Endpoint exhaustion (the proxy could not reach the upstream at all)
        stays ENDPOINT_ERROR; everything else is HARNESS_ERROR.

        A blocked provider-executed tool is explicitly NOT a benchmark FAIL: the
        agent tried to use a tool that runs on the model provider's
        infrastructure instead of inside the sandbox. That is a harness
        misconfiguration (or an agent-version change), and grading it as a wrong
        answer would hide a containment problem behind a plausible score.
        """
        if getattr(execution, "blocked_provider_executed_tools", None):
            return "HARNESS_ERROR", "provider_executed_tool_blocked"
        if getattr(execution, "context_length_exceeded", False):
            return "FAIL", "context_length"
        if execution.endpoint_exhausted:
            return "ENDPOINT_ERROR", "harness"
        return "HARNESS_ERROR", "harness"

    def _read_verify_result(self, log_file: Path, returncode: int | None = None) -> str:
        with open(log_file) as f:
            log_content = f.read()
        status_lines = [line.strip() for line in log_content.splitlines() if line.strip() in {"PASS", "FAIL"}]
        final_status = status_lines[-1] if status_lines else "FAIL"
        if final_status == "PASS" and (returncode is None or returncode == 0):
            return "PASS"
        return "FAIL"

    def _clean_task_env(self, workspace: Path) -> None:
        """Remove task environment to save storage."""
        task_env = workspace / "abc-bench-task-env"
        if not task_env.exists():
            if not self.dry_run:
                print("No task environment to clean (task may not use one)")
            return
        if self.dry_run:
            print(f"[DRY-RUN] Would remove task environment: {task_env}")
            return
        try:
            result = subprocess.run(
                ["du", "-sh", str(task_env)],
                capture_output=True, text=True, check=False,
            )
            size_str = result.stdout.split()[0] if result.returncode == 0 else "unknown size"
        except Exception:
            size_str = "unknown size"
        print(f"Cleaning task environment ({size_str})...")
        shutil.rmtree(task_env)
        print("Task environment cleaned successfully")

    # -- result finalization --------------------------------------------------

    def _finalize(
        self,
        result: BenchmarkResult,
        task: Task,
        agent: Agent,
        models: list[dict[str, Any]],
        result_dir: Path,
        output_mode: str,
        agent_archive_dir: str,
        timeout: int | None,
        started_at: datetime,
        ended_at: datetime,
        usage_summary: dict[str, Any] | None = None,
    ) -> None:
        """Attach usage, write result.json/metadata.json/usage.json, print summary."""
        result.usage_summary = usage_summary or {}
        with open(result_dir / "usage.json", "w") as f:
            json.dump(result.usage_summary, f, indent=2, sort_keys=True)
            f.write("\n")
        metadata = self._metadata(
            result=result, task=task, agent=agent, models=models, result_dir=result_dir,
            output_mode=output_mode, agent_archive_dir=agent_archive_dir,
            timeout=timeout, started_at=started_at, ended_at=ended_at,
        )
        result.save(result_dir / "result.json")
        with open(result_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2, sort_keys=True)
            f.write("\n")
        _print_run_summary(result, started_at, ended_at)

    # -- result directory -----------------------------------------------------

    def _result_dir(
        self,
        task_group: str,
        task_id: str,
        agent_id: str,
        models: list[dict[str, Any]],
        harness_id: str,
        started_at: datetime,
        results_dir: Path | None,
        output_dir: Path | None,
        attempt: int | None = None,
        attempts: int = 1,
    ) -> tuple[Path, str, str]:
        """Resolve the single benchmark result directory.

        For the organized layout with ``attempts > 1``, the run is placed under a
        per-attempt subdirectory ``<task>/attempt_NN/<run_name>`` and any
        pre-existing flat single-attempt run under ``<task>/`` is migrated into
        ``attempt_01/`` first. A single attempt keeps the flat layout.
        """
        env_output_dir = os.environ.get("ABC_BENCH_OUTPUT_DIR")
        env_results_dir = os.environ.get("ABC_BENCH_RESULTS_DIR")

        if output_dir is not None and results_dir is not None:
            raise ValueError("output_dir and results_dir are mutually exclusive")

        if output_dir is None and results_dir is None:
            if env_output_dir and env_results_dir:
                raise ValueError("ABC_BENCH_OUTPUT_DIR and ABC_BENCH_RESULTS_DIR are mutually exclusive")
            if env_output_dir:
                output_dir = Path(env_output_dir)
            elif env_results_dir:
                results_dir = Path(env_results_dir)

        agent_archive_dir = self._agent_archive_dir(agent_id, harness_id, models)
        group_dir = _safe_path_segment(task_group)
        task_dir = _safe_path_segment(task_id)

        if output_dir is not None:
            return Path(output_dir).expanduser().resolve(), "standalone", agent_archive_dir

        configured_results_dir = Path(results_dir) if results_dir is not None else self.config.results_dir
        if configured_results_dir.is_absolute():
            archive_root = configured_results_dir.resolve()
        else:
            archive_root = (Path.cwd() / configured_results_dir).resolve()

        run_name = f"{_date_label(started_at)}_{_safe_path_segment(agent_id)}_{task_dir}"
        task_parent = archive_root / agent_archive_dir / group_dir / task_dir

        if attempts <= 1:
            parent = task_parent
        else:
            self._migrate_flat_runs_to_attempt_one(task_parent)
            attempt_index = attempt if attempt is not None else 1
            parent = task_parent / _attempt_dir_name(attempt_index)

        return self._unique_dir(parent / run_name), "organized", agent_archive_dir

    @staticmethod
    def _migrate_flat_runs_to_attempt_one(task_parent: Path) -> None:
        """Move any pre-existing flat run(s) under ``task_parent`` into attempt_01.

        A single-attempt run stored runs directly as ``<task>/<run_name>/``. When
        the task later grows to multiple attempts, those flat runs are relocated
        under ``<task>/attempt_01/`` so the whole task uses the attempt layout,
        preserving each run's metadata verbatim.
        """
        task_parent = task_parent.expanduser()
        if not task_parent.is_dir():
            return
        attempt_one = task_parent / _attempt_dir_name(1)
        flat_runs = [
            child
            for child in task_parent.iterdir()
            if child.is_dir() and not _is_attempt_dir(child.name)
        ]
        if not flat_runs:
            return
        attempt_one.mkdir(parents=True, exist_ok=True)
        for run in flat_runs:
            destination = attempt_one / run.name
            if destination.exists():
                for index in range(2, 1000):
                    candidate = attempt_one / f"{run.name}_{index}"
                    if not candidate.exists():
                        destination = candidate
                        break
            shutil.move(str(run), str(destination))

    def _unique_dir(self, path: Path) -> Path:
        path = path.expanduser().resolve()
        if not path.exists():
            return path
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.name}_{index}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"could not find available result directory near {path}")

    def _agent_archive_dir(self, agent_id: str, harness_id: str, models: list[dict[str, Any]]) -> str:
        return f"{_safe_path_segment(agent_id)}_{_agent_identity_hash(harness_id, models)}"

    # -- metadata -------------------------------------------------------------

    def _metadata(
        self,
        result: BenchmarkResult,
        task: Task,
        agent: Agent,
        models: list[dict[str, Any]],
        result_dir: Path,
        output_mode: str,
        agent_archive_dir: str,
        timeout: int | None,
        started_at: datetime,
        ended_at: datetime,
    ) -> dict[str, Any]:
        """Build self-contained metadata for a single benchmark result."""
        elapsed_seconds = (ended_at - started_at).total_seconds()
        return {
            "schema_version": 2,
            "output_mode": output_mode,
            "result_dir": str(result_dir),
            "task": task.task_id,
            "task_group": task.group_id,
            "task_ref": task.task_ref,
            "agent": {
                "name": agent.agent_id,
                "harness": agent.harness_id,
                "harness_config": agent.harness_config,
                "models": agent.model_names,
            },
            "agent_archive_dir": agent_archive_dir,
            "agent_identity_hash": _agent_identity_hash(agent.harness_id, models),
            "models": [
                {
                    "name": configured_name,
                    "model": model_config.get("model"),
                    "base_url": model_config.get("base_url"),
                    "api": model_config.get("api"),
                    "identity_hash": _model_identity_hash(model_config),
                    "cost": model_config.get("cost") or {
                        "currency": "USD",
                        "input_per_token": 0.0,
                        "cached_input_per_token": 0.0,
                        "output_per_token": 0.0,
                    },
                }
                for configured_name, model_config in zip(agent.model_names, models, strict=False)
            ],
            "status": result.status,
            "complete": result.status in CONCLUSIVE_STATUSES,
            "failure_phase": result.failure_phase,
            "error": result.error,
            "timeout_seconds": timeout,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "elapsed_seconds": elapsed_seconds,
            "hostname": socket.gethostname(),
            "host_metadata": self._host_metadata(),
            "cwd": str(Path.cwd()),
            "package_root": str(self.package_root),
            "abc_bench_git_sha": self._git_sha(),
            "ci": self._ci_metadata(),
            "usage": result.usage_summary,
            "files": {
                "result": "result.json",
                "metadata": "metadata.json",
                "usage": "usage.json",
                "workspace": "workspace/",
                "harness_output": "harness_output/",
                "verify_log": result.verify_log,
            },
        }

    def _host_metadata(self) -> dict[str, Any]:
        uname = platform.uname()
        return {
            "os": {
                "system": uname.system,
                "release": uname.release,
                "version": uname.version,
                "machine": uname.machine,
            },
            "cpu": {
                "model": self._cpu_model(),
                "architecture": uname.machine,
                "logical_cores": os.cpu_count(),
            },
            "memory": {"total_bytes": self._meminfo_total_bytes()},
            "gpus": self._gpu_metadata(),
        }

    def _cpu_model(self) -> str | None:
        try:
            lines = Path("/proc/cpuinfo").read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() == "model name":
                return value.strip() or None
        return None

    def _meminfo_total_bytes(self) -> int | None:
        try:
            lines = Path("/proc/meminfo").read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
        return None

    def _gpu_metadata(self) -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return []
        gpus = []
        for line in completed.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 2)]
            if len(parts) != 3:
                continue
            index_text, name, memory_mib_text = parts
            try:
                index = int(index_text)
                memory_total_bytes = int(memory_mib_text) * 1024 * 1024
            except ValueError:
                continue
            gpus.append({"index": index, "name": name, "memory_total_bytes": memory_total_bytes})
        return gpus

    def _git_sha(self) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.package_root, check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
        return completed.stdout.strip() or None

    def _ci_metadata(self) -> dict[str, str]:
        keys = [
            "CI_PROJECT_ID", "CI_PROJECT_PATH", "CI_PIPELINE_ID", "CI_PIPELINE_URL",
            "CI_JOB_ID", "CI_JOB_URL", "CI_COMMIT_SHA", "CI_COMMIT_REF_NAME",
            "CI_RUNNER_DESCRIPTION", "CI_RUNNER_TAGS",
            "SLURM_JOB_ID", "SLURM_JOB_NODELIST", "SCHEDULER_PARAMETERS",
        ]
        return {key: os.environ[key] for key in keys if os.environ.get(key)}

    # -- in-container paths / bind mounts -------------------------------------

    def _container_run_paths(self) -> dict[str, Path]:
        """Return anonymous in-container paths for a benchmark run."""
        return {
            "run_root": CONTAINER_RUN_ROOT,
            "workspace": CONTAINER_WORKSPACE_ROOT,
            "prompt": CONTAINER_RUN_ROOT / "prompt.md",
            "group_prompt": CONTAINER_RUN_ROOT / "group_prompt.md",
            "harness_dir": CONTAINER_RUN_ROOT / "harness",
            "build_env": CONTAINER_RUN_ROOT / "build_env.sh",
            "models_config": CONTAINER_RUN_ROOT / "models_config.yaml",
            "harness_config": CONTAINER_RUN_ROOT / "harness_config.yaml",
            "harness_output": CONTAINER_RUN_ROOT / "harness_output",
            "verification": CONTAINER_RUN_ROOT / "verification",
            "sandbox_helper": CONTAINER_RUN_ROOT / "sandbox_helper.py",
            "result_dir": CONTAINER_RUN_ROOT,
            "task_env": CONTAINER_WORKSPACE_ROOT / "abc-bench-task-env",
        }

    def _container_bind_mounts(self, result_dir: Path) -> list[BindMount]:
        """Return bind mounts required for a benchmark run."""
        return [
            BindMount(result_dir, CONTAINER_RUN_ROOT),
            BindMount(result_dir / "workspace", CONTAINER_WORKSPACE_ROOT),
        ]
