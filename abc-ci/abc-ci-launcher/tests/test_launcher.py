"""Tests for abc-ci-launcher trigger-request variable mapping and batch flags."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from abc_ci_launcher.config import DEFAULT_CONFIG_DIR, init_config, load_config
from abc_ci_launcher.gitlab import GitLabTriggerClient, TriggerRequest
from abc_ci_launcher.main import build_parser, print_trigger_result


def test_trigger_request_variables_default_omits_batch_vars() -> None:
    variables = TriggerRequest(task="g/t", agent="a").variables()
    assert variables == {"AGENT": "a", "TASK": "g/t"}


def test_trigger_request_variables_include_attempts() -> None:
    variables = TriggerRequest(task="", task_group="g", agent="a", attempts=3).variables()
    assert variables["TASK_GROUP"] == "g"
    assert variables["ATTEMPTS"] == "3"
    assert "NODES" not in variables


def test_trigger_request_variables_include_nodes() -> None:
    variables = TriggerRequest(task="g/t", agent="a", attempts=4, nodes=8).variables()
    assert variables["ATTEMPTS"] == "4"
    assert variables["NODES"] == "8"
    # USE_MPI is derived downstream from NODES>1; the launcher never sends it.
    assert "USE_MPI" not in variables


def test_trigger_request_single_node_still_sends_nodes() -> None:
    # A single node is a valid explicit request; MPI is derived downstream
    # (NODES>1), so NODES=1 simply runs directly on the CI runner.
    variables = TriggerRequest(task="g/t", agent="a", nodes=1).variables()
    assert variables["NODES"] == "1"
    assert "USE_MPI" not in variables


def test_parser_accepts_attempts_nodes() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["trigger", "--task", "g/t", "--agent", "a", "--attempts", "5", "--nodes", "4"]
    )
    assert args.attempts == 5
    assert args.nodes == 4
    assert not hasattr(args, "mpi")


def test_parser_rejects_nonpositive_attempts() -> None:
    parser = build_parser()
    try:
        parser.parse_args(["trigger", "--task", "g/t", "--agent", "a", "--attempts", "0"])
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for --attempts 0")


def test_trigger_request_dev_sets_dev_var() -> None:
    variables = TriggerRequest(task="g/t", agent="a", dev=True).variables()
    assert variables["DEV"] == "1"


def test_trigger_request_default_omits_dev_var() -> None:
    variables = TriggerRequest(task="g/t", agent="a").variables()
    assert "DEV" not in variables


def test_parser_accepts_dev_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["trigger", "--task", "g/t", "--agent", "a", "--dev"])
    assert args.dev is True
    group_args = parser.parse_args(["trigger-group", "--task-group", "g", "--agent", "a", "--dev"])
    assert group_args.dev is True


def test_public_config_defaults(tmp_path: Path) -> None:
    path = init_config(tmp_path / "config.yaml")
    config = load_config(path)
    assert DEFAULT_CONFIG_DIR == Path.home() / ".abc-bench" / "abc-ci"
    assert config.gitlab_base_url == "https://gitlab.example.org"
    assert config.trigger_token_env == "ABC_CI_TRIGGER_TOKEN"
    assert config.validate(require_token=False) == ["gitlab.project_id is required"]


def test_parser_uses_public_benchmark_config_option() -> None:
    parser = build_parser()
    assert parser.prog == "abc-ci"
    for command, target in [("trigger", "--task"), ("trigger-group", "--task-group")]:
        args = parser.parse_args([command, target, "g/t", "--abc-bench-config", "example.yaml"])
        assert args.abc_bench_config == "example.yaml"


def test_dry_run_redacts_all_forwarded_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    config = load_config(init_config(tmp_path / "config.yaml"))
    monkeypatch.setenv("ABC_CI_TRIGGER_TOKEN", "trigger-secret")
    # This name deliberately contains neither KEY nor SECRET.
    monkeypatch.setenv("PROVIDER_AUTH", "provider-secret")
    client = GitLabTriggerClient(config)
    request = TriggerRequest(task="g/t", agent="example", key_env_names=["PROVIDER_AUTH"])
    form = client.build_form_data(request)
    assert form["variables[PROVIDER_AUTH]"] == "provider-secret"
    payload = client.trigger(request, dry_run=True)
    assert payload["form_data"]["token"] == "<hidden>"
    assert payload["form_data"]["variables[PROVIDER_AUTH]"] == "<hidden>"
    assert "provider-secret" not in str(payload)
    capsys.readouterr()
    for json_output in [False, True]:
        print_trigger_result(request, payload, json_output=json_output)
        output = capsys.readouterr().out
        assert "provider-secret" not in output
        assert "trigger-secret" not in output
        assert "<hidden>" in output
