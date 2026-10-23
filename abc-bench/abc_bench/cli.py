#!/usr/bin/env python3
"""ABC Bench command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from automaton import Agent
from .config import DEFAULT_CONFIG_PATH, Config, initialize_user_config
from .container import ContainerManager
from .install import ensure_smoke_test_installed, install_source, InstallError
from .runner import BenchmarkRunner, _safe_path_segment
from .task import Task


def _resolver(config: Config, args: argparse.Namespace):
    """Build a task-group resolver from config and optional CLI --task-dir."""
    task_dir = getattr(args, "task_dir", None)
    ensure_smoke_test_installed()
    return config.group_resolver(task_dir=task_dir)


def _pool_checkpoint_root(config: Config, results_dir: Path | None, agent_id: str) -> Path:
    """Return the pool checkpoint root for a batch (holds only manifest.jsonl).

    Result artifacts live in the organized per-attempt archive; the pool's
    checkpoint root is a small sibling that records the batch manifest. It is
    keyed by agent so concurrent batches for different agents don't collide.
    """
    import os

    if results_dir is None:
        env_results_dir = os.environ.get("ABC_BENCH_RESULTS_DIR")
        results_dir = Path(env_results_dir) if env_results_dir else None
    configured = Path(results_dir) if results_dir is not None else config.results_dir
    root = configured.resolve() if configured.is_absolute() else (Path.cwd() / configured).resolve()
    return root / ".pool" / _safe_path_segment(agent_id)


def parse_memory_limit(value: str) -> int:
    """Parse a memory size like 4096M or 8G into bytes."""
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("memory limit cannot be empty")

    suffix_multipliers = {
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
    }
    number = text
    multiplier = 1
    for suffix, suffix_multiplier in sorted(suffix_multipliers.items(), key=lambda item: len(item[0]), reverse=True):
        if text.lower().endswith(suffix):
            number = text[: -len(suffix)]
            multiplier = suffix_multiplier
            break

    try:
        bytes_value = int(float(number.strip()) * multiplier)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid memory limit: {value}") from exc
    if bytes_value <= 0:
        raise argparse.ArgumentTypeError("memory limit must be positive")
    return bytes_value


def _container_kind_label(kind: str) -> str:
    if kind == "base":
        return "base"
    if kind == "harness":
        return "harness-layer"
    return "runtime"


def _print_container_detail(info) -> None:
    status = "BUILT" if info.exists else "NOT BUILT"
    print(f"Container: {info.container_id}")
    print(f"  Status: {status}")
    print(f"  Kind: {_container_kind_label(info.kind)}")
    print(f"  Canonical ref: {info.canonical_ref}")
    if len(info.aliases) > 1:
        print(f"  Aliases: {', '.join(alias for alias in info.aliases if alias != info.container_id)}")
    print(f"  Path: {info.path}")
    if info.exists:
        print(f"  Size: {info.size_human()}")
        if info.build_time:
            print(f"  Built: {info.build_time.strftime('%Y-%m-%d %H:%M:%S')}")


def cmd_list_tasks(args: argparse.Namespace) -> int:
    """List available tasks, optionally filtered to one task group."""
    config = Config(args.config) if args.config else Config()
    resolver = _resolver(config, args)

    if args.task_group is not None:
        groups = Task.list_task_groups(resolver)
        if args.task_group not in groups:
            print(f"Task group not found: {args.task_group}", file=sys.stderr)
            return 1
        task_ids = Task.list_tasks_by_group(resolver).get(args.task_group, [])
        if not task_ids:
            print(f"No tasks found in task group: {args.task_group}")
            return 1
        print(f"Available tasks in {args.task_group}:")
        for task_id in task_ids:
            print(f"  - {task_id}")
            print(f"    canonical: {args.task_group}/{task_id}")
        return 0

    tasks_by_group = Task.list_tasks_by_group(resolver)
    tasks = [task for task_list in tasks_by_group.values() for task in task_list]
    if not tasks:
        print("No tasks found")
        return 1

    print("Available tasks:")
    for group_id, task_ids in tasks_by_group.items():
        print(f"  {group_id}")
        for task_id in task_ids:
            print(f"    - {task_id}")
            print(f"      canonical: {group_id}/{task_id}")
    return 0


def cmd_list_task_groups(args: argparse.Namespace) -> int:
    """List all available task groups."""
    config = Config(args.config) if args.config else Config()
    resolver = _resolver(config, args)
    groups = Task.list_task_groups(resolver)
    if not groups:
        print("No task groups found")
        return 1

    print("Available task groups:")
    for group_id in groups:
        print(f"  - {group_id}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Install task group(s) into ~/.abc-bench/tasks."""
    config = Config(args.config) if args.config else Config()
    dest_root = config.installed_root

    if not args.sources:
        # No explicit source: seed the bundled smoke_test if nothing installed.
        seeded = ensure_smoke_test_installed(dest_root)
        if seeded:
            print(f"Installed bundled task group: {seeded} -> {dest_root / seeded}")
        else:
            print("Nothing to install (already populated or no bundled groups).")
            print("Pass a directory, .sqfs, or .tar.gz to install a task group.")
        return 0

    installed: list[str] = []
    for source in args.sources:
        try:
            group_ids = install_source(Path(source), dest_root, force=args.force)
        except InstallError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        for group_id in group_ids:
            print(f"Installed task group: {group_id} -> {dest_root / group_id}")
            installed.append(group_id)
    print(f"Installed {len(installed)} task group(s)")
    return 0


def cmd_list_containers(args: argparse.Namespace) -> int:
    """List containers or show details for an exact container ref."""
    config = Config(args.config) if args.config else Config()
    resolver = _resolver(config, args)
    manager = ContainerManager(config.package_root, resolver)

    if args.container is None:
        containers = manager.list_containers(only_built=args.built_only)
        if not containers:
            print("No containers found")
            return 1

        print("Available containers:")
        for container_id, info in containers.items():
            status = "BUILT" if info.exists else "NOT BUILT"
            print(f"  {container_id}")
            print(f"    Status: {status}")
            print(f"    Kind: {_container_kind_label(info.kind)}")
            print(f"    Canonical: {info.canonical_ref}")
        return 0

    matches = manager.find_container_infos(args.container, only_built=args.built_only)
    if not matches:
        print(f"No containers match: {args.container}")
        return 1

    if len(matches) == 1 and any(args.container == alias for alias in matches[0].aliases):
        _print_container_detail(matches[0])
        return 0

    print(f"Matching containers for {args.container}:")
    for info in matches:
        status = "BUILT" if info.exists else "NOT BUILT"
        print(f"  {info.container_id}")
        print(f"    Status: {status}")
        print(f"    Kind: {_container_kind_label(info.kind)}")
        print(f"    Canonical: {info.canonical_ref}")
    return 0


def cmd_list_models(args: argparse.Namespace) -> int:
    """List configured models."""
    try:
        config = Config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not config.models:
        print("No models found in config")
        return 1

    print(f"Models from: {config.config_path}")
    print()
    for name, model_config in sorted(config.models.items()):
        print(f"  {name}")
        for key, value in sorted(model_config.items()):
            normalized_key = key.lower()
            if any(secret_word in normalized_key for secret_word in ("api_key", "token", "secret", "password")):
                continue
            print(f"    {key}: {value}")
        print()

    return 0


def cmd_list_agents(args: argparse.Namespace) -> int:
    """List configured agents."""
    try:
        config = Config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not config.agents:
        print("No agents found in config")
        return 1

    print(f"Agents from: {config.config_path}")
    print()
    for name in Agent.list_agents(config):
        agent = Agent(name, config)
        print(f"  {name}")
        print(f"    harness: {agent.harness_id}")
        print(f"    models: {', '.join(agent.model_names)}")
        print(f"    harness_config: {agent.harness_config}")
        print()
    return 0


def cmd_build_container(args: argparse.Namespace) -> int:
    """Build one or more containers."""
    config = Config(args.config) if args.config else Config()
    resolver = _resolver(config, args)
    manager = ContainerManager(config.package_root, resolver)

    if args.all:
        container_refs = [info.container_id for info in manager.list_containers().values()]
        if not container_refs:
            print("No containers found")
            return 1

        print(f"Building {len(container_refs)} container(s)...")
        print()

        failed = []
        for container_ref in container_refs:
            print(f"{'=' * 60}")
            print(f"Building container: {container_ref}")
            print(f"{'=' * 60}")
            try:
                manager.build_container(container_ref, force=args.rebuild, verbose=True)
            except Exception as e:
                print(f"Failed to build {container_ref}: {e}")
                failed.append(container_ref)
            print()

        if failed:
            print(f"Failed to build {len(failed)} container(s): {', '.join(failed)}")
            return 1

        print("Successfully built all requested containers")
        return 0

    if not args.container:
        print("Error: Must specify a container ref or --all", file=sys.stderr)
        return 1

    try:
        manager.build_container(args.container, force=args.rebuild, verbose=True)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def cmd_delete_container(args: argparse.Namespace) -> int:
    """Delete one or more built containers."""
    config = Config(args.config) if args.config else Config()
    resolver = _resolver(config, args)
    manager = ContainerManager(config.package_root, resolver)

    if args.all:
        containers = manager.list_containers(only_built=True)
        if not containers:
            print("No built containers found")
            return 0

        if not args.force:
            print(f"About to delete {len(containers)} built container(s):")
            for container_id in sorted(containers.keys()):
                info = containers[container_id]
                print(f"  - {container_id} ({info.size_human()})")
            print()
            response = input("Continue? [y/N] ")
            if response.lower() not in ("y", "yes"):
                print("Cancelled")
                return 0

        for container_id in sorted(containers.keys()):
            manager.delete_container(container_id, verbose=True)
            print()

        print(f"Deleted {len(containers)} built container(s)")
        return 0

    if not args.container:
        print("Error: Must specify a container ref or --all", file=sys.stderr)
        return 1

    try:
        deleted = manager.delete_container(args.container, verbose=True)
        return 0 if deleted else 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_clear_cache(args: argparse.Namespace) -> int:
    """Clear all ABC Bench temporary build and runtime cache."""
    config = Config(args.config) if args.config else Config()
    resolver = _resolver(config, args)
    manager = ContainerManager(config.package_root, resolver)
    if not manager.cache_root.exists():
        print(f"ABC Bench cache does not exist: {manager.cache_root}")
        return 0

    if not args.force:
        print("About to remove all ABC Bench cache data under:")
        print(f"  {manager.cache_root}")
        print()
        print("This includes all built container artifacts and Charliecloud builder storage.")
        response = input("Continue? [y/N] ")
        if response.lower() not in ("y", "yes"):
            print("Cancelled")
            return 0

    manager.clear_cache(verbose=True)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a benchmark."""
    if args.results_dir is not None and args.output_dir is not None:
        print("Error: --results-dir and --output-dir are mutually exclusive", file=sys.stderr)
        return 1

    if args.harness_memory_limit is not None and args.no_harness_memory_limit:
        print("Error: --harness-memory-limit and --no-harness-memory-limit are mutually exclusive", file=sys.stderr)
        return 1
    if (
        args.harness_memory_limit is None
        and not args.no_harness_memory_limit
        and (args.harness_memory_limit_fraction <= 0 or args.harness_memory_limit_fraction > 1)
    ):
        print("Error: --harness-memory-limit-fraction must be greater than 0 and at most 1", file=sys.stderr)
        return 1

    try:
        config = Config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    resolver = _resolver(config, args)
    manager = ContainerManager(config.package_root, resolver)
    available, error_msg = manager.check_charliecloud_available()
    if not available:
        print(f"Error: {error_msg}", file=sys.stderr)
        return 1

    try:
        resolved_task = Task(args.task, resolver, task_group=args.task_group)
        agent = Agent(args.agent, config)
        runtime_ref = manager.runtime_container_ref(agent.harness_id, resolved_task.group_id)
        if not manager.container_exists(runtime_ref):
            container_path = manager.get_container_path(runtime_ref)
            print(f"Error: Container runtime for '{agent.harness_id}/{resolved_task.group_id}' not found", file=sys.stderr)
            print(f"Expected path: {container_path}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Build it first with:", file=sys.stderr)
            print(f"  abc-bench build-container {agent.harness_id}/{resolved_task.group_id}", file=sys.stderr)
            return 1

        runner = BenchmarkRunner(
            config,
            resolver,
            dry_run=args.dry_run,
            stream_output=not args.no_stream_output,
            clean_env=not args.no_clean_env,
            harness_memory_limit_bytes=args.harness_memory_limit,
            harness_memory_limit_fraction=None if args.no_harness_memory_limit else args.harness_memory_limit_fraction,
            sandbox_network=not args.no_sandbox_network,
        )
        result = runner.run(
            task_id=args.task,
            agent_id=args.agent,
            task_group=args.task_group,
            timeout=args.timeout,
            results_dir=args.results_dir,
            output_dir=args.output_dir,
        )

        return 1 if result.status == "ERROR" else 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def cmd_run_tasks(args: argparse.Namespace) -> int:
    """Run multiple benchmarks selected by canonical task-ref globs."""
    if args.harness_memory_limit is not None and args.no_harness_memory_limit:
        print("Error: --harness-memory-limit and --no-harness-memory-limit are mutually exclusive", file=sys.stderr)
        return 1
    if (
        args.harness_memory_limit is None
        and not args.no_harness_memory_limit
        and (args.harness_memory_limit_fraction <= 0 or args.harness_memory_limit_fraction > 1)
    ):
        print("Error: --harness-memory-limit-fraction must be greater than 0 and at most 1", file=sys.stderr)
        return 1

    try:
        config = Config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    resolver = _resolver(config, args)
    manager = ContainerManager(config.package_root, resolver)
    available, error_msg = manager.check_charliecloud_available()
    if not available:
        print(f"Error: {error_msg}", file=sys.stderr)
        return 1

    try:
        matched_tasks, unmatched_patterns = Task.match_task_refs(args.patterns, resolver)
        if not matched_tasks:
            print(
                "Error: no tasks matched the supplied canonical task-ref pattern(s): "
                + ", ".join(args.patterns),
                file=sys.stderr,
            )
            return 1

        if unmatched_patterns:
            print(
                "Warning: the following pattern(s) matched no canonical task refs: "
                + ", ".join(unmatched_patterns),
                file=sys.stderr,
            )

        if not matched_tasks:
            print("No tasks matched the supplied pattern(s)")
            return 0

        print(f"Matched {len(matched_tasks)} task(s):")
        for task_ref in matched_tasks:
            print(f"  - {task_ref}")
        print()

        attempts = args.attempts
        if attempts < 1:
            print("Error: --attempts must be >= 1", file=sys.stderr)
            return 1

        use_mpi = args.mpi
        rank = 0
        size = 1
        if use_mpi:
            available, error_msg = manager.check_mpi_available()
            if not available:
                print(f"Error: {error_msg}", file=sys.stderr)
                return 1
            from mpi4py import MPI

            rank = MPI.COMM_WORLD.Get_rank()
            size = MPI.COMM_WORLD.Get_size()

        # Every rank builds the runtime containers it needs into its (node-local)
        # image store, then all ranks synchronize before any dispatch begins.
        from .pool_runner import BenchmarkPoolRunner, build_attempt_jobs, build_required_containers

        try:
            build_required_containers(config, resolver, args.agent, matched_tasks)
        except Exception as e:
            print(f"Error building runtime container(s): {e}", file=sys.stderr)
            if use_mpi:
                # Do not leave peers waiting at the barrier on a build failure.
                from mpi4py import MPI

                MPI.COMM_WORLD.Abort(1)
            return 1

        if use_mpi:
            from mpi4py import MPI

            MPI.COMM_WORLD.Barrier()

        runner_kwargs = dict(
            dry_run=args.dry_run,
            stream_output=not args.no_stream_output,
            clean_env=not args.no_clean_env,
            harness_memory_limit_bytes=args.harness_memory_limit,
            harness_memory_limit_fraction=None if args.no_harness_memory_limit else args.harness_memory_limit_fraction,
            sandbox_network=not args.no_sandbox_network,
        )
        pool_runner = BenchmarkPoolRunner(
            config,
            resolver,
            agent_id=args.agent,
            results_dir=args.results_dir,
            timeout=args.timeout,
            runner_kwargs=runner_kwargs,
        )
        jobs = build_attempt_jobs(matched_tasks, attempts)
        checkpoint_root = _pool_checkpoint_root(config, args.results_dir, args.agent)
        pool = pool_runner.make_pool(jobs, checkpoint_root, use_mpi=use_mpi)

        results = pool.run()

        # Only rank 0 (or the serial process) reports the batch summary.
        if rank != 0:
            return 0

        conclusive = sum(1 for r in results if r.success)
        pass_count = sum(1 for r in results if r.summary.get("status") == "PASS")
        fail_count = sum(1 for r in results if r.summary.get("status") == "FAIL")
        inconclusive = len(results) - conclusive
        not_run = len(jobs) - len(results)

        print("Batch summary:")
        print(f"  Tasks: {len(matched_tasks)}  Attempts/task: {attempts}  Jobs: {len(jobs)}")
        if use_mpi:
            print(f"  MPI ranks: {size}")
        print(f"  Completed (conclusive): {conclusive}")
        print(f"  PASS: {pass_count}")
        print(f"  FAIL: {fail_count}")
        if inconclusive:
            print(f"  Inconclusive (will retry on resume): {inconclusive}")
        if not_run:
            print(f"  Not run: {not_run}")

        return 1 if inconclusive or not_run else 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def main() -> int:
    """Main CLI entrypoint."""
    initialize_user_config()

    parser = argparse.ArgumentParser(
        prog="abc-bench",
        description="ABC Bench: Autonomous benchmark suite for scientific computing",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    parser_list_tasks = subparsers.add_parser("list-tasks", help="List available tasks")
    parser_list_tasks.add_argument("task_group", nargs="?", help="Optional task group filter")
    parser_list_tasks.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_list_tasks.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_list_tasks.set_defaults(func=cmd_list_tasks)

    parser_list_task_groups = subparsers.add_parser("list-task-groups", help="List all available task groups")
    parser_list_task_groups.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_list_task_groups.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_list_task_groups.set_defaults(func=cmd_list_task_groups)

    parser_install = subparsers.add_parser(
        "install", help="Install task group(s) into ~/.abc-bench/tasks"
    )
    parser_install.add_argument(
        "sources",
        nargs="*",
        help="Task-group source(s): a directory, .sqfs image, or .tar.gz archive. "
        "With no source, seeds the bundled smoke_test if nothing is installed.",
    )
    parser_install.add_argument(
        "--force", "-f", action="store_true", help="Overwrite already-installed groups"
    )
    parser_install.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_install.set_defaults(func=cmd_install)

    parser_list_containers = subparsers.add_parser("list-containers", help="List containers or show container details")
    parser_list_containers.add_argument("container", nargs="?", help="Container ref or glob pattern")
    parser_list_containers.add_argument(
        "--built-only",
        action="store_true",
        help="Show only built containers",
    )
    parser_list_containers.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_list_containers.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_list_containers.set_defaults(func=cmd_list_containers)

    parser_list_models = subparsers.add_parser("list-models", help="List configured models")
    parser_list_models.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_list_models.set_defaults(func=cmd_list_models)

    parser_list_agents = subparsers.add_parser("list-agents", help="List configured agents")
    parser_list_agents.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_list_agents.set_defaults(func=cmd_list_agents)

    parser_build_container = subparsers.add_parser("build-container", help="Build a container artifact")
    parser_build_container.add_argument("container", nargs="?", help="Container ref to build")
    parser_build_container.add_argument("--all", action="store_true", help="Build all containers")
    parser_build_container.add_argument("--rebuild", action="store_true", help="Force rebuild even if already built")
    parser_build_container.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser_build_container.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_build_container.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_build_container.set_defaults(func=cmd_build_container)

    parser_delete_container = subparsers.add_parser("delete-container", help="Delete a built container artifact")
    parser_delete_container.add_argument("container", nargs="?", help="Container ref to delete")
    parser_delete_container.add_argument("--all", action="store_true", help="Delete all built containers")
    parser_delete_container.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")
    parser_delete_container.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_delete_container.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_delete_container.set_defaults(func=cmd_delete_container)

    parser_clear_cache = subparsers.add_parser("clear-cache", help="Clear built containers and temporary build cache")
    parser_clear_cache.add_argument("--force", "-f", action="store_true", help="Skip confirmation prompt")
    parser_clear_cache.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_clear_cache.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_clear_cache.set_defaults(func=cmd_clear_cache)

    parser_run = subparsers.add_parser("run", help="Run a benchmark")
    parser_run.add_argument("--task", required=True, help="Task ID to run")
    parser_run.add_argument("--agent", required=True, help="Agent ID to use")
    parser_run.add_argument("--task-group", default=None, help="Explicit task group when --task is not canonical")
    parser_run.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_run.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_run.add_argument("--timeout", type=int, default=None, help="Timeout in seconds (default: from config)")
    parser_run.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="Organized archive root for many benchmark results (or ABC_BENCH_RESULTS_DIR)",
    )
    parser_run.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Standalone directory for this single benchmark result (or ABC_BENCH_OUTPUT_DIR)",
    )
    parser_run.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser_run.add_argument("--no-stream-output", action="store_true", help="Write container output only to log files")
    parser_run.add_argument(
        "--harness-memory-limit",
        type=parse_memory_limit,
        default=None,
        metavar="SIZE",
        help="Fixed memory cap for the harness process tree, e.g. 8G or 4096M (default: 90%% of available memory)",
    )
    parser_run.add_argument(
        "--harness-memory-limit-fraction",
        type=float,
        default=0.9,
        help="Fraction of available memory to use as harness cap when --harness-memory-limit is not set (default: 0.9)",
    )
    parser_run.add_argument("--no-harness-memory-limit", action="store_true", help="Disable the harness memory cap")
    parser_run.add_argument(
        "--no-clean-env",
        action="store_true",
        help="Do not remove task environment after verification (useful for debugging)",
    )
    parser_run.add_argument(
        "--no-sandbox-network",
        action="store_true",
        help="Run harness and verification with host networking instead of loopback-only sandboxing",
    )
    parser_run.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser_run.set_defaults(func=cmd_run)

    parser_run_tasks = subparsers.add_parser("run-tasks", help="Run multiple benchmarks from canonical task-ref globs")
    parser_run_tasks.add_argument("patterns", nargs="+", help="Canonical task-ref glob pattern(s), e.g. 'smoke_test/*'")
    parser_run_tasks.add_argument("--agent", required=True, help="Agent ID to use")
    parser_run_tasks.add_argument(
        "--task-dir",
        type=Path,
        default=None,
        help="Directory containing task groups (highest resolution priority)",
    )
    parser_run_tasks.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser_run_tasks.add_argument("--timeout", type=int, default=None, help="Timeout in seconds (default: from config)")
    parser_run_tasks.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="Organized archive root for many benchmark results (or ABC_BENCH_RESULTS_DIR)",
    )
    parser_run_tasks.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser_run_tasks.add_argument(
        "--no-stream-output", action="store_true", help="Write container output only to log files"
    )
    parser_run_tasks.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="Number of attempts to run per task (default: 1). Already-completed "
        "attempts are kept; a batch never exceeds this many attempts per task.",
    )
    parser_run_tasks.add_argument(
        "--mpi",
        action="store_true",
        help="Distribute the batch across MPI ranks (requires launching under "
        "an MPI launcher, e.g. srun/mpiexec, and the automaton[mpi] extra). "
        "Rank 0 both dispatches and runs jobs.",
    )
    parser_run_tasks.add_argument(
        "--harness-memory-limit",
        type=parse_memory_limit,
        default=None,
        metavar="SIZE",
        help="Fixed memory cap for the harness process tree, e.g. 8G or 4096M (default: 90%% of available memory)",
    )
    parser_run_tasks.add_argument(
        "--harness-memory-limit-fraction",
        type=float,
        default=0.9,
        help="Fraction of available memory to use as harness cap when --harness-memory-limit is not set (default: 0.9)",
    )
    parser_run_tasks.add_argument(
        "--no-harness-memory-limit", action="store_true", help="Disable the harness memory cap"
    )
    parser_run_tasks.add_argument(
        "--no-clean-env",
        action="store_true",
        help="Do not remove task environment after verification (useful for debugging)",
    )
    parser_run_tasks.add_argument(
        "--no-sandbox-network",
        action="store_true",
        help="Run harness and verification with host networking instead of loopback-only sandboxing",
    )
    parser_run_tasks.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser_run_tasks.set_defaults(func=cmd_run_tasks)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
