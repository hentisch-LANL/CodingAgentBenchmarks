"""Task-group installation for ABC Bench.

``abc-bench install`` materializes task groups into the installed root
(``~/.abc-bench/tasks/<group>/``) so they can be referenced by name without a
filesystem path. Sources may be:

* a task-group directory (contains ``group.yaml``),
* a directory that *contains* task groups,
* a squashfs (``.sqfs``) image of a task group, or
* a ``.tar.gz`` / ``.tgz`` archive of a task group.

The tiny bundled ``smoke_test`` group ships inside the package and is
auto-installed on first use so ABC Bench works out of the box.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

import yaml

from . import squashfs
from ._progress import copytree_with_progress
from .groups import installed_tasks_root

#: Group id for the bundled sanity-check benchmarks.
BUNDLED_SMOKE_TEST = "smoke_test"


class InstallError(RuntimeError):
    """Raised for any task-group install failure."""


def _read_group_id(group_dir: Path) -> str | None:
    group_file = group_dir / "group.yaml"
    if not group_file.is_file():
        return None
    try:
        with open(group_file) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    group_id = data.get("id")
    return str(group_id) if group_id else None


def _install_group_dir(group_dir: Path, dest_root: Path, force: bool) -> str:
    """Copy a single task-group directory into ``dest_root``."""
    group_id = _read_group_id(group_dir)
    if group_id is None:
        raise InstallError(f"not a task group (missing/invalid group.yaml): {group_dir}")
    dest = dest_root / group_id
    if dest.exists():
        if not force:
            raise InstallError(
                f"task group {group_id!r} already installed at {dest}; use --force to overwrite"
            )
        shutil.rmtree(dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    copytree_with_progress(group_dir, dest, desc=f"Installing {group_id}")
    return group_id


def _iter_group_dirs(root: Path) -> list[Path]:
    """Return group dirs directly under ``root`` (or ``[root]`` if it is one)."""
    if (root / "group.yaml").is_file():
        return [root]
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / "group.yaml").is_file()
    ]


def install_source(source: Path, dest_root: Path | None = None, force: bool = False) -> list[str]:
    """Install task group(s) from ``source`` into the installed root.

    Returns the list of installed group ids.
    """
    source = Path(source).expanduser()
    if dest_root is None:
        dest_root = installed_tasks_root()

    if not source.exists():
        raise InstallError(f"source not found: {source}")

    if squashfs.is_squashfs_artifact(source):
        materialized = squashfs.materialize_groups(source)
        return [_install_group_dir(gd, dest_root, force) for gd in materialized]

    if source.is_file() and (source.name.endswith(".tar.gz") or source.name.endswith(".tgz")):
        with tempfile.TemporaryDirectory(prefix="abc-bench-install.") as tmp:
            tmp_path = Path(tmp)
            with tarfile.open(source, "r:gz") as tar:
                _safe_extract(tar, tmp_path)
            group_dirs = _iter_group_dirs(tmp_path)
            if not group_dirs:
                raise InstallError(f"archive contains no task group: {source}")
            return [_install_group_dir(gd, dest_root, force) for gd in group_dirs]

    if source.is_dir():
        group_dirs = _iter_group_dirs(source)
        if not group_dirs:
            raise InstallError(f"directory contains no task group: {source}")
        return [_install_group_dir(gd, dest_root, force) for gd in group_dirs]

    raise InstallError(f"unsupported install source (expected dir, .sqfs, or .tar.gz): {source}")


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a tar archive, rejecting paths that escape ``dest``."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise InstallError(f"unsafe path in archive: {member.name}")
    tar.extractall(dest)


def bundled_smoke_test_dir() -> Path | None:
    """Return the bundled ``smoke_test`` group dir, if shipped with the package."""
    import importlib.resources as resources

    try:
        candidate = resources.files("abc_bench") / "data" / "tasks" / BUNDLED_SMOKE_TEST
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        candidate = None
    if candidate is not None and candidate.is_dir():
        with resources.as_file(candidate) as path:
            if (path / "group.yaml").is_file():
                return Path(path)

    # Editable/checkout fallback: tasks/smoke_test relative to the repo root.
    repo_candidate = Path(__file__).resolve().parent.parent / "tasks" / BUNDLED_SMOKE_TEST
    if (repo_candidate / "group.yaml").is_file():
        return repo_candidate
    return None


def ensure_smoke_test_installed(dest_root: Path | None = None) -> str | None:
    """Auto-install the bundled ``smoke_test`` group if nothing is installed yet.

    Returns the installed group id, or ``None`` if there was nothing to seed
    (either the installed root already has groups, or no bundled group exists).
    """
    if dest_root is None:
        dest_root = installed_tasks_root()

    if dest_root.is_dir() and any(
        child.is_dir() and (child / "group.yaml").is_file()
        for child in dest_root.iterdir()
    ):
        return None

    smoke_dir = bundled_smoke_test_dir()
    if smoke_dir is None:
        return None
    return _install_group_dir(smoke_dir, dest_root, force=False)
