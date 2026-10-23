"""Tests for the provider-executed tool guard in the model proxy.

Provider-executed ("native") tools are performed by the model provider's own
infrastructure inside the single HTTP response the proxy forwards. Unlike a
locally executed function tool, nothing egresses from the container, so the
sandbox network namespace cannot confine them and the harness transcript shows
no tool activity. For a benchmark that may run export-controlled code that is an
untraceable egress path, so the proxy blocks them unless explicitly allowed.

The end-to-end proof against a real `codex exec` run lives in
`tools_dev/prove_tool_guard.py`; these tests pin the logic and wiring.
"""

from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton.proxy import (  # noqa: E402
    LOCALLY_EXECUTED_TOOL_TYPES,
    PROVIDER_EXECUTED_TOOL_TYPES,
    ProviderExecutedToolBlocked,
    ProxyRequest,
    _UpstreamProxyCore,
    find_provider_executed_tools,
)

from abc_bench.config import Config  # noqa: E402
from abc_bench.runner import BenchmarkRunner  # noqa: E402


# --- detection ------------------------------------------------------------


def test_local_function_tools_are_allowed() -> None:
    """The four tools Codex normally declares are locally executed."""
    body = {
        "tools": [
            {"type": "function", "name": "exec_command", "parameters": {}, "strict": True},
            {"type": "function", "name": "write_stdin", "parameters": {}, "strict": True},
            {"type": "function", "name": "view_image", "parameters": {}, "strict": True},
        ]
    }
    assert find_provider_executed_tools(body) == []


def test_native_web_search_is_detected() -> None:
    """This is the exact entry codex-cli 0.155.1 sends by default."""
    body = {"tools": [{"type": "web_search", "external_web_access": True}]}
    assert find_provider_executed_tools(body) == ["web_search"]


def test_namespace_tools_are_recursed_into() -> None:
    """Codex nests function tools under a `namespace` entry."""
    local = {
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}}],
            }
        ]
    }
    assert find_provider_executed_tools(local) == []

    smuggled = {
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [{"type": "web_search"}],
            }
        ]
    }
    assert find_provider_executed_tools(smuggled) == ["web_search"]


def test_tool_type_matching_is_case_insensitive() -> None:
    assert find_provider_executed_tools({"tools": [{"type": "WEB_SEARCH"}]}) == ["web_search"]


def test_unknown_tool_without_local_signature_fails_closed() -> None:
    """A future native tool must be blocked, not silently forwarded."""
    body = {"tools": [{"type": "some_future_native_tool"}]}
    assert find_provider_executed_tools(body) == ["some_future_native_tool"]


def test_unknown_tool_with_local_call_signature_is_allowed() -> None:
    """A name plus a schema means the agent invokes it locally."""
    body = {"tools": [{"type": "newstyle", "name": "do_thing", "parameters": {}}]}
    assert find_provider_executed_tools(body) == []
    body = {"tools": [{"type": "newstyle", "name": "do_thing", "input_schema": {}}]}
    assert find_provider_executed_tools(body) == []


@pytest.mark.parametrize(
    "body", [{}, {"tools": []}, {"tools": "not-a-list"}, None, [], "string"]
)
def test_malformed_bodies_do_not_raise(body) -> None:
    assert find_provider_executed_tools(body) == []


def test_known_tool_sets_are_disjoint() -> None:
    assert not (PROVIDER_EXECUTED_TOOL_TYPES & LOCALLY_EXECUTED_TOOL_TYPES)


# --- enforcement ----------------------------------------------------------


class _ReachedUpstream(RuntimeError):
    """Raised by the stub to prove the guard let a request through."""


def _core(tmp_path: Path, allowed=None) -> _UpstreamProxyCore:
    registry = {
        "demo": {
            "base_url": "https://example.invalid/v1",
            "api_key": "secret",
            "model": "demo/model",
        }
    }
    core = _UpstreamProxyCore(
        registry, tmp_path / "proxy.log", allowed_provider_executed_tools=allowed
    )

    # Never touch the network. A permitted request must fail fast and
    # unmistakably, instead of retrying against an unroutable host until the
    # request deadline expires (which would hang the test suite).
    def _fail_fast(*args, **kwargs):
        raise _ReachedUpstream("guard permitted the request")

    core._ssl_context = _fail_fast  # type: ignore[method-assign]
    return core


def _request(body: dict) -> ProxyRequest:
    return ProxyRequest(
        method="POST",
        path="/responses",
        headers={"content-type": "application/json"},
        body=json.dumps(body).encode(),
    )


def test_blocked_before_any_upstream_connection(tmp_path: Path) -> None:
    """The guard must run before the socket is opened.

    base_url is unroutable: if enforcement happened after connecting we would see
    a connection error instead of ProviderExecutedToolBlocked.
    """
    core = _core(tmp_path)
    request = _request({"model": "demo", "tools": [{"type": "web_search"}]})

    with pytest.raises(ProviderExecutedToolBlocked) as excinfo:
        core.forward(request)

    assert excinfo.value.tool_types == ["web_search"]
    assert core.blocked_provider_executed_tools == ["web_search"]
    assert core.last_error_kind == "provider_executed_tool_blocked"
    # The operator needs to know why and what to do about it.
    assert "not inside the sandbox" in excinfo.value.detail
    assert "allowed_provider_executed_tools" in excinfo.value.detail
    assert "[tool-policy] BLOCKED" in (tmp_path / "proxy.log").read_text()


def test_explicitly_allowed_tool_is_not_blocked(tmp_path: Path) -> None:
    core = _core(tmp_path, allowed={"web_search"})
    request = _request({"model": "demo", "tools": [{"type": "web_search"}]})

    # Not blocked by policy; the stubbed upstream proves it got past the guard.
    with pytest.raises(_ReachedUpstream):
        core.forward(request)
    assert core.blocked_provider_executed_tools == []
    assert "[tool-policy] allowing" in (tmp_path / "proxy.log").read_text()


def test_allowlist_is_case_insensitive(tmp_path: Path) -> None:
    core = _core(tmp_path, allowed={"WEB_SEARCH"})
    request = _request({"model": "demo", "tools": [{"type": "web_search"}]})
    with pytest.raises(_ReachedUpstream):
        core.forward(request)
    assert core.blocked_provider_executed_tools == []


def test_partial_allowlist_still_blocks_the_rest(tmp_path: Path) -> None:
    core = _core(tmp_path, allowed={"web_search"})
    request = _request(
        {"model": "demo", "tools": [{"type": "web_search"}, {"type": "code_interpreter"}]}
    )
    with pytest.raises(ProviderExecutedToolBlocked) as excinfo:
        core.forward(request)
    assert excinfo.value.tool_types == ["code_interpreter"]


def test_non_json_body_is_not_rejected(tmp_path: Path) -> None:
    """Only JSON bodies can declare tools; don't break anything else."""
    core = _core(tmp_path)
    request = ProxyRequest(
        method="POST", path="/responses", headers={}, body=b"\x00\x01not json"
    )
    with pytest.raises(_ReachedUpstream):
        core.forward(request)
    assert core.blocked_provider_executed_tools == []


# --- configuration and classification -------------------------------------


def _write_config(path: Path, allowed_line: str = "") -> None:
    path.write_text(
        "providers:\n"
        "  demo:\n"
        "    base_url: https://example.com/v1\n"
        "    api_key_env: DEMO_API_KEY\n"
        "models:\n"
        "  demo-model:\n"
        "    provider: demo\n"
        "    model: demo/model\n"
        "agents:\n"
        "  demo-agent:\n"
        "    harness: codex\n"
        "    models:\n"
        "      - demo-model\n"
        "    harness_config: {}\n"
        "defaults:\n"
        "  results_dir: results\n"
        f"{allowed_line}"
        f"package_root: {Path(__file__).resolve().parent.parent}\n"
    )


def test_nothing_is_allowed_by_default(tmp_path: Path) -> None:
    """Fail closed: the default must permit no provider-executed tools."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    assert Config(config_path).allowed_provider_executed_tools == frozenset()


def test_config_allowlist_is_parsed_and_normalized(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        allowed_line="  allowed_provider_executed_tools:\n    - Web_Search\n",
    )
    assert Config(config_path).allowed_provider_executed_tools == frozenset({"web_search"})


def test_config_allowlist_rejects_a_bad_type(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, allowed_line="  allowed_provider_executed_tools: 42\n")
    with pytest.raises(ValueError, match="must be a list"):
        Config(config_path).allowed_provider_executed_tools


def test_runner_passes_allowlist_to_the_sandbox(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        allowed_line="  allowed_provider_executed_tools:\n    - web_search\n",
    )
    config = Config(config_path)
    runner = BenchmarkRunner(config, config.group_resolver(), dry_run=True)
    sandbox = runner._sandbox(tmp_path)
    assert sandbox.allowed_provider_executed_tools == frozenset({"web_search"})


def test_blocked_tool_is_not_graded_as_a_task_failure() -> None:
    """A containment failure must not be recorded as a plain benchmark FAIL."""

    class _Execution:
        endpoint_exhausted = False
        context_length_exceeded = False
        blocked_provider_executed_tools = ["web_search"]

    status, phase = BenchmarkRunner._classify_harness_failure(_Execution())
    assert status == "HARNESS_ERROR"
    assert phase == "provider_executed_tool_blocked"


def test_blocked_tool_classification_takes_priority_over_context_length() -> None:
    class _Execution:
        endpoint_exhausted = True
        context_length_exceeded = True
        blocked_provider_executed_tools = ["web_search"]

    status, phase = BenchmarkRunner._classify_harness_failure(_Execution())
    assert status == "HARNESS_ERROR"
    assert phase == "provider_executed_tool_blocked"
