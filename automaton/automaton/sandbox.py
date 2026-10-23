"""Sandboxed execution engine for Automaton.

:class:`Sandbox` runs commands inside a Charliecloud container with a clean,
minimal environment and optional memory limits. It provides two entry points:

- :meth:`Sandbox.run_command` -- run an arbitrary, non-agentic command inside a
  container. By default this runs with loopback-only networking and no model
  proxy. This is the general-purpose "run some code against the deliverable"
  path (a benchmark orchestrator uses it for environment setup and
  verification).
- :meth:`Sandbox.run_agent` -- run an autonomous agent (a harness driving one
  or more models) inside the container, brokering all model traffic through a
  loopback-only :class:`~automaton.proxy.ModelProxy`. The agent's deliverable
  is left in the bind-mounted workspace directory. Returns an
  :class:`ExecutionResult`; Automaton stays agnostic about the deliverable's
  contents.

Automaton is content-free: the caller supplies the container image, the
harness, the agent, staged file paths, and bind mounts.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import Agent
from .harness import Harness
from .proxy import (
    MODEL_PROXY_REQUEST_DEADLINE_SECONDS,
    ModelProxy,
)
from .sandbox_helper import SCRIPT as SANDBOX_HELPER_SCRIPT
from .usage import aggregate_usage


DEFAULT_CONTAINER_RUN_ROOT = Path("/automaton/run")
DEFAULT_CONTAINER_WORKSPACE_ROOT = Path("/workspace")
DEFAULT_SANDBOX_PROXY_PORT = 18080

# Engine <-> harness contract defaults. abc_bench (or any caller) matches these
# in its harness run scripts. Configurable via Sandbox constructor.
DEFAULT_PROXY_API_KEY_ENV = "AUTOMATON_MODEL_PROXY_API_KEY"
DEFAULT_PROXY_API_KEY_VALUE = "automaton-proxy"
DEFAULT_PROXY_PROVIDER_ID = "automaton-proxy"


@dataclass(frozen=True)
class BindMount:
    """Host/container bind mount specification for Charliecloud."""

    host_path: Path
    container_path: Path


class CommandRunFailed(RuntimeError):
    """A sandboxed command exited non-zero.

    Attributes
    ----------
    returncode : int
        Process return code.
    log_file : Path
        Path to the command's log file.
    detail : str
        Human-readable failure detail.
    """

    def __init__(self, returncode: int, log_file: Path, detail: str):
        self.returncode = returncode
        self.log_file = log_file
        self.detail = detail
        super().__init__(detail)


@dataclass
class ExecutionResult:
    """Result of running an agent in the sandbox.

    Automaton is agnostic about the deliverable's *contents*; it reports only
    execution facts and the workspace location where the deliverable lives.

    Attributes
    ----------
    success : bool
        Whether the harness process exited cleanly.
    returncode : int | None
        Harness process return code (None if not run, e.g. dry-run).
    workspace : Path
        Host path to the deliverable workspace.
    endpoint_exhausted : bool
        True if the model proxy could not obtain a complete upstream response.
    context_length_exceeded : bool
        True if the harness failed because an upstream request exceeded the
        model's context window (an oversized-prompt HTTP 4xx).
    failure_detail : str | None
        Failure detail when ``success`` is False.
    proxy_error_detail : str | None
        Last upstream/model proxy error, if any.
    usage_records : list[dict]
        Per-request usage/cost accounting records from the model proxy.
    usage_summary : dict
        Aggregated usage summary computed from ``usage_records``.
    log_file : Path | None
        Path to the harness process log.
    blocked_provider_executed_tools : list[str]
        Provider-executed tool types the model proxy refused to forward. Non-empty
        means the agent tried to use a tool that runs on the provider's
        infrastructure outside the sandbox; the run should not be graded as an
        ordinary task failure.
    dry_run : bool
        True if this was a dry run (no execution performed).
    """

    success: bool
    workspace: Path
    returncode: int | None = None
    endpoint_exhausted: bool = False
    context_length_exceeded: bool = False
    failure_detail: str | None = None
    proxy_error_detail: str | None = None
    usage_records: list[dict[str, Any]] = field(default_factory=list)
    usage_summary: dict[str, Any] = field(default_factory=dict)
    log_file: Path | None = None
    blocked_provider_executed_tools: list[str] = field(default_factory=list)
    dry_run: bool = False


class Sandbox:
    """Runs commands and agents inside a Charliecloud container.

    Parameters
    ----------
    host_root : Path, optional
        Host directory bound into the container as ``container_base_root`` when
        no explicit bind mounts are supplied. Defaults to the current directory.
    dry_run : bool, optional
        If True, print commands instead of executing them.
    stream_output : bool, optional
        If True, mirror container output to the terminal.
    harness_memory_limit_bytes : int, optional
        Fixed harness memory cap in bytes.
    harness_memory_limit_fraction : float, optional
        Fraction of available memory to cap the harness at. Default 0.9.
    proxy_api_key_env : str, optional
        Env var name the harness reads for the proxy auth token.
    proxy_api_key_value : str, optional
        Proxy auth token value.
    proxy_provider_id : str, optional
        Provider id used for proxied model routing (``<provider>/<model>``).
    sandbox_proxy_port : int, optional
        Loopback port the sandbox-local proxy listens on.
    container_run_root : Path, optional
        In-container root for staged run assets.
    container_workspace_root : Path, optional
        In-container workspace mount point.
    container_base_root : Path, optional
        Default in-container mount point for ``host_root``.
    allowed_provider_executed_tools : Iterable[str], optional
        Provider-executed tool types the model proxy may forward. Defaults to
        none, which blocks them: such tools run on the provider's infrastructure
        instead of in the sandbox, so the sandbox network namespace cannot
        confine them and their use leaves no trace in the harness transcript.
    """

    def __init__(
        self,
        host_root: Path | None = None,
        dry_run: bool = False,
        stream_output: bool = True,
        harness_memory_limit_bytes: int | None = None,
        harness_memory_limit_fraction: float | None = 0.9,
        proxy_api_key_env: str = DEFAULT_PROXY_API_KEY_ENV,
        proxy_api_key_value: str = DEFAULT_PROXY_API_KEY_VALUE,
        proxy_provider_id: str = DEFAULT_PROXY_PROVIDER_ID,
        sandbox_proxy_port: int = DEFAULT_SANDBOX_PROXY_PORT,
        container_run_root: Path = DEFAULT_CONTAINER_RUN_ROOT,
        container_workspace_root: Path = DEFAULT_CONTAINER_WORKSPACE_ROOT,
        container_base_root: Path = Path("/automaton/base"),
        allowed_provider_executed_tools: frozenset[str] | set[str] | None = None,
    ):
        self.host_root = (Path(host_root) if host_root else Path.cwd()).resolve()
        self.dry_run = dry_run
        self.stream_output = stream_output
        self.harness_memory_limit_bytes = harness_memory_limit_bytes
        self.harness_memory_limit_fraction = harness_memory_limit_fraction
        self.proxy_api_key_env = proxy_api_key_env
        self.proxy_api_key_value = proxy_api_key_value
        self.proxy_provider_id = proxy_provider_id
        self.sandbox_proxy_port = sandbox_proxy_port
        self.container_run_root = container_run_root
        self.container_workspace_root = container_workspace_root
        self.container_base_root = container_base_root
        self.allowed_provider_executed_tools = frozenset(
            str(name).strip().lower() for name in (allowed_provider_executed_tools or ())
        )

    # -- static helper: the model-proxy contract the harness must speak -------

    @property
    def proxy_base_url(self) -> str:
        return f"http://127.0.0.1:{self.sandbox_proxy_port}"

    def sandbox_helper_script(self) -> str:
        """Return the sandbox helper script source to stage into a run dir."""
        return SANDBOX_HELPER_SCRIPT

    def harness_models_config(self, agent: Agent, *, sandboxed: bool = True) -> dict[str, Any]:
        """Build the harness-facing models config (proxy wiring).

        When ``sandboxed`` is True, models are routed through the loopback
        proxy; otherwise the harness talks to the upstream base_url directly.
        """
        return {
            "proxy_base_url": self.proxy_base_url if sandboxed else None,
            "api_key_env": self.proxy_api_key_env,
            "models": [
                {
                    "name": model.name,
                    "model": model.config["model"],
                    "base_url": self.proxy_base_url if sandboxed else str(model.config["base_url"]),
                    "opencode_model": f"{self.proxy_provider_id}/{model.name}",
                    "proxied_base_url": str(model.config["base_url"]),
                    # Opaque API-style hint carried verbatim from provider/model
                    # config. Automaton assigns no meaning or default; the
                    # harness interprets this value.
                    "api": model.config.get("api"),
                }
                for model in agent.models
            ],
        }

    def model_registry(self, agent: Agent) -> dict[str, dict[str, Any]]:
        """Build the upstream model registry consumed by :class:`ModelProxy`."""
        registry: dict[str, dict[str, Any]] = {}
        for model in agent.models:
            registry[model.name] = {
                **model.config,
                "api_key": self._model_api_key(model.config),
                "opencode_model": f"{self.proxy_provider_id}/{model.name}",
            }
        return registry

    # -- public: run an arbitrary (non-agentic) command -----------------------

    def run_command(
        self,
        container_path: Path,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env_vars: dict[str, str] | None = None,
        bind_mounts: list[BindMount] | None = None,
        log_file: Path | None = None,
        memory_limit_bytes: int | None = None,
        network: bool = False,
        check: bool = True,
    ) -> int:
        """Run an arbitrary command inside the container.

        By default runs with loopback-only networking (``network=False``) and
        no model proxy. This is the general-purpose path for running
        non-agentic code (setup, verification, tooling) against a workspace.

        Parameters
        ----------
        container_path : Path
            Path to the built container directory.
        argv : list[str]
            Command to run inside the container (e.g. ``["bash", script]``).
        cwd : Path, optional
            Working directory inside the container.
        env_vars : dict[str, str], optional
            Extra environment variables to set inside the container.
        bind_mounts : list[BindMount], optional
            Bind mounts. Defaults to binding ``host_root`` at
            ``container_base_root``.
        log_file : Path, optional
            Combined stdout/stderr log path.
        memory_limit_bytes : int, optional
            Memory cap for the command.
        network : bool, optional
            If False (default), run inside a loopback-only network namespace
            with no outbound access. If True, run without network isolation.
        check : bool, optional
            If True, raise :class:`CommandRunFailed` on non-zero exit.

        Returns
        -------
        int
            The command's return code (or 0 in dry-run mode).
        """
        cmd = self._ch_run_prefix(
            container_path, cwd=cwd, env_vars=env_vars, bind_mounts=bind_mounts
        ) + ["--", *argv]

        if self.dry_run:
            print(f"[DRY-RUN] Would execute: {' '.join(cmd)}")
            if cwd:
                print(f"[DRY-RUN]   in directory: {cwd}")
            return 0

        if network:
            result = self._run_logged(
                cmd, log_file, cwd=self.host_root, memory_limit_bytes=memory_limit_bytes
            )
            returncode = result.returncode
        else:
            returncode = self._run_isolated_network_command(
                cmd,
                log_file=log_file if log_file is not None else self.host_root / "automaton-command.log",
                memory_limit_bytes=memory_limit_bytes,
            )

        if returncode != 0 and check:
            detail = self._command_failure_detail(returncode, log_file) if log_file else (
                f"Command failed with exit code {returncode}"
            )
            raise CommandRunFailed(returncode, log_file or Path("/dev/null"), detail)
        return returncode

    # -- public: run an autonomous agent --------------------------------------

    def run_agent(
        self,
        container_path: Path,
        harness: Harness,
        agent: Agent,
        *,
        workspace: Path,
        run_dir: Path,
        container_paths: dict[str, Path],
        bind_mounts: list[BindMount],
        proxy_log: Path,
        usage_records_file: Path,
        harness_output_dir: Path,
        models_config_file: Path,
        harness_config_file: Path,
        extra_env: dict[str, str] | None = None,
        timeout: int | None = None,
        sandboxed: bool = True,
    ) -> ExecutionResult:
        """Run an autonomous agent inside the sandbox.

        The caller is responsible for staging the workspace, prompt, harness
        assets, and sandbox helper script into ``run_dir`` before calling.

        Parameters
        ----------
        container_path : Path
            Built runtime container directory.
        harness : Harness
            Loaded harness.
        agent : Agent
            Configured agent (harness + models).
        workspace : Path
            In-container workspace path (where the deliverable is produced).
        run_dir : Path
            Host per-run directory (already populated with staged assets).
        container_paths : dict[str, Path]
            In-container paths for staged assets. Required keys:
            ``prompt``, ``harness_dir``, ``models_config``, ``harness_config``,
            ``harness_output``, ``sandbox_helper``, ``task_env``. Optional key:
            ``group_prompt`` — when present, forwarded to the harness as
            ``--group-prompt`` so it can inject shared task-group instructions
            between the system prompt and the per-task prompt.
        bind_mounts : list[BindMount]
            Bind mounts for the run.
        proxy_log : Path
            Model proxy log path.
        usage_records_file : Path
            JSONL path for per-request usage records.
        harness_output_dir : Path
            Host directory for harness output/logs.
        models_config_file : Path
            Host path to write the harness models config to.
        harness_config_file : Path
            Host path to write the agent harness config to.
        extra_env : dict[str, str], optional
            Additional environment variables passed into the harness container
            (e.g. task-facing workspace/env variables the caller defines).
        timeout : int, optional
            Per-run timeout in seconds passed to the harness.
        sandboxed : bool, optional
            If True (default), route model traffic through the loopback proxy
            and run the harness in an isolated network namespace. If False, run
            the harness with direct network access to upstream models.

        Returns
        -------
        ExecutionResult
            Execution facts and deliverable workspace location.
        """
        harness_output_dir.mkdir(parents=True, exist_ok=True)
        run_script = container_paths["harness_dir"] / "run_benchmark.sh"
        system_prompt = container_paths["harness_dir"] / "system_prompt.md"

        proxy: ModelProxy | None = None
        with ModelProxy(
            self.model_registry(agent),
            proxy_log,
            request_deadline_seconds=MODEL_PROXY_REQUEST_DEADLINE_SECONDS,
            usage_records_file=usage_records_file,
            allowed_provider_executed_tools=self.allowed_provider_executed_tools,
        ) as proxy:
            proxy.stream_errors = self.stream_output
            with open(models_config_file, "w") as f:
                yaml.dump(self.harness_models_config(agent, sandboxed=sandboxed), f)
            with open(harness_config_file, "w") as f:
                yaml.dump(agent.harness_config, f)

            env_vars = self._model_env_vars(models_config_file)
            if extra_env:
                env_vars.update(extra_env)

            harness_cmd = self._ch_run_prefix(container_path, env_vars=env_vars, bind_mounts=bind_mounts) + [
                "--",
                "bash",
                str(run_script),
                "--workspace", str(workspace),
                "--task-prompt", str(container_paths["prompt"]),
                "--system-prompt", str(system_prompt),
                "--models-config", str(container_paths["models_config"]),
                "--harness-config", str(container_paths["harness_config"]),
                "--output-dir", str(container_paths["harness_output"]),
                "--task-env", str(container_paths["task_env"]),
            ]
            group_prompt = container_paths.get("group_prompt")
            if group_prompt is not None:
                harness_cmd.extend(["--group-prompt", str(group_prompt)])
            if timeout is not None:
                harness_cmd.extend(["--timeout-seconds", str(timeout)])

            harness_log = harness_output_dir / "ch-run.log"

            if self.dry_run:
                print("[DRY-RUN] Would execute agent harness")
                print(f"[DRY-RUN] Harness command: {' '.join(harness_cmd)}")
                return ExecutionResult(success=True, workspace=workspace, dry_run=True, log_file=harness_log)

            memory_limit_bytes = self._harness_memory_limit_bytes()
            try:
                if sandboxed:
                    sock = proxy.sandbox_socket()
                    returncode = self._run_single_sandbox_command(
                        sock=sock,
                        command=harness_cmd,
                        log_file=harness_log,
                        helper_script=run_dir / "sandbox_helper.py",
                        memory_limit_bytes=memory_limit_bytes,
                        failure_log=harness_log,
                    )
                else:
                    result = self._run_logged(
                        harness_cmd, harness_log, cwd=self.host_root, memory_limit_bytes=memory_limit_bytes
                    )
                    returncode = result.returncode
                    if returncode != 0:
                        raise CommandRunFailed(
                            returncode, harness_log, self._command_failure_detail(returncode, harness_log)
                        )
            except CommandRunFailed as exc:
                proxy_error_detail = proxy.last_error_detail
                detail = exc.detail
                if proxy_error_detail and proxy_error_detail not in detail:
                    detail = f"{detail} Upstream model error: {proxy_error_detail}"
                usage_records = list(proxy.usage_records)
                return ExecutionResult(
                    success=False,
                    workspace=workspace,
                    returncode=exc.returncode,
                    endpoint_exhausted=proxy.endpoint_exhausted,
                    context_length_exceeded=proxy.context_length_exceeded,
                    failure_detail=detail,
                    proxy_error_detail=proxy_error_detail,
                    usage_records=usage_records,
                    usage_summary=aggregate_usage(usage_records),
                    log_file=harness_log,
                    blocked_provider_executed_tools=proxy.blocked_provider_executed_tools,
                )

            if not harness_log.exists():
                harness_log.write_text("")
            usage_records = list(proxy.usage_records)
            return ExecutionResult(
                success=True,
                workspace=workspace,
                returncode=returncode,
                endpoint_exhausted=proxy.endpoint_exhausted,
                usage_records=usage_records,
                usage_summary=aggregate_usage(usage_records),
                log_file=harness_log,
                # Recorded even on success: an agent may retry without the tool
                # and still exit 0, but the attempt must remain visible.
                blocked_provider_executed_tools=proxy.blocked_provider_executed_tools,
            )

    # -- sandboxed single command (unshare --net + fd-passed proxy) -----------
#
# --pid --mount-proc: without a private PID namespace the sandbox sees every
# host process on the node (`ps`), and /proc/<pid>/root of a same-user host
# process exposes the host filesystem, bypassing the container's binds.

    def _run_single_sandbox_command(
        self,
        sock,
        command: list[str],
        log_file: Path,
        helper_script: Path,
        memory_limit_bytes: int | None,
        failure_log: Path,
        allow_failure: bool = False,
    ) -> int:
        sandbox_env = {
            "AUTOMATON_PROXY_FD": str(sock.fileno()),
            "AUTOMATON_SANDBOX_PROXY_PORT": str(self.sandbox_proxy_port),
            "AUTOMATON_SANDBOX_CMD": json.dumps(command),
            "AUTOMATON_SANDBOX_LOG": str(log_file),
        }
        launch_cmd = [
            "env",
            *[f"{key}={value}" for key, value in sandbox_env.items()],
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--mount-proc",
            "--fork",
            "python",
            str(helper_script),
        ]
        if memory_limit_bytes is not None:
            launch_cmd = self._with_memory_limit(launch_cmd, memory_limit_bytes)
        sandbox_log = log_file.parent / f"{log_file.stem}.sandbox.log"
        result = self._run_logged(launch_cmd, sandbox_log, cwd=self.host_root, pass_fds=[sock.fileno()])
        if result.returncode != 0 and not allow_failure:
            raise CommandRunFailed(
                result.returncode,
                failure_log,
                self._command_failure_detail(result.returncode, failure_log),
            )
        return result.returncode

    def _run_isolated_network_command(
        self,
        command: list[str],
        log_file: Path,
        memory_limit_bytes: int | None,
    ) -> int:
        """Run a command inside a loopback-only network namespace, no proxy fd."""
        launch_cmd = [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--mount-proc",
            "--fork",
            "bash",
            "-c",
            'ip link set lo up 2>/dev/null || true; exec "$@"',
            "automaton-net-isolate",
            *command,
        ]
        if memory_limit_bytes is not None:
            launch_cmd = self._with_memory_limit(launch_cmd, memory_limit_bytes)
        result = self._run_logged(launch_cmd, log_file, cwd=self.host_root)
        return result.returncode

    # -- process execution ----------------------------------------------------

    def _run_logged(
        self,
        cmd: list[str],
        log_file: Path | None,
        cwd: Path | None = None,
        memory_limit_bytes: int | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess:
        """Run a command, writing combined stdout/stderr to log and terminal."""
        if memory_limit_bytes is not None:
            cmd = self._with_memory_limit(cmd, memory_limit_bytes)

        if log_file is None:
            result = subprocess.run(cmd, cwd=cwd, text=True, pass_fds=pass_fds)
            return subprocess.CompletedProcess(cmd, result.returncode)

        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w") as f:
            if memory_limit_bytes is not None:
                limit_message = f"Automaton harness memory cap: {self._format_bytes(memory_limit_bytes)}\n"
                f.write(limit_message)
                f.flush()
                if self.stream_output:
                    sys.stdout.write(limit_message)
                    sys.stdout.flush()
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                pass_fds=pass_fds,
            )
            assert process.stdout is not None
            for line in process.stdout:
                f.write(line)
                f.flush()
                if self.stream_output:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            returncode = process.wait()

        return subprocess.CompletedProcess(cmd, returncode)

    # -- memory limits --------------------------------------------------------

    def _harness_memory_limit_bytes(self) -> int | None:
        if self.harness_memory_limit_bytes is not None:
            return self.harness_memory_limit_bytes
        if self.harness_memory_limit_fraction is None:
            return None
        available_bytes = self._available_memory_bytes()
        if available_bytes is None:
            return None
        return max(1, int(available_bytes * self.harness_memory_limit_fraction))

    def _available_memory_bytes(self) -> int | None:
        cgroup_available = self._cgroup_available_memory_bytes()
        if cgroup_available is not None:
            return cgroup_available
        meminfo_available = self._meminfo_available_bytes()
        if meminfo_available is not None:
            return meminfo_available
        try:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            return None
        return int(pages) * int(page_size)

    def _cgroup_available_memory_bytes(self) -> int | None:
        for limit_path, usage_path in self._cgroup_memory_paths():
            try:
                value = limit_path.read_text().strip()
            except OSError:
                continue
            if not value or value == "max":
                continue
            try:
                limit = int(value)
            except ValueError:
                continue
            if not 0 < limit < 1 << 60:
                continue
            usage = 0
            if usage_path is not None:
                try:
                    usage = int(usage_path.read_text().strip())
                except (OSError, ValueError):
                    usage = 0
            return max(1, limit - usage)
        return None

    def _cgroup_memory_paths(self) -> list[tuple[Path, Path | None]]:
        cgroup_root = Path("/sys/fs/cgroup")
        candidates: list[tuple[Path, Path | None]] = []
        for controllers, relative_path in self._process_cgroup_paths():
            cgroup_path = cgroup_root / relative_path.lstrip("/")
            if controllers == [""]:
                candidates.append((cgroup_path / "memory.max", cgroup_path / "memory.current"))
            if "memory" in controllers:
                memory_path = cgroup_root / "memory" / relative_path.lstrip("/")
                candidates.append((memory_path / "memory.limit_in_bytes", memory_path / "memory.usage_in_bytes"))
        candidates.append((cgroup_root / "memory.max", cgroup_root / "memory.current"))
        candidates.append((cgroup_root / "memory/memory.limit_in_bytes", cgroup_root / "memory/memory.usage_in_bytes"))
        deduped = []
        seen = set()
        for candidate in candidates:
            if candidate[0] in seen:
                continue
            seen.add(candidate[0])
            deduped.append(candidate)
        return deduped

    def _process_cgroup_paths(self) -> list[tuple[list[str], str]]:
        try:
            lines = Path("/proc/self/cgroup").read_text().splitlines()
        except OSError:
            return []
        paths = []
        for line in lines:
            parts = line.split(":", 2)
            if len(parts) == 3:
                paths.append((parts[1].split(","), parts[2]))
        return paths

    def _meminfo_available_bytes(self) -> int | None:
        try:
            lines = Path("/proc/meminfo").read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
        return None

    def _with_memory_limit(self, cmd: list[str], memory_limit_bytes: int) -> list[str]:
        memory_limit_kib = max(1, memory_limit_bytes // 1024)
        return [
            "bash",
            "-c",
            "ulimit -v \"$1\"; shift; exec \"$@\"",
            "automaton-memory-limit",
            str(memory_limit_kib),
            *cmd,
        ]

    # -- failure detail -------------------------------------------------------

    def _command_failure_detail(self, returncode: int, log_file: Path) -> str:
        reason = f"Command failed with exit code {returncode}."
        if returncode == 124:
            reason = "Command timed out."
        elif returncode < 0:
            signal_name = self._signal_name(-returncode)
            reason = f"Command was terminated by signal {signal_name}."
        elif returncode >= 128:
            signal_name = self._signal_name(returncode - 128)
            reason = f"Command exited after signal {signal_name}."

        if returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}:
            reason += " This is commonly caused by an OOM killer or external scheduler cancellation."

        if log_file.exists():
            try:
                log_text = log_file.read_text(errors="replace").lower()
            except OSError:
                log_text = ""
            if any(token in log_text for token in ["memoryerror", "cannot allocate memory", "out of memory", "oom"]):
                reason += " Logs contain memory-allocation/OOM indicators."

        reason += f" See {log_file.name} for process output."
        try:
            with open(log_file, "a") as f:
                f.write(f"\n=== Automaton Command Failure ===\n{reason}\n")
        except OSError:
            pass
        return reason

    def _signal_name(self, signal_number: int) -> str:
        try:
            return signal.Signals(signal_number).name
        except ValueError:
            return str(signal_number)

    def _format_bytes(self, value: int) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        amount = float(value)
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.1f} {unit}" if unit != "B" else f"{value} B"
            amount /= 1024
        return f"{value} B"

    # -- model auth -----------------------------------------------------------

    def _model_env_vars(self, model_config: Path) -> dict[str, str]:
        """Return explicitly allowed model environment variables for the harness."""
        with open(model_config) as f:
            data = yaml.safe_load(f) or {}

        api_key_env = data.get("api_key_env")
        if not api_key_env or api_key_env == "None":
            return {}

        if api_key_env == self.proxy_api_key_env:
            return {self.proxy_api_key_env: self.proxy_api_key_value}

        api_key_value = os.environ.get(str(api_key_env))
        if not api_key_value:
            return {}

        return {str(api_key_env): api_key_value}

    def _model_api_key(self, model_config: dict[str, Any]) -> str:
        """Return the host-side upstream model API key."""
        api_key_env = model_config.get("api_key_env")
        if not api_key_env or api_key_env == "None":
            return ""
        api_key = os.environ.get(str(api_key_env))
        if not api_key:
            raise RuntimeError(f"API key environment variable {api_key_env} is not set")
        return api_key

    # -- ch-run invocation ----------------------------------------------------

    def _ch_run_prefix(
        self,
        container_path: Path,
        cwd: Path | None = None,
        env_vars: dict[str, str] | None = None,
        bind_mounts: list[BindMount] | None = None,
    ) -> list[str]:
        """Return the common Charliecloud invocation with required binds."""
        if bind_mounts is None:
            bind_mounts = [BindMount(self.host_root, self.container_base_root)]

        # --private-tmp: without it /tmp is the host node's /tmp, shared by every
        # run on the node, so one attempt's scratch files (build trees, patches,
        # partial solutions) are visible to later attempts and attempts are not
        # independent samples.
        cmd = ["ch-run", "--write-fake", "--private-tmp", "--uid=1000", "--gid=1000"]
        for mount in bind_mounts:
            host_path = mount.host_path.resolve()
            mount_container_path = mount.container_path
            cmd.append(f"--bind={host_path}:{mount_container_path}")
        clean_env = {
            "PATH": "/opt/conda/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": "/var/tmp/automaton-home",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
            "CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
            "CONDA_PLUGINS_AUTO_ACCEPT_TOS": "yes",
        }
        if env_vars:
            clean_env.update(env_vars)
        cmd.append("--unset-env=*")
        for key, value in clean_env.items():
            cmd.append(f"--set-env={key}={value}")
        if cwd is not None:
            cmd.append(f"--cd={cwd}")
        cmd.append(str(container_path))
        return cmd
