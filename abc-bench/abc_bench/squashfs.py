"""SquashFS support for ABC Bench task groups.

A task group may be supplied either as an ordinary directory or as a squashfs
(``.sqfs``) image of that directory. This module mounts such an image read-only
with ``squashfuse`` and copies its contents out to a temporary directory so the
rest of ABC Bench can treat it exactly like a directory-backed task group.

squashfs is a hard requirement on every platform ABC Bench targets, so this
module is intentionally small: it mounts, copies, and unmounts, and raises a
clear error if ``squashfuse`` is unavailable. There is no directory fallback
and no partial-extraction cleverness.
"""

from __future__ import annotations

import atexit
import contextlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from ._progress import copy_dir_contents_with_progress


#: File suffix that marks a squashfs task-group image.
SQUASHFS_SUFFIX = ".sqfs"

# Temp directories created for materialized squashfs groups, cleaned at exit.
_MATERIALIZED_DIRS: list[Path] = []


class SquashfsError(RuntimeError):
    """Raised for any squashfs mount/copy failure."""


def is_squashfs_artifact(path: Path) -> bool:
    """Return True if ``path`` looks like a squashfs task-group image."""
    return path.is_file() and path.suffix == SQUASHFS_SUFFIX


def _require_squashfuse() -> None:
    if shutil.which("squashfuse") is None:
        raise SquashfsError(
            "squashfuse is required to use squashfs task groups but was not "
            "found on PATH"
        )


@contextlib.contextmanager
def mount_squashfs(artifact: Path) -> Iterator[Path]:
    """Mount a squashfs image read-only, yielding the mount point.

    Uses ``squashfuse`` (a userspace FUSE mount): no root required and nothing
    is expanded to disk. The mount is always torn down on exit.
    """
    artifact = Path(artifact)
    if not artifact.is_file():
        raise SquashfsError(f"squashfs artifact not found: {artifact}")
    _require_squashfuse()

    mount_point = Path(tempfile.mkdtemp(prefix="abc-bench-sqfs-mount."))
    try:
        print(f"Mounting squashfs image {artifact} ...", flush=True)
        result = subprocess.run(
            ["squashfuse", str(artifact), str(mount_point)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise SquashfsError(
                f"failed to mount squashfs image {artifact}: {result.stderr.strip()}"
            )
        try:
            yield mount_point
        finally:
            _unmount(mount_point)
    finally:
        with contextlib.suppress(OSError):
            mount_point.rmdir()


def _unmount(mount_point: Path) -> None:
    """Unmount a squashfuse mount, retrying briefly if the FS is busy."""
    for attempt in range(5):
        result = subprocess.run(
            ["fusermount", "-u", str(mount_point)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5 * (attempt + 1))
    # Last resort: lazy unmount so we do not leak the mount indefinitely.
    subprocess.run(
        ["fusermount", "-uz", str(mount_point)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def materialize_group(artifact: Path) -> Path:
    """Mount a squashfs group image and copy its contents to a temp directory.

    Returns the path to the materialized directory. The directory is registered
    for cleanup at interpreter exit. The image is never fully decompressed to
    disk during the mount; only the copied contents live on disk afterward.

    This is the *single-group* form: the image must contain exactly one task
    group (``group.yaml`` at the root, or a single top-level dir containing it).
    Use :func:`materialize_groups` for images that may pack several groups.
    """
    artifact = Path(artifact)
    dest = Path(tempfile.mkdtemp(prefix="abc-bench-group."))
    _MATERIALIZED_DIRS.append(dest)
    try:
        with mount_squashfs(artifact) as mount_point:
            _copy_group_root(mount_point, dest)
    except Exception:
        with contextlib.suppress(OSError):
            shutil.rmtree(dest, ignore_errors=True)
        with contextlib.suppress(ValueError):
            _MATERIALIZED_DIRS.remove(dest)
        raise
    return dest


def materialize_groups(artifact: Path) -> list[Path]:
    """Mount a squashfs image and copy out *every* task group it contains.

    Handles three packings:

    * ``group.yaml`` at the image root -> one group,
    * a single top-level dir that contains ``group.yaml`` -> one group,
    * a top level of several dirs each containing ``group.yaml`` -> many groups.

    Returns the list of materialized group directories (one per group), all
    under a single temp dir registered for cleanup at interpreter exit.
    """
    artifact = Path(artifact)
    dest = Path(tempfile.mkdtemp(prefix="abc-bench-groups."))
    _MATERIALIZED_DIRS.append(dest)
    try:
        with mount_squashfs(artifact) as mount_point:
            group_roots = _iter_group_roots(mount_point)
            if not group_roots:
                raise SquashfsError(
                    f"squashfs image does not contain any task group "
                    f"(no group.yaml): {mount_point}"
                )
            out: list[Path] = []
            for root in group_roots:
                # For a root-level single group the mount root itself is the
                # group; copy its contents into a named subdir so multiple
                # groups never collide and each returned path is a group dir.
                target = dest / root.name if root != mount_point else dest / "group"
                target.mkdir(parents=True, exist_ok=True)
                _copy_dir_contents(root, target, desc=f"Extracting {target.name}")
                out.append(target)
            return out
    except Exception:
        with contextlib.suppress(OSError):
            shutil.rmtree(dest, ignore_errors=True)
        with contextlib.suppress(ValueError):
            _MATERIALIZED_DIRS.remove(dest)
        raise


def _iter_group_roots(mount_point: Path) -> list[Path]:
    """Return every task-group root inside a mounted image.

    A group root is a directory containing ``group.yaml``. This is the mount
    root itself (contents-at-top-level packing) or any of its immediate child
    directories that contain a ``group.yaml`` (one or many groups).
    """
    if (mount_point / "group.yaml").is_file():
        return [mount_point]
    return [
        child
        for child in sorted(mount_point.iterdir())
        if child.is_dir() and (child / "group.yaml").is_file()
    ]


def _copy_dir_contents(source: Path, dest: Path, desc: str = "Copying") -> None:
    """Copy the contents of ``source`` into existing dir ``dest`` with progress."""
    copy_dir_contents_with_progress(source, dest, desc=desc)


def _copy_group_root(mount_point: Path, dest: Path) -> None:
    """Copy the single group root from a mount into ``dest`` (which must exist).

    Supports images that pack either the group directory's *contents* at the
    top level (``group.yaml`` at the root) or a single top-level directory that
    contains the group. Rejects images with several top-level groups (use
    :func:`materialize_groups` for those).
    """
    if (mount_point / "group.yaml").is_file():
        source = mount_point
    else:
        children = [c for c in mount_point.iterdir() if c.is_dir()]
        if len(children) == 1 and (children[0] / "group.yaml").is_file():
            source = children[0]
        else:
            raise SquashfsError(
                f"squashfs image does not contain a single task group (no group.yaml): "
                f"{mount_point}"
            )
    _copy_dir_contents(source, dest, desc=f"Extracting {source.name}")


@atexit.register
def _cleanup_materialized() -> None:
    for path in _MATERIALIZED_DIRS:
        with contextlib.suppress(OSError):
            shutil.rmtree(path, ignore_errors=True)
