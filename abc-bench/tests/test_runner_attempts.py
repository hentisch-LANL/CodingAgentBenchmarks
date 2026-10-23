from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench.config import Config
from abc_bench.runner import (
    BenchmarkRunner,
    _agent_identity_hash,
    _attempt_dir_name,
    _is_attempt_dir,
    attempt_is_complete,
)


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
        "agents:\n"
        "  demo-agent:\n"
        "    harness: opencode\n"
        "    models:\n"
        "      - demo-model\n"
        "    harness_config: {}\n"
        "defaults:\n"
        "  results_dir: results\n"
        "  timeout_seconds: 60\n"
        "  sandbox_network: true\n"
        f"package_root: {Path(__file__).resolve().parent.parent}\n"
    )


def _runner(tmp_path: Path) -> BenchmarkRunner:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    return BenchmarkRunner(config, config.group_resolver(), dry_run=True, stream_output=False)


def _models() -> list[dict]:
    return [{"model": "demo/model", "base_url": "https://example.com/v1"}]


def test_attempt_dir_name_and_detection() -> None:
    assert _attempt_dir_name(1) == "attempt_01"
    assert _attempt_dir_name(12) == "attempt_12"
    assert _is_attempt_dir("attempt_01")
    assert _is_attempt_dir("attempt_07")
    assert not _is_attempt_dir("20260101T000000_demo-agent_a")
    assert not _is_attempt_dir("attempt_x")


def test_attempt_is_complete_reads_metadata(tmp_path: Path) -> None:
    d = tmp_path / "run"
    d.mkdir()
    assert attempt_is_complete(d) is False  # no metadata

    (d / "metadata.json").write_text(json.dumps({"status": "HARNESS_ERROR", "complete": False}))
    assert attempt_is_complete(d) is False

    (d / "metadata.json").write_text(json.dumps({"status": "PASS", "complete": True}))
    assert attempt_is_complete(d) is True

    # Backward-compat: no explicit "complete" flag falls back to status.
    (d / "metadata.json").write_text(json.dumps({"status": "FAIL"}))
    assert attempt_is_complete(d) is True
    (d / "metadata.json").write_text(json.dumps({"status": "ENDPOINT_ERROR"}))
    assert attempt_is_complete(d) is False


def test_result_dir_single_attempt_is_flat(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    started = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    result_dir, mode, archive = runner._result_dir(
        task_group="demo",
        task_id="a",
        agent_id="demo-agent",
        models=_models(),
        harness_id="opencode",
        started_at=started,
        results_dir=tmp_path / "results",
        output_dir=None,
        attempt=None,
        attempts=1,
    )
    assert mode == "organized"
    # Flat: <archive>/demo/a/<run_name>, no attempt_NN segment.
    assert result_dir.parent.name == "a"
    assert not any(_is_attempt_dir(part) for part in result_dir.parts)


def test_result_dir_multi_attempt_uses_attempt_subdir_and_migrates(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    started = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    archive_root = tmp_path / "results"

    # Simulate a pre-existing flat single-attempt run for demo/a.
    agent_archive = f"demo-agent_{_agent_identity_hash('opencode', _models())}"
    task_parent = archive_root / agent_archive / "demo" / "a"
    flat_run = task_parent / "20260101T000000_demo-agent_a"
    flat_run.mkdir(parents=True)
    (flat_run / "metadata.json").write_text(json.dumps({"status": "PASS", "complete": True}))

    # Now request attempt 2 of a 3-attempt batch.
    result_dir, mode, archive = runner._result_dir(
        task_group="demo",
        task_id="a",
        agent_id="demo-agent",
        models=_models(),
        harness_id="opencode",
        started_at=started,
        results_dir=archive_root,
        output_dir=None,
        attempt=2,
        attempts=3,
    )

    assert mode == "organized"
    # The new run lands under attempt_02.
    assert result_dir.parent.name == "attempt_02"
    # The pre-existing flat run was migrated into attempt_01.
    migrated = task_parent / "attempt_01" / "20260101T000000_demo-agent_a"
    assert migrated.is_dir()
    assert (migrated / "metadata.json").is_file()
    # No flat run remains directly under the task dir.
    assert not (task_parent / "20260101T000000_demo-agent_a").exists()
