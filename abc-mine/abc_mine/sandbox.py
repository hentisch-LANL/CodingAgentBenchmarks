"""Sandbox wiring: staging assets and running one agent in the container.

This module owns the single most failure-prone piece of mechanics in the tool:
getting a host directory, a source tree, and a set of staged files presented to an
agent inside a Charliecloud container, consistently, across all three stages.

The single-mount rule
---------------------
Everything the agent sees lives **under one host directory**, bind-mounted at one
in-container path. Nothing is nested.

This is not a stylistic preference. With ``ch-run --write-fake`` a bind-mount
*target* cannot be created inside an already-bound directory: binding
``workspace/ -> /workspace`` and then ``src/ -> /workspace/src`` fails with
``can't mkdir /workspace/src under existing bind-mount /workspace`` whenever the
target does not already exist in the image. The original pipeline hit exactly this
and, notably, its own handoff notes record that the nested pattern *should* have
failed in two stages that nonetheless produced real output — a discrepancy never
explained.

Rather than depend on behaviour nobody could account for, this module only ever
uses the pattern that was verified to work: materialize everything inside one host
directory (the source tree as a git worktree, the staged prompt, the outputs) and
carry it in with a single mount. There is no ambiguity about whether it works, and
the mechanism is identical in every stage.

A consequence worth stating: because we never inject a tools directory into the
container, **the container must already contain whatever the toolchain commands
invoke.** That is why those commands are declared in config and proved by
``preflight`` — the verifiers that ship inside emitted tasks have to call something
that still exists at grading time, when even fewer host paths are available.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from automaton.agent import Agent
from automaton.config import AutomatonConfig
from automaton.harness import Harness
from automaton.sandbox import BindMount, ExecutionResult, Sandbox

from .config import MineConfig

#: In-container root for the agent's own working area. Deliberately **not**
#: ``/workspace``: many images (and abc-bench itself at grading time) attach
#: meaning to ``/workspace``, and colliding with that is how nested-mount
#: failures start. Keeping the harness's scratch area on its own path leaves
#: ``/workspace`` free for the codebase.
CONTAINER_RUN_ROOT = Path("/lbm/run")

#: In-container root for the single mount carrying everything the agent reads and
#: writes.
CONTAINER_WORK_ROOT = Path("/workspace")

#: Directory name, inside the work root, holding agent deliverables.
OUT_DIRNAME = "out"

#: Harness assets shipped with the package.
HARNESS_DIR = Path(__file__).resolve().parent / "harness"

#: Harness id; must match ``harness.yaml``.
HARNESS_ID = "mine-agent"

#: Agent name registered in the generated automaton config.
AGENT_NAME = "mine-agent"


@dataclass
class AgentRun:
    """Result of one agent invocation.

    Attributes
    ----------
    execution : ExecutionResult
        automaton's raw result (success flag, return code, log path, failure
        detail).
    work_dir : Path
        Host directory that was mounted at :data:`CONTAINER_WORK_ROOT`.
    run_dir : Path
        Host directory holding staged assets and harness output.
    """

    execution: ExecutionResult
    work_dir: Path
    run_dir: Path

    @property
    def ok(self) -> bool:
        """True if the agent ran and reported success (dry runs are not ok)."""
        return bool(self.execution.success) and not getattr(self.execution, "dry_run", False)

    @property
    def dry_run(self) -> bool:
        """True if the sandbox only printed what it would have done."""
        return bool(getattr(self.execution, "dry_run", False))

    @property
    def detail(self) -> str:
        """Best available failure description."""
        return getattr(self.execution, "failure_detail", "") or ""

    def out_path(self, *parts: str) -> Path:
        """Return a host path beneath the agent's ``out/`` directory."""
        return self.work_dir.joinpath(OUT_DIRNAME, *parts)


def build_automaton_config(model: Mapping[str, str]) -> AutomatonConfig:
    """Build an :class:`AutomatonConfig` from the resolved model settings.

    The provider/model/agent/harness graph is generated in memory rather than read
    from a file, so there is exactly one way to configure the model — the
    environment — and no chance of a stale YAML silently overriding it. The API key
    is referenced by *variable name* (``api_key_env``), never by value, so it is
    never written to disk.
    """
    data: dict[str, Any] = {
        "providers": {
            "llm": {"base_url": model["base_url"], "api_key_env": "LLM_API_KEY"},
        },
        "models": {
            "llm": {"provider": "llm", "model": model["model"], "api": model["api"]},
        },
        "agents": {
            AGENT_NAME: {
                "harness": HARNESS_ID,
                "models": ["llm"],
                # Subagents and bash are both required: agents research with
                # subagents to keep their own context lean, and compile/test
                # through bash.
                "harness_config": {"subagents": True, "bash": True},
            },
        },
        "harnesses": {
            HARNESS_ID: {
                "description": "Autonomous benchmark-mining agent (OpenCode).",
            },
        },
        "defaults": {},
    }

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="lbm-automaton-", delete=False
    )
    try:
        yaml.safe_dump(data, tmp)
        tmp.flush()
        tmp.close()
        return AutomatonConfig(config_path=Path(tmp.name))
    finally:
        # AutomatonConfig has already parsed the file into memory.
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def make_agent(automaton_cfg: AutomatonConfig) -> Agent:
    """Return the configured mining :class:`Agent`."""
    return Agent(AGENT_NAME, automaton_cfg)


def make_harness() -> Harness:
    """Return the packaged mining :class:`Harness`."""
    if not (HARNESS_DIR / "harness.yaml").is_file():
        raise RuntimeError(
            f"harness assets are missing from the installation: {HARNESS_DIR}"
        )
    return Harness(HARNESS_ID, HARNESS_DIR)


def make_sandbox(cfg: MineConfig, *, dry_run: bool = False) -> Sandbox:
    """Return a :class:`Sandbox` with this tool's in-container layout."""
    return Sandbox(
        dry_run=dry_run,
        container_run_root=CONTAINER_RUN_ROOT,
        container_workspace_root=CONTAINER_WORK_ROOT,
    )


@contextmanager
def scratch_dir(cfg: MineConfig, prefix: str) -> Iterator[Path]:
    """Yield a node-local scratch directory, removed on exit.

    Heavy, throwaway state (worktrees, build trees, agent transcripts) belongs
    here rather than in the run's durable output tree: a sweep over a large history
    would otherwise accumulate many gigabytes of material nobody reads, on shared
    storage. Removal is unconditional, including on failure — callers that need to
    preserve evidence must copy it out first (see ``keep_logs``).
    """
    root = Path(
        tempfile.mkdtemp(prefix=prefix, dir=os.environ.get("LBM_SCRATCH_DIR", cfg.execution.scratch_dir))
    )
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _container_paths(sandbox: Sandbox) -> dict[str, Path]:
    """Build the in-container asset paths automaton's ``run_agent`` requires."""
    run_root = sandbox.container_run_root
    work_root = sandbox.container_workspace_root
    return {
        "run_root": run_root,
        "workspace": work_root,
        "prompt": run_root / "prompt.md",
        "harness_dir": run_root / "harness",
        "build_env": run_root / "build_env.sh",
        "models_config": run_root / "models_config.yaml",
        "harness_config": run_root / "harness_config.yaml",
        "harness_output": run_root / "harness_output",
        "verification": run_root / "verification",
        "sandbox_helper": run_root / "sandbox_helper.py",
        "result_dir": run_root,
        "task_env": work_root / "lbm-task-env",
    }


def run_agent(
    cfg: MineConfig,
    *,
    sandbox: Sandbox,
    harness: Harness,
    agent: Agent,
    work_dir: Path,
    run_dir: Path,
    prompt_text: str,
    build_env_text: str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> AgentRun:
    """Stage assets and run one agent against ``work_dir``.

    ``work_dir`` must already contain everything the agent should see — the source
    worktree, the staged inputs, and an ``out/`` directory for its deliverables.
    It is mounted, whole, at :data:`CONTAINER_WORK_ROOT`; see the module docstring
    for why nothing is nested.

    Parameters
    ----------
    work_dir : Path
        Host directory mounted at the container work root.
    run_dir : Path
        Host directory for staged prompt/harness assets and harness output.
    prompt_text : str
        The fully-composed prompt.
    build_env_text : str, optional
        Contents of the pre-agent setup script. A sensible no-op is used when
        omitted.
    extra_env : mapping, optional
        Additional environment variables for the agent process.
    """
    work_dir, run_dir = Path(work_dir), Path(run_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / OUT_DIRNAME).mkdir(parents=True, exist_ok=True)

    paths = _container_paths(sandbox)

    (run_dir / "prompt.md").write_text(prompt_text)

    build_env = run_dir / "build_env.sh"
    build_env.write_text(
        build_env_text
        if build_env_text is not None
        else "#!/usr/bin/env bash\n"
        "# No setup required: the container is expected to be self-contained.\n"
        "exit 0\n"
    )
    build_env.chmod(0o755)

    staged_harness = run_dir / "harness"
    if staged_harness.exists():
        shutil.rmtree(staged_harness)
    shutil.copytree(
        harness.harness_dir,
        staged_harness,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    helper = run_dir / "sandbox_helper.py"
    helper.write_text(sandbox.sandbox_helper_script())
    helper.chmod(0o755)

    harness_output = run_dir / "harness_output"
    harness_output.mkdir(exist_ok=True)

    # Exactly two mounts, neither nested inside the other.
    bind_mounts = [
        BindMount(run_dir, sandbox.container_run_root),
        BindMount(work_dir, sandbox.container_workspace_root),
    ]

    env: dict[str, str] = {
        "LBM_WORKSPACE": str(paths["workspace"]),
        "LBM_OUT": str(paths["workspace"] / OUT_DIRNAME),
    }
    env.update(cfg.toolchain.rendered_env(cfg.codebase.source_mount))
    if extra_env:
        env.update(extra_env)

    sandbox.run_command(
        cfg.container.image,
        ["bash", str(paths["build_env"])],
        cwd=paths["workspace"],
        log_file=run_dir / "build_env.log",
        env_vars=dict(env),
        bind_mounts=bind_mounts,
        network=True,
    )

    execution = sandbox.run_agent(
        cfg.container.image,
        harness,
        agent,
        workspace=paths["workspace"],
        run_dir=run_dir,
        container_paths=paths,
        bind_mounts=bind_mounts,
        proxy_log=harness_output / "model_proxy.log",
        usage_records_file=harness_output / "usage.jsonl",
        harness_output_dir=harness_output,
        models_config_file=run_dir / "models_config.yaml",
        harness_config_file=run_dir / "harness_config.yaml",
        extra_env=env,
        timeout=cfg.execution.timeout_seconds,
        sandboxed=cfg.execution.sandboxed,
    )

    return AgentRun(execution=execution, work_dir=work_dir, run_dir=run_dir)


def run_command(
    cfg: MineConfig,
    *,
    sandbox: Sandbox,
    command: str,
    work_dir: Path,
    log_file: Path,
    mount_at: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
) -> int | None:
    """Run one shell command in the container against ``work_dir``.

    Used by ``preflight`` (to prove the toolchain works) and by the build stage's
    independent polarity re-check. ``bash -lc`` is used so the command string may
    use ordinary shell syntax — pipes, quoting, ``&&`` — exactly as written in the
    config.

    Returns the exit status, or ``None`` under a dry run.
    """
    target = mount_at or sandbox.container_workspace_root
    log_file.parent.mkdir(parents=True, exist_ok=True)
    return sandbox.run_command(
        cfg.container.image,
        ["bash", "-lc", command],
        cwd=target,
        log_file=log_file,
        env_vars=dict(env or {}),
        bind_mounts=[BindMount(Path(work_dir), target)],
        network=False,
        check=check,
    )


def read_usage(run_dir: Path) -> dict[str, Any]:
    """Summarize token usage from a harness's ``usage.jsonl``.

    Recorded per item so a sweep's cost is attributable after the fact, rather
    than being a single opaque bill.
    """
    path = Path(run_dir) / "harness_output" / "usage.jsonl"
    if not path.is_file():
        return {}
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals["requests"] += 1
            for key, aliases in (
                ("input_tokens", ("input_tokens", "prompt_tokens")),
                ("output_tokens", ("output_tokens", "completion_tokens")),
            ):
                for alias in aliases:
                    value = record.get(alias)
                    if isinstance(value, (int, float)):
                        totals[key] += int(value)
                        break
    return totals


def preserve_logs(
    run_dir: Path, dest: Path, *, keep_logs: str, failed: bool
) -> Path | None:
    """Copy agent transcripts out of scratch, per the retention policy.

    Scratch is deleted unconditionally, so anything worth keeping must be copied
    first. The default policy (``failed``) keeps transcripts for failures only:
    enough to diagnose what went wrong, without retaining gigabytes of logs for
    thousands of successful items.

    Returns the destination directory, or ``None`` if nothing was kept.
    """
    if keep_logs == "none" or (keep_logs == "failed" and not failed):
        return None

    source = Path(run_dir) / "harness_output"
    if not source.is_dir():
        return None

    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Transcripts and proxy logs only; the staged harness copy is reproducible
    # from the package and would just bloat the output tree.
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("opencode"))

    for name in ("build_env.log", "prompt.md"):
        candidate = Path(run_dir) / name
        if candidate.is_file():
            shutil.copy2(candidate, dest / name)
    return dest
