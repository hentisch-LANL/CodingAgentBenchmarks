"""Tests for ``abc-bench install`` across archive formats.

Covers the combined-archive case: a single ``.tar.gz`` / ``.sqfs`` whose root
contains *several* task-group directories installs every group in one call.
The squashfs cases are skipped when ``mksquashfs``/``squashfuse`` are absent.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench import install


def _make_group(root: Path, group_id: str) -> Path:
    group_dir = root / group_id
    group_dir.mkdir(parents=True)
    (group_dir / "group.yaml").write_text(
        f"id: {group_id}\nname: {group_id.title()}\ndescription: {group_id} group\n"
    )
    (group_dir / "marker.txt").write_text(group_id)
    return group_dir


_HAVE_SQUASHFS = shutil.which("mksquashfs") is not None and shutil.which("squashfuse") is not None


def test_install_multi_group_targz(tmp_path: Path) -> None:
    exp = tmp_path / "expanded"
    _make_group(exp, "group_a")
    _make_group(exp, "group_b")

    tarball = tmp_path / "benchmarks.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(exp / "group_a", arcname="group_a")
        tar.add(exp / "group_b", arcname="group_b")

    dest = tmp_path / "installed"
    ids = install.install_source(tarball, dest_root=dest, force=True)

    assert sorted(ids) == ["group_a", "group_b"]
    for gid in ("group_a", "group_b"):
        assert (dest / gid / "group.yaml").is_file()
        assert (dest / gid / "marker.txt").read_text() == gid


@pytest.mark.skipif(not _HAVE_SQUASHFS, reason="mksquashfs/squashfuse not available")
def test_install_multi_group_sqfs(tmp_path: Path) -> None:
    exp = tmp_path / "expanded"
    _make_group(exp, "group_a")
    _make_group(exp, "group_b")

    image = tmp_path / "benchmarks.sqfs"
    subprocess.run(
        ["mksquashfs", str(exp / "group_a"), str(exp / "group_b"), str(image),
         "-noappend", "-no-progress"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    dest = tmp_path / "installed"
    ids = install.install_source(image, dest_root=dest, force=True)

    assert sorted(ids) == ["group_a", "group_b"]
    for gid in ("group_a", "group_b"):
        assert (dest / gid / "group.yaml").is_file()
        assert (dest / gid / "marker.txt").read_text() == gid


@pytest.mark.skipif(not _HAVE_SQUASHFS, reason="mksquashfs/squashfuse not available")
def test_install_single_group_sqfs(tmp_path: Path) -> None:
    exp = tmp_path / "expanded"
    _make_group(exp, "only_group")

    # Image whose root IS the group dir contents (group.yaml at root).
    image = tmp_path / "only_group.sqfs"
    subprocess.run(
        ["mksquashfs", str(exp / "only_group"), str(image), "-noappend", "-no-progress"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    dest = tmp_path / "installed"
    ids = install.install_source(image, dest_root=dest, force=True)

    assert ids == ["only_group"]
    assert (dest / "only_group" / "group.yaml").is_file()
