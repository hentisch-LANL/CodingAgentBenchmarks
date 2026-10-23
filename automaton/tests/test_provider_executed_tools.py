"""Tests for the provider-executed tool policy in automaton.proxy.

Provider-executed ("native") tools are run by the model provider's own
infrastructure inside the single HTTP response the proxy forwards, rather than by
the agent inside the sandbox. A locally executed function tool is confined by the
sandbox's loopback-only network namespace and appears in the harness transcript;
a provider-executed one opens no connection from the container at all, so neither
the namespace nor the proxy log reveals that a fetch occurred.

That makes them an untraceable egress path in both directions -- remote content
can be pulled in, and prompt content can leave inside a provider-side query -- so
the proxy blocks them unless a caller opts in explicitly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from automaton.proxy import (
    LOCALLY_EXECUTED_TOOL_TYPES,
    PROVIDER_EXECUTED_TOOL_TYPES,
    ProviderExecutedToolBlocked,
    ProxyRequest,
    _UpstreamProxyCore,
    find_provider_executed_tools,
)


class _ReachedUpstream(RuntimeError):
    """Raised by the stub to prove a request got past the policy check."""


def _core(tmp_path: Path, allowed=None) -> _UpstreamProxyCore:
    registry = {
        "demo-model": {
            "base_url": "https://example.com/v1",
            "api_key": "key",
            "model": "provider/demo-model",
        }
    }
    core = _UpstreamProxyCore(
        registry,
        tmp_path / "model_proxy.log",
        allowed_provider_executed_tools=allowed,
    )

    # Fail fast instead of retrying against the network for the full deadline.
    def _fail_fast(*args, **kwargs):
        raise _ReachedUpstream("policy permitted the request")

    core._ssl_context = _fail_fast  # type: ignore[method-assign]
    return core


def _request(body: dict) -> ProxyRequest:
    return ProxyRequest(
        method="POST",
        path="/responses",
        headers={"content-type": "application/json"},
        body=json.dumps(body).encode(),
    )


# --- detection ------------------------------------------------------------


def test_function_tools_are_locally_executed() -> None:
    """These are the tools the Codex CLI normally declares."""
    body = {
        "tools": [
            {"type": "function", "name": "exec_command", "parameters": {}, "strict": True},
            {"type": "function", "name": "write_stdin", "parameters": {}, "strict": True},
        ]
    }
    assert find_provider_executed_tools(body) == []


def test_native_web_search_is_detected() -> None:
    """The exact entry codex-cli 0.155.1 sends when search is not disabled."""
    body = {"tools": [{"type": "web_search", "external_web_access": True}]}
    assert find_provider_executed_tools(body) == ["web_search"]


def test_anthropic_messages_tools_without_a_type_are_locally_executed() -> None:
    """These are the tools the Claude Code CLI declares on /v1/messages.

    The Messages API identifies a client-executed tool by `name` +
    `input_schema` and omits `type` entirely; only server-side tools carry a
    versioned `type`. `WebSearch`/`WebFetch` are included deliberately: in Claude
    Code these are the CLI's *own* tools, executed in the sandbox where the
    loopback-only network namespace confines them and the call is visible in the
    transcript — unlike Anthropic's server-side `web_search_20250305`, which is
    performed by the provider and is blocked below.
    """
    body = {
        "tools": [
            {"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}},
            {"name": "Edit", "description": "Edit a file", "input_schema": {"type": "object"}},
            {"name": "WebSearch", "description": "Search", "input_schema": {"type": "object"}},
            {"name": "WebFetch", "description": "Fetch", "input_schema": {"type": "object"}},
        ]
    }
    assert find_provider_executed_tools(body) == []


def test_anthropic_server_side_tools_are_still_detected() -> None:
    """A versioned server-side type is provider-executed even next to local tools."""
    body = {
        "tools": [
            {"name": "Bash", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search"},
        ]
    }
    assert find_provider_executed_tools(body) == ["web_search_20250305"]


def test_namespace_tools_are_recursed_into() -> None:
    """A native tool must not be able to hide inside a namespace tool."""
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
            {"type": "namespace", "name": "ns", "tools": [{"type": "web_search"}]}
        ]
    }
    assert find_provider_executed_tools(smuggled) == ["web_search"]


def test_detection_is_case_insensitive() -> None:
    assert find_provider_executed_tools({"tools": [{"type": "WEB_SEARCH"}]}) == ["web_search"]


def test_unknown_tool_without_local_signature_fails_closed() -> None:
    """A native tool added by a future agent release must be blocked, not forwarded."""
    body = {"tools": [{"type": "some_future_native_tool"}]}
    assert find_provider_executed_tools(body) == ["some_future_native_tool"]


@pytest.mark.parametrize("schema_key", ["parameters", "input_schema"])
def test_unknown_tool_with_local_call_signature_is_allowed(schema_key: str) -> None:
    """A name plus a schema means the agent invokes the tool itself."""
    body = {"tools": [{"type": "newstyle", "name": "do_thing", schema_key: {}}]}
    assert find_provider_executed_tools(body) == []


@pytest.mark.parametrize(
    "body", [{}, {"tools": []}, {"tools": "not-a-list"}, {"tools": [None, 3]}, None, [], "s"]
)
def test_malformed_bodies_are_tolerated(body) -> None:
    assert find_provider_executed_tools(body) == []


def test_tool_type_sets_are_disjoint() -> None:
    assert not (PROVIDER_EXECUTED_TOOL_TYPES & LOCALLY_EXECUTED_TOOL_TYPES)


# --- enforcement ----------------------------------------------------------


def test_blocked_before_opening_the_upstream_connection(tmp_path: Path) -> None:
    """Enforcement must precede the socket, so nothing reaches the provider.

    The stub raises _ReachedUpstream if the request gets that far, so seeing
    ProviderExecutedToolBlocked proves the check ran first.
    """
    core = _core(tmp_path)

    with pytest.raises(ProviderExecutedToolBlocked) as excinfo:
        core.forward(_request({"model": "demo-model", "tools": [{"type": "web_search"}]}))

    assert excinfo.value.tool_types == ["web_search"]
    assert core.blocked_provider_executed_tools == ["web_search"]
    assert core.last_error_kind == "provider_executed_tool_blocked"
    assert "[tool-policy] BLOCKED" in (tmp_path / "model_proxy.log").read_text()


def test_block_detail_explains_the_risk_and_the_remedy(tmp_path: Path) -> None:
    core = _core(tmp_path)
    with pytest.raises(ProviderExecutedToolBlocked) as excinfo:
        core.forward(_request({"model": "demo-model", "tools": [{"type": "web_search"}]}))

    detail = excinfo.value.detail
    assert "not inside the sandbox" in detail
    assert "allowed_provider_executed_tools" in detail


def test_explicitly_allowed_tool_is_forwarded(tmp_path: Path) -> None:
    core = _core(tmp_path, allowed={"web_search"})

    with pytest.raises(_ReachedUpstream):
        core.forward(_request({"model": "demo-model", "tools": [{"type": "web_search"}]}))

    assert core.blocked_provider_executed_tools == []
    assert "[tool-policy] allowing" in (tmp_path / "model_proxy.log").read_text()


def test_allowlist_is_case_insensitive(tmp_path: Path) -> None:
    core = _core(tmp_path, allowed={"WEB_SEARCH"})
    with pytest.raises(_ReachedUpstream):
        core.forward(_request({"model": "demo-model", "tools": [{"type": "web_search"}]}))


def test_partial_allowlist_still_blocks_the_remainder(tmp_path: Path) -> None:
    core = _core(tmp_path, allowed={"web_search"})

    with pytest.raises(ProviderExecutedToolBlocked) as excinfo:
        core.forward(
            _request(
                {
                    "model": "demo-model",
                    "tools": [{"type": "web_search"}, {"type": "code_interpreter"}],
                }
            )
        )

    assert excinfo.value.tool_types == ["code_interpreter"]


def test_requests_without_tools_are_unaffected(tmp_path: Path) -> None:
    core = _core(tmp_path)
    with pytest.raises(_ReachedUpstream):
        core.forward(_request({"model": "demo-model", "messages": []}))
    assert core.blocked_provider_executed_tools == []


def test_non_json_body_is_not_rejected(tmp_path: Path) -> None:
    """Tool declarations only appear in JSON bodies; don't break anything else."""
    core = _core(tmp_path)
    request = ProxyRequest("POST", "/responses", {}, b"\x00\x01not json")
    with pytest.raises(_ReachedUpstream):
        core.forward(request)
    assert core.blocked_provider_executed_tools == []


def test_empty_body_is_not_rejected(tmp_path: Path) -> None:
    core = _core(tmp_path)
    with pytest.raises(_ReachedUpstream):
        core.forward(ProxyRequest("GET", "/models", {}, b""))


def test_default_policy_allows_nothing(tmp_path: Path) -> None:
    """Fail closed by default: an absent allowlist must not permit anything."""
    core = _core(tmp_path, allowed=None)
    assert core.allowed_provider_executed_tools == frozenset()
