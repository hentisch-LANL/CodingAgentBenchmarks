from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton.proxy import (
    MODEL_PROXY_ATTEMPT_TIMEOUT_SECONDS,
    MODEL_PROXY_REQUEST_DEADLINE_SECONDS,
)
from automaton.sandbox import CommandRunFailed, ExecutionResult

from abc_bench.config import Config
from abc_bench.runner import BenchmarkResult, BenchmarkRunner


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


def test_default_benchmark_timeout_is_disabled_and_proxy_limits_are_explicit(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    assert runner.config.timeout_seconds == 60
    assert MODEL_PROXY_ATTEMPT_TIMEOUT_SECONDS == 900.0
    assert MODEL_PROXY_REQUEST_DEADLINE_SECONDS == 3600.0


def test_benchmark_result_supports_new_statuses(tmp_path: Path) -> None:
    result = BenchmarkResult(
        task_id="task",
        task_group="group",
        task_ref="group/task",
        agent_id="demo-agent",
        harness_id="opencode",
        model_names=["demo-model"],
        status="ENDPOINT_ERROR",
        result_dir=tmp_path,
        error="proxy exhausted",
        failure_phase="harness",
    )

    payload = result.to_dict()

    assert payload["status"] == "ENDPOINT_ERROR"
    assert payload["failure_phase"] == "harness"
    assert payload["error"] == "proxy exhausted"


def test_command_failure_detail_mentions_timeout(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    log_file = tmp_path / "harness.log"
    log_file.write_text("partial output\n")

    detail = runner._sandbox(tmp_path)._command_failure_detail(124, log_file)

    assert "Command timed out." in detail
    assert "See harness.log for process output." in detail


def test_command_run_failed_keeps_detail(tmp_path: Path) -> None:
    exc = CommandRunFailed(1, tmp_path / "ch-run.log", "boom")

    assert exc.returncode == 1
    assert exc.detail == "boom"


def _failed_execution(tmp_path: Path, **flags) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        workspace=tmp_path,
        returncode=1,
        failure_detail="agent failed",
        **flags,
    )


def test_context_length_exhaustion_maps_to_fail(tmp_path: Path) -> None:
    execution = _failed_execution(tmp_path, context_length_exceeded=True)

    status, failure_phase = BenchmarkRunner._classify_harness_failure(execution)

    assert status == "FAIL"
    assert failure_phase == "context_length"


def test_endpoint_exhaustion_still_maps_to_endpoint_error(tmp_path: Path) -> None:
    execution = _failed_execution(tmp_path, endpoint_exhausted=True)

    status, failure_phase = BenchmarkRunner._classify_harness_failure(execution)

    assert status == "ENDPOINT_ERROR"
    assert failure_phase == "harness"


def test_other_harness_failure_maps_to_harness_error(tmp_path: Path) -> None:
    execution = _failed_execution(tmp_path)

    status, failure_phase = BenchmarkRunner._classify_harness_failure(execution)

    assert status == "HARNESS_ERROR"
    assert failure_phase == "harness"


def test_context_length_takes_priority_over_endpoint(tmp_path: Path) -> None:
    execution = _failed_execution(
        tmp_path, context_length_exceeded=True, endpoint_exhausted=True
    )

    status, _ = BenchmarkRunner._classify_harness_failure(execution)

    assert status == "FAIL"
