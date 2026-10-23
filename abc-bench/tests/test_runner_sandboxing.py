from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton.sandbox_helper import SCRIPT as SANDBOX_HELPER_SCRIPT

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


def test_runner_defaults_to_configured_sandbox_network(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)

    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver())

    assert runner.sandbox_network is True


def test_runner_stages_automaton_sandbox_helper_script(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver())

    # The abc-bench runner keeps the /abc-bench/run mount root for the helper.
    container_paths = runner._container_run_paths()
    assert container_paths["sandbox_helper"] == Path("/abc-bench/run/sandbox_helper.py")

    # The runner sources the helper script from automaton.
    assert runner._sandbox(tmp_path).sandbox_helper_script() == SANDBOX_HELPER_SCRIPT


def test_sandbox_helper_script_sets_loopback_and_unsets_proxies() -> None:
    assert 'subprocess.run(["ip", "link", "set", "lo", "up"], check=True)' in SANDBOX_HELPER_SCRIPT
    assert "HTTP_PROXY" in SANDBOX_HELPER_SCRIPT
    assert "AUTOMATON_PROXY_FD" in SANDBOX_HELPER_SCRIPT
    assert 'ThreadingHTTPServer(("127.0.0.1", proxy_port), Handler)' in SANDBOX_HELPER_SCRIPT
    assert "sandbox proxy transport failed" in SANDBOX_HELPER_SCRIPT
    assert "os._exit(86)" in SANDBOX_HELPER_SCRIPT
    assert "AUTOMATON_SANDBOX_CMD" in SANDBOX_HELPER_SCRIPT
    assert "AUTOMATON_SANDBOX_LOG" in SANDBOX_HELPER_SCRIPT
