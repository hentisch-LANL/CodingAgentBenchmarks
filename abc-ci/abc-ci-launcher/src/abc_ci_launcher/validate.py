"""Validation utilities for abc-ci inputs."""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

import yaml


def _import_abc_bench() -> tuple[object, object, object]:
    """Import abc-bench config, task, and agent helpers or exit clearly."""
    try:
        from abc_bench.config import Config
        from automaton import Agent
        from abc_bench.task import Task
    except ImportError:
        print(
            "ERROR: abc-bench must be installed locally to use abc-ci launcher validation.",
            file=sys.stderr,
        )
        print("Install abc-bench and try again.", file=sys.stderr)
        raise SystemExit(2)
    return Config, Task, Agent


def _abc_bench_resolver():
    """Return a task-group resolver from installed abc-bench."""
    Config, _, _ = _import_abc_bench()
    return Config().group_resolver()


def _abc_bench_config_path() -> Path:
    """Return the installed abc-bench default config path."""
    Config, _, _ = _import_abc_bench()
    return Config().config_path


def get_available_tasks() -> list[str]:
    """Return available canonical task refs from installed abc-bench."""
    _, Task, _ = _import_abc_bench()
    return Task.list_tasks(_abc_bench_resolver())


def get_available_task_groups() -> list[str]:
    """Return available task groups from installed abc-bench."""
    _, Task, _ = _import_abc_bench()
    return Task.list_task_groups(_abc_bench_resolver())


def get_available_agents(config_path: str | None = None) -> list[str]:
    """Return available agent ids from installed abc-bench config."""
    Config, _, Agent = _import_abc_bench()
    config = Config(config_path) if config_path is not None else Config()
    return Agent.list_agents(config)


def is_glob_pattern(value: str) -> bool:
    """Check whether a string contains shell-style glob metacharacters."""

    return any(c in value for c in "*?[]")


def resolve_agents(pattern: str, config_path: str | None = None) -> list[str]:
    """Expand a glob pattern to matching agent ids from the abc-bench config.

    Returns a sorted list of agent ids that match the given pattern.
    """

    available = get_available_agents(config_path)
    return sorted(fnmatch.filter(available, pattern))


def get_available_models(config_path: str | None = None) -> list[str]:
    """Return available model ids from `ABC_BENCH_CONFIG`.

    This remains useful for displaying underlying model references in tooling.
    """
    if config_path is None:
        config_file = _abc_bench_config_path()
    else:
        config_file = Path(config_path).expanduser().resolve()

    try:
        if not config_file.is_file():
            return []

        with config_file.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        if not isinstance(config, dict):
            return []

        models = config.get("models", {})
        if not isinstance(models, dict):
            return []

        return sorted(models.keys())
    except Exception:
        return []


def confirm_or_exit(message: str) -> bool:
    """Prompt for confirmation and return whether the user accepted."""
    try:
        response = input(f"{message} [y/N]: ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return False


def resolve_task_ref(task: str) -> str:
    """Resolve a short or canonical task id to a canonical group/task ref."""
    _, Task, _ = _import_abc_bench()

    try:
        resolved = Task(task, _abc_bench_resolver())
    except FileNotFoundError:
        print(f"WARNING: Task '{task}' not found in installed abc-bench", file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

    return resolved.task_ref


def validate_task(task: str, force: bool = False) -> bool:
    """Validate a task id against installed abc-bench."""
    available_tasks = get_available_tasks()

    if task in available_tasks:
        return True

    if "/" not in task:
        try:
            resolve_task_ref(task)
            return True
        except SystemExit:
            if force:
                return True
            return False

    if force:
        return True

    print(f"WARNING: Task '{task}' not found in installed abc-bench", file=sys.stderr)
    print(f"Available tasks: {', '.join(available_tasks[:10])}", file=sys.stderr)
    if len(available_tasks) > 10:
        print(f"... and {len(available_tasks) - 10} more", file=sys.stderr)

    return confirm_or_exit("Continue anyway?")


def validate_task_group(task_group: str, force: bool = False) -> bool:
    """Validate a task group id against installed abc-bench."""
    available_groups = get_available_task_groups()

    if task_group in available_groups:
        return True

    if force:
        return True

    print(f"WARNING: Task group '{task_group}' not found in installed abc-bench", file=sys.stderr)
    print(f"Available task groups: {', '.join(available_groups)}", file=sys.stderr)

    return confirm_or_exit("Continue anyway?")


def validate_agent(agent: str, config_path: str | None = None, force: bool = False) -> bool:
    """Validate an agent id, optionally allowing user confirmation."""
    available_agents = get_available_agents(config_path)

    if agent in available_agents:
        return True

    if force:
        return True

    print(f"WARNING: Agent '{agent}' not found in installed abc-bench config", file=sys.stderr)
    print(f"Available agents: {', '.join(available_agents)}", file=sys.stderr)

    return confirm_or_exit("Continue anyway?")
