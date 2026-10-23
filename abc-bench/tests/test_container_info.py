from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench.container import ContainerManager
from abc_bench.groups import GroupResolver


def test_container_size_skips_symlink_dereference(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ABC_BENCH_CONTAINER_BASE", str(tmp_path / "cache"))

    manager = ContainerManager(tmp_path, GroupResolver(installed_root=tmp_path / "empty"))
    container_dir = manager.container_root / "base"
    container_dir.mkdir(parents=True)

    data_file = container_dir / "regular.txt"
    data_file.write_text("hello")
    (container_dir / "cargo").symlink_to("/root/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin/cargo")

    info = manager.get_container_info("base")

    assert info.exists is True
    assert info.size_bytes is not None
    assert info.size_bytes >= data_file.stat().st_size
