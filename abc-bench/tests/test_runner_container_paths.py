from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton import Agent, BindMount
from automaton.proxy import (
    MODEL_PROXY_ATTEMPT_TIMEOUT_SECONDS,
    MODEL_PROXY_REQUEST_DEADLINE_SECONDS,
)

from abc_bench.config import Config
from abc_bench.runner import (
    BenchmarkRunner,
    CONTAINER_BASE_ROOT,
    CONTAINER_RUN_ROOT,
    CONTAINER_WORKSPACE_ROOT,
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
        f"package_root: {Path(__file__).resolve().parent.parent}\n"
    )


def test_ch_run_prefix_uses_anonymous_container_mounts(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver(), dry_run=True)

    result_dir = tmp_path / "results" / "demo-run"
    mounts = runner._container_bind_mounts(result_dir)
    sandbox = runner._sandbox(tmp_path)
    cmd = sandbox._ch_run_prefix(
        container_path=tmp_path / "container",
        cwd=CONTAINER_WORKSPACE_ROOT,
        env_vars={"ABC_BENCH_WORKSPACE": str(CONTAINER_WORKSPACE_ROOT)},
        bind_mounts=mounts,
    )

    assert mounts == [
        BindMount(result_dir, CONTAINER_RUN_ROOT),
        BindMount(result_dir / "workspace", CONTAINER_WORKSPACE_ROOT),
    ]
    assert f"--bind={result_dir.resolve()}:{CONTAINER_RUN_ROOT}" in cmd
    assert f"--bind={(result_dir / 'workspace').resolve()}:{CONTAINER_WORKSPACE_ROOT}" in cmd
    assert f"--cd={CONTAINER_WORKSPACE_ROOT}" in cmd
    assert f"--set-env=ABC_BENCH_WORKSPACE={CONTAINER_WORKSPACE_ROOT}" in cmd
    assert cmd[-1] == str((tmp_path / "container").resolve())


def test_container_run_paths_use_anonymous_locations(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver(), dry_run=True)

    paths = runner._container_run_paths()

    assert paths["run_root"] == CONTAINER_RUN_ROOT
    assert paths["workspace"] == CONTAINER_WORKSPACE_ROOT
    assert paths["prompt"] == CONTAINER_RUN_ROOT / "prompt.md"
    assert paths["group_prompt"] == CONTAINER_RUN_ROOT / "group_prompt.md"
    assert paths["harness_dir"] == CONTAINER_RUN_ROOT / "harness"
    assert paths["build_env"] == CONTAINER_RUN_ROOT / "build_env.sh"
    assert paths["models_config"] == CONTAINER_RUN_ROOT / "models_config.yaml"
    assert paths["harness_config"] == CONTAINER_RUN_ROOT / "harness_config.yaml"
    assert paths["harness_output"] == CONTAINER_RUN_ROOT / "harness_output"
    assert paths["verification"] == CONTAINER_RUN_ROOT / "verification"
    assert paths["sandbox_helper"] == CONTAINER_RUN_ROOT / "sandbox_helper.py"
    assert paths["task_env"] == CONTAINER_WORKSPACE_ROOT / "abc-bench-task-env"
    assert str(tmp_path) not in {str(value) for value in paths.values()}


def test_default_ch_run_prefix_mounts_repo_under_internal_base(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver(), dry_run=True)

    cmd = runner._sandbox(tmp_path)._ch_run_prefix(container_path=tmp_path / "container")

    assert f"--bind={tmp_path.resolve()}:{CONTAINER_BASE_ROOT}" in cmd


def test_harness_models_config_keeps_abc_bench_proxy_wiring(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver(), dry_run=True)

    sandbox = runner._sandbox(tmp_path)
    agent = Agent("demo-agent", Config(config_path))
    models_config = sandbox.harness_models_config(agent)

    assert models_config["api_key_env"] == "ABC_BENCH_MODEL_PROXY_API_KEY"
    assert models_config["proxy_base_url"] == sandbox.proxy_base_url
    assert models_config["models"][0]["opencode_model"] == "abc-bench-proxy/demo-model"
    assert models_config["models"][0]["proxied_base_url"] == "https://example.com/v1"


def test_model_proxy_timeout_constants_match_current_policy() -> None:
    assert MODEL_PROXY_ATTEMPT_TIMEOUT_SECONDS == 900.0
    assert MODEL_PROXY_REQUEST_DEADLINE_SECONDS == 3600.0
