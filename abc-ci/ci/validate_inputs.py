#!/usr/bin/env python3
"""Validate abc-ci API trigger inputs."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.:-]+$")


def error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def require_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        error(f"required environment variable {name} is not set")
        return None
    return value


def validate_safe(name: str, value: str) -> bool:
    if SAFE_SEGMENT.match(value):
        return True
    error(f"{name} contains unsupported characters: {value!r}")
    return False


def _dev_mode_active() -> bool:
    """Return True when this run should target the dev results archive.

    Mirrors ci/resolve_results_dir.sh: dev mode is active when the DEV trigger
    variable is truthy, or when the pipeline runs on the configured dev branch.
    """

    if os.environ.get("DEV", "").strip() in {"1", "true", "True", "TRUE", "yes", "Yes"}:
        return True
    dev_ref = os.environ.get("ABC_BENCH_DEV_REF", "").strip() or "dev"
    return os.environ.get("CI_COMMIT_REF_NAME", "").strip() == dev_ref


def validate_task(value: str) -> bool:
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        error(f"TASK must use canonical task-group/task-id form, got {value!r}")
        return False
    if all(SAFE_SEGMENT.match(part) for part in parts):
        return True
    error(f"TASK contains unsupported characters: {value!r}")
    return False


def validate_task_group(value: str) -> bool:
    if SAFE_SEGMENT.match(value):
        return True
    error(f"TASK_GROUP contains unsupported characters: {value!r}")
    return False


def main() -> int:
    ok = True

    task_value = os.environ.get("TASK", "").strip()
    task_group_value = os.environ.get("TASK_GROUP", "").strip()
    if bool(task_value) == bool(task_group_value):
        error("exactly one of TASK or TASK_GROUP must be set")
        ok = False
    elif task_value:
        ok = validate_task(task_value) and ok
    else:
        ok = validate_task_group(task_group_value) and ok

    agent_value = require_env("AGENT")
    ok = ok and agent_value is not None
    if agent_value is not None:
        ok = validate_safe("AGENT", agent_value) and ok

    config_value = require_env("ABC_BENCH_CONFIG")
    ok = ok and config_value is not None
    if config_value:
        config_path = Path(config_value).expanduser()
        if not config_path.is_file():
            error(f"ABC_BENCH_CONFIG does not point to a file: {config_path}")
            ok = False

    results_var = "ABC_BENCH_RESULTS_DIR"
    if _dev_mode_active():
        results_var = "ABC_BENCH_RESULTS_DIR_DEV"

    results_value = require_env(results_var)
    ok = ok and results_value is not None
    if results_value:
        results_dir = Path(results_value).expanduser()
        try:
            results_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            error(f"could not create {results_var} {results_dir}: {exc}")
            ok = False
        else:
            if not os.access(results_dir, os.W_OK):
                error(f"{results_var} is not writable: {results_dir}")
                ok = False

    timeout = os.environ.get("TIMEOUT_SECONDS", "").strip()
    if timeout:
        try:
            timeout_seconds = int(timeout)
        except ValueError:
            error(f"TIMEOUT_SECONDS must be an integer, got {timeout!r}")
            ok = False
        else:
            if timeout_seconds <= 0:
                error("TIMEOUT_SECONDS must be positive")
                ok = False

    attempts = os.environ.get("ATTEMPTS", "").strip()
    if attempts:
        try:
            attempts_value = int(attempts)
        except ValueError:
            error(f"ATTEMPTS must be an integer, got {attempts!r}")
            ok = False
        else:
            if attempts_value <= 0:
                error("ATTEMPTS must be positive")
                ok = False

    nodes = os.environ.get("NODES", "").strip()
    if nodes:
        try:
            nodes_value = int(nodes)
        except ValueError:
            error(f"NODES must be an integer, got {nodes!r}")
            ok = False
        else:
            if nodes_value <= 0:
                error("NODES must be positive")
                ok = False

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
