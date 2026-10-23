"""Shared progress-reporting copy helpers for ABC Bench installs.

Task-group installs (especially from squashfs images, which can be several GB)
copy large directory trees around. These helpers wrap ``shutil.copytree`` so all
symlink/directory semantics are preserved while a tqdm progress bar advances as
bytes are copied. Byte-based progress is used (rather than file counts) because
task-group payloads are dominated by a few large files.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tqdm import tqdm


def _tree_size_bytes(src: Path) -> int:
    """Return the total size of regular files under ``src`` in bytes.

    Symlinks are not followed (matching ``copytree(symlinks=True)``), so their
    tiny link targets do not contribute meaningfully and are ignored here.
    """
    total = 0
    for p in src.rglob("*"):
        if p.is_symlink():
            continue
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def copytree_with_progress(src: Path, dest: Path, desc: str) -> None:
    """Recursively copy ``src`` to ``dest`` with a byte-based tqdm progress bar.

    Delegates to ``shutil.copytree(src, dest, symlinks=True)`` so all symlink and
    directory semantics are preserved, wrapping the per-file copy function to
    advance a progress bar by the number of bytes copied. ``dest`` must not
    already exist.
    """
    src = Path(src)
    dest = Path(dest)
    total = _tree_size_bytes(src)

    with tqdm(
        total=total, desc=desc, unit="B", unit_scale=True, unit_divisor=1024
    ) as bar:

        def _copy(source: str, target: str) -> None:
            shutil.copy2(source, target, follow_symlinks=False)
            try:
                bar.update(Path(target).stat().st_size)
            except OSError:
                pass

        shutil.copytree(src, dest, symlinks=True, copy_function=_copy)


def copy_dir_contents_with_progress(source: Path, dest: Path, desc: str) -> None:
    """Copy the *contents* of ``source`` into existing dir ``dest`` with a bar.

    Like :func:`copytree_with_progress` but ``dest`` already exists and only the
    children of ``source`` are copied into it (used when materializing squashfs
    groups into a pre-created target directory).
    """
    source = Path(source)
    dest = Path(dest)
    total = _tree_size_bytes(source)

    with tqdm(
        total=total, desc=desc, unit="B", unit_scale=True, unit_divisor=1024
    ) as bar:

        def _copy(src_file: str, target: str) -> None:
            shutil.copy2(src_file, target, follow_symlinks=False)
            try:
                bar.update(Path(target).stat().st_size)
            except OSError:
                pass

        for child in source.iterdir():
            target = dest / child.name
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, target, symlinks=True, copy_function=_copy)
            else:
                _copy(str(child), str(target))
