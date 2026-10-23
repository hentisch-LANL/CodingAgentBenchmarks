from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench.config import Config
from abc_bench.runner import BenchmarkRunner


def _write_config(config_path: Path) -> None:
    config_path.write_text(
        "providers:\n"
        "  demo:\n"
        "    base_url: https://example.com/v1\n"
        "    api_key_env: DEMO_API_KEY\n"
        "    cert: null\n"
        "models:\n"
        "  demo-model:\n"
        "    provider: demo\n"
        "    model: demo/model\n"
        "defaults:\n"
        "  results_dir: results\n"
        "  timeout_seconds: 60\n"
        f"package_root: {Path(__file__).resolve().parent.parent}\n"
    )


def _runner(tmp_path: Path) -> BenchmarkRunner:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    return BenchmarkRunner(config, config.group_resolver(), dry_run=True)


def test_host_metadata_collects_cpu_memory_and_gpu_data(tmp_path: Path, monkeypatch) -> None:
    runner = _runner(tmp_path)

    monkeypatch.setattr(runner, "_cpu_model", lambda: "Demo CPU")
    monkeypatch.setattr(runner, "_meminfo_total_bytes", lambda: 123456789)
    monkeypatch.setattr(runner, "_gpu_metadata", lambda: [{"index": 0, "name": "Demo GPU", "memory_total_bytes": 2048}])
    monkeypatch.setattr("abc_bench.runner.os.cpu_count", lambda: 64)

    metadata = runner._host_metadata()

    assert metadata["cpu"] == {
        "model": "Demo CPU",
        "architecture": metadata["os"]["machine"],
        "logical_cores": 64,
    }
    assert metadata["memory"] == {"total_bytes": 123456789}
    assert metadata["gpus"] == [{"index": 0, "name": "Demo GPU", "memory_total_bytes": 2048}]


def test_gpu_metadata_returns_empty_when_nvidia_smi_missing(tmp_path: Path, monkeypatch) -> None:
    runner = _runner(tmp_path)

    def fake_run(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("abc_bench.runner.subprocess.run", fake_run)

    assert runner._gpu_metadata() == []


def test_cpu_and_memory_helpers_tolerate_missing_proc_files(tmp_path: Path, monkeypatch) -> None:
    runner = _runner(tmp_path)

    def fake_read_text(self):
        raise OSError

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert runner._cpu_model() is None
    assert runner._meminfo_total_bytes() is None
