from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench.groups import GroupResolver
from abc_bench.task import Task


def _resolver(root: Path, tmp_path: Path) -> GroupResolver:
    """Build a resolver rooted at ``root`` with an empty installed root.

    The empty (nonexistent) installed root prevents discovery from picking up
    the developer's real ``~/.abc-bench/tasks``.
    """
    return GroupResolver(
        task_path=str(root),
        installed_root=tmp_path / "empty-installed",
    )


def _write_group_yaml(group_dir: Path, group_id: str = "demo") -> None:
    (group_dir / "group.yaml").write_text(
        f"id: {group_id}\n"
        f"name: {group_id.title()}\n"
        f"description: {group_id.title()} task group\n"
    )


def _write_valid_task(group_dir: Path, task_id: str) -> None:
    task_dir = group_dir / task_id
    (task_dir / "workspace").mkdir(parents=True)
    (task_dir / "verification").mkdir()
    (task_dir / "build_env.sh").write_text("#!/usr/bin/env bash\n")
    (task_dir / "prompt.md").write_text("# Demo task\n")
    (task_dir / "verification" / "verify_solution.sh").write_text("#!/usr/bin/env bash\n")


def test_list_tasks_skips_invalid_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    group_dir = root / "demo"
    group_dir.mkdir(parents=True)
    _write_group_yaml(group_dir)
    _write_valid_task(group_dir, "good-task")

    # This mimics a source corpus directory that lives under the task group but
    # is not itself a runnable ABC Bench task.
    (group_dir / "linpack").mkdir()

    resolver = _resolver(root, tmp_path)
    assert Task.list_tasks(resolver) == ["demo/good-task"]
    assert Task.list_tasks_by_group(resolver) == {"demo": ["good-task"]}


def test_explicit_invalid_task_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    group_dir = root / "demo"
    group_dir.mkdir(parents=True)
    _write_group_yaml(group_dir)
    (group_dir / "linpack").mkdir()

    resolver = _resolver(root, tmp_path)
    with pytest.raises(ValueError, match="not a valid task"):
        Task("demo/linpack", resolver)


def test_match_task_refs_matches_canonical_refs_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    demo_dir = root / "demo"
    demo_dir.mkdir(parents=True)
    _write_group_yaml(demo_dir)
    _write_valid_task(demo_dir, "good-task")
    _write_valid_task(demo_dir, "other-task")

    alt_dir = root / "alt"
    alt_dir.mkdir(parents=True)
    _write_group_yaml(alt_dir, group_id="alt")
    _write_valid_task(alt_dir, "good-task")

    resolver = _resolver(root, tmp_path)
    matched, unmatched = Task.match_task_refs(["demo/*", "*/good-*", "missing/*"], resolver)

    assert matched == ["alt/good-task", "demo/good-task", "demo/other-task"]
    assert unmatched == ["missing/*"]


def test_match_task_refs_returns_empty_when_nothing_matches(tmp_path: Path) -> None:
    root = tmp_path / "root"
    group_dir = root / "demo"
    group_dir.mkdir(parents=True)
    _write_group_yaml(group_dir)
    _write_valid_task(group_dir, "good-task")

    resolver = _resolver(root, tmp_path)
    matched, unmatched = Task.match_task_refs(["other/*"], resolver)

    assert matched == []
    assert unmatched == ["other/*"]


def test_group_prompt_file_absent_by_default(tmp_path: Path) -> None:
    root = tmp_path / "root"
    group_dir = root / "demo"
    group_dir.mkdir(parents=True)
    _write_group_yaml(group_dir)
    _write_valid_task(group_dir, "good-task")

    resolver = _resolver(root, tmp_path)
    task = Task("demo/good-task", resolver)

    assert task.group_prompt_file is None


def test_group_prompt_file_detected_when_present(tmp_path: Path) -> None:
    root = tmp_path / "root"
    group_dir = root / "demo"
    group_dir.mkdir(parents=True)
    _write_group_yaml(group_dir)
    _write_valid_task(group_dir, "good-task")
    (group_dir / "group_prompt.md").write_text("# Group instructions\n")

    resolver = _resolver(root, tmp_path)
    task = Task("demo/good-task", resolver)

    assert task.group_prompt_file == (group_dir / "group_prompt.md").resolve()
