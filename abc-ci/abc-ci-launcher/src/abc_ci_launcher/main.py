"""Command-line interface for abc-ci-launcher."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_config
from .gitlab import GitLabTriggerClient, TriggerRequest
from .validate import (
    get_available_agents,
    is_glob_pattern,
    resolve_agents,
    resolve_task_ref,
    validate_agent,
    validate_task,
    validate_task_group,
)


def positive_int(value: str) -> int:
    """Parse a strictly positive integer for CLI arguments."""

    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def add_common_config_args(parser: argparse.ArgumentParser) -> None:
    """Register launcher config path arguments on a parser."""

    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"launcher config path (default: {DEFAULT_CONFIG_PATH})",
    )


def add_batch_args(parser: argparse.ArgumentParser) -> None:
    """Register attempt/node batch arguments shared by trigger subcommands."""

    parser.add_argument(
        "--attempts",
        type=positive_int,
        default=1,
        help="attempts to run per task (default: 1); already-completed attempts "
        "are kept and the batch never exceeds this many attempts per task",
    )
    parser.add_argument(
        "--nodes",
        type=positive_int,
        default=None,
        help="number of site-allocated nodes for the batch (one rank per "
        "node); more than one node distributes the batch across the cluster via MPI",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="target the dev branch and archive to the dev results dir "
        "(sends DEV=1 and triggers on the configured gitlab.dev_ref)",
    )


def load_checked_config(path: Path | None, require_token: bool = True):
    """Load config and exit with user-facing errors if invalid."""

    config = load_config(path)
    errors = config.validate(require_token=require_token)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
    return config


def print_trigger_result(request: TriggerRequest, result: dict, json_output: bool = False) -> None:
    """Render trigger results in either text or JSON form."""

    secret_names = set(request.key_env_names or [])
    safe_variables = {
        name: "<hidden>" if name in secret_names or "KEY" in name.upper() or "SECRET" in name.upper() else value
        for name, value in request.variables().items()
    }
    if json_output:
        print(json.dumps({"request": safe_variables, "result": result}, indent=2, sort_keys=True))
        return

    target_name = request.task_group if request.task_group is not None else request.task
    target = f"{target_name} / {request.agent}"
    if result.get("success"):
        if result.get("dry_run"):
            print(f"DRY-RUN {target}")
            print(f"  URL: {result['url']}")
            print(f"  Variables: {safe_variables}")
        else:
            message = f"TRIGGERED {target}"
            if result.get("web_url"):
                message += f" -> {result['web_url']}"
            print(message)
    else:
        print(f"FAILED {target}: {result.get('error')}", file=sys.stderr)


def resolve_agent_list(agent_pattern: str, config_path: str | None, force: bool) -> list[str] | None:
    """Resolve an agent pattern to a list of agent ids.

    Glob patterns (e.g. ``example-*``) are expanded against the available
    agents from the abc-bench config.  Non-glob patterns are validated as
    exact agent ids, honouring ``force`` to skip confirmation.

    Returns the list of agent ids, or ``None`` when validation failed.
    """

    if is_glob_pattern(agent_pattern):
        agents = resolve_agents(agent_pattern, config_path=config_path)
        if not agents:
            available = get_available_agents(config_path)
            print(f"ERROR: No agents matched pattern '{agent_pattern}'", file=sys.stderr)
            print(f"Available agents: {', '.join(available) if available else '(none)'}", file=sys.stderr)
            return None
        return agents

    if not force:
        if not validate_agent(agent_pattern, config_path=config_path, force=force):
            return None
    return [agent_pattern]


def trigger_for_requests(
    config,
    requests: list[TriggerRequest],
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    """Trigger pipelines for a list of requests and print consolidated results.

    Returns ``0`` when every request succeeded, ``1`` otherwise.
    """

    client = GitLabTriggerClient(config)
    paired: list[tuple[TriggerRequest, dict]] = []
    all_success = True

    for request in requests:
        result = client.trigger(request, dry_run=dry_run)
        paired.append((request, result))
        if not result.get("success"):
            all_success = False

    if json_output:
        output = [
            {"request": req.variables(), "result": res}
            for req, res in paired
        ]
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        for req, res in paired:
            print_trigger_result(req, res, json_output=False)

    return 0 if all_success else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Validate local launcher configuration and print a summary."""

    config = load_config(args.config)
    errors = config.validate(require_token=not args.no_token)
    print(f"Config: {config.path}")
    print(f"GitLab base URL: {config.gitlab_base_url or '<missing>'}")
    print(f"GitLab project ID: {config.gitlab_project_id or '<missing>'}")
    print(f"GitLab ref: {config.gitlab_ref or '<missing>'}")
    print(f"Trigger token env: {config.trigger_token_env or '<missing>'}")
    print(f"Default agent: {config.default_agent or '<none>'}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Config looks usable.")
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """Validate CLI input and trigger a benchmark pipeline."""

    config = load_checked_config(args.config, require_token=not args.dry_run)
    selected_agent = args.agent or config.default_agent
    if not selected_agent:
        print("ERROR: --agent is required when no default agent is configured", file=sys.stderr)
        return 2

    if not args.force:
        if not validate_task(args.task, force=args.force):
            return 2

    config_path = args.abc_bench_config if hasattr(args, "abc_bench_config") and args.abc_bench_config else None

    agents = resolve_agent_list(selected_agent, config_path, force=args.force)
    if agents is None:
        return 2

    canonical_task = resolve_task_ref(args.task)

    timeout = args.timeout_seconds if args.timeout_seconds is not None else config.default_timeout_seconds
    key_env_names = args.key_envs if args.key_envs else None

    target_ref = config.gitlab_dev_ref if args.dev else config.gitlab_ref

    requests = [
        TriggerRequest(
            task=canonical_task,
            agent=agent,
            task_group=None,
            timeout_seconds=timeout,
            run_group=args.run_group,
            run_label=args.run_label,
            key_env_names=key_env_names,
            attempts=args.attempts,
            nodes=args.nodes,
            dev=args.dev,
            ref=target_ref,
        )
        for agent in agents
    ]

    return trigger_for_requests(config, requests, dry_run=args.dry_run, json_output=args.json)


def cmd_trigger_group(args: argparse.Namespace) -> int:
    """Validate CLI input and trigger a task-group benchmark pipeline."""

    config = load_checked_config(args.config, require_token=not args.dry_run)
    selected_agent = args.agent or config.default_agent
    if not selected_agent:
        print("ERROR: --agent is required when no default agent is configured", file=sys.stderr)
        return 2

    if not args.force:
        if not validate_task_group(args.task_group, force=args.force):
            return 2

    config_path = args.abc_bench_config if hasattr(args, "abc_bench_config") and args.abc_bench_config else None

    agents = resolve_agent_list(selected_agent, config_path, force=args.force)
    if agents is None:
        return 2

    timeout = args.timeout_seconds if args.timeout_seconds is not None else config.default_timeout_seconds
    key_env_names = args.key_envs if args.key_envs else None

    target_ref = config.gitlab_dev_ref if args.dev else config.gitlab_ref

    requests = [
        TriggerRequest(
            task="",
            task_group=args.task_group,
            agent=agent,
            timeout_seconds=timeout,
            run_group=args.run_group,
            run_label=args.run_label,
            key_env_names=key_env_names,
            attempts=args.attempts,
            nodes=args.nodes,
            dev=args.dev,
            ref=target_ref,
        )
        for agent in agents
    ]

    return trigger_for_requests(config, requests, dry_run=args.dry_run, json_output=args.json)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""

    parser = argparse.ArgumentParser(prog="abc-ci", description="Trigger abc-ci GitLab pipelines")
    subparsers = parser.add_subparsers(dest="command", required=False)

    doctor_parser = subparsers.add_parser("doctor", help="validate launcher configuration")
    add_common_config_args(doctor_parser)
    doctor_parser.add_argument("--no-token", action="store_true", help="do not require the trigger token env var")
    doctor_parser.set_defaults(func=cmd_doctor)

    trigger_parser = subparsers.add_parser("trigger", help="trigger one benchmark pipeline")
    add_common_config_args(trigger_parser)
    trigger_parser.add_argument("--task", required=True, help="abc-bench task id")
    trigger_parser.add_argument("--agent", help="agent id or glob pattern (e.g. 'example-*') from ABC_BENCH_CONFIG")
    trigger_parser.add_argument("--timeout-seconds", type=positive_int, help="per-run timeout override")
    trigger_parser.add_argument("--run-group", help="logical group stored in CI metadata")
    trigger_parser.add_argument("--run-label", help="human-readable label stored in CI metadata")
    trigger_parser.add_argument("--key-envs", nargs="+", help="environment variable names containing API keys to pass to CI")
    trigger_parser.add_argument("--dry-run", action="store_true", help="print the request without triggering GitLab")
    trigger_parser.add_argument("--json", action="store_true", help="print JSON output")
    trigger_parser.add_argument("--force", action="store_true", help="skip validation and confirmation prompts")
    trigger_parser.add_argument("--abc-bench-config", help="path to ABC_BENCH_CONFIG for agent validation (optional)")
    add_batch_args(trigger_parser)
    trigger_parser.set_defaults(func=cmd_trigger)

    trigger_group_parser = subparsers.add_parser("trigger-group", help="trigger one task-group benchmark pipeline")
    add_common_config_args(trigger_group_parser)
    trigger_group_parser.add_argument("--task-group", required=True, help="abc-bench task group id")
    trigger_group_parser.add_argument("--agent", help="agent id or glob pattern (e.g. 'example-*') from ABC_BENCH_CONFIG")
    trigger_group_parser.add_argument("--timeout-seconds", type=positive_int, help="per-run timeout override")
    trigger_group_parser.add_argument("--run-group", help="logical group stored in CI metadata")
    trigger_group_parser.add_argument("--run-label", help="human-readable label stored in CI metadata")
    trigger_group_parser.add_argument(
        "--key-envs", nargs="+", help="environment variable names containing API keys to pass to CI"
    )
    trigger_group_parser.add_argument("--dry-run", action="store_true", help="print the request without triggering GitLab")
    trigger_group_parser.add_argument("--json", action="store_true", help="print JSON output")
    trigger_group_parser.add_argument("--force", action="store_true", help="skip validation and confirmation prompts")
    trigger_group_parser.add_argument(
        "--abc-bench-config", help="path to ABC_BENCH_CONFIG for agent validation (optional)"
    )
    add_batch_args(trigger_group_parser)
    trigger_group_parser.set_defaults(func=cmd_trigger_group)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the `abc-ci` command-line entry point."""

    parser = build_parser()
    args = parser.parse_args(argv)

    # If no subcommand provided, initialize config and show help
    if not args.command:
        load_config()  # This will create config if it doesn't exist
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
