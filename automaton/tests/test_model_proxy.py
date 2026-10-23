"""Tests for automaton.proxy (ported from abc_bench/tests/test_model_proxy.py).

The proxy code moved to ``automaton.proxy`` essentially verbatim. These tests
adapt the import paths (``abc_bench.runner`` -> ``automaton.proxy``) and the
monkeypatch targets, and keep the ``opencode_model`` field usage as-is (it is
intentionally NOT renamed).
"""

from __future__ import annotations

import io
import json
import socket
import sys
from pathlib import Path

# Ensure the repo root is on sys.path (also covered by conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from automaton.proxy import EndpointRetryExhausted, ProxyRequest, _UpstreamProxyCore


def _registry() -> dict[str, dict[str, str]]:
    return {
        "demo-model": {
            "base_url": "https://example.com/v1",
            "api_key": "key",
            "model": "provider/demo-model",
            "opencode_model": "abc-bench-proxy/demo-model",
        }
    }


def _anthropic_registry() -> dict[str, dict[str, str]]:
    return {
        "demo-model": {
            "base_url": "https://example.com/v1",
            "api_key": "key",
            "model": "provider/demo-model",
            "opencode_model": "abc-bench-proxy/demo-model",
            "api": "anthropic_messages",
        }
    }


def _multi_registry() -> dict[str, dict[str, str]]:
    return {
        "planner": {
            "base_url": "https://planner.example.com/v1",
            "api_key": "planner-key",
            "model": "provider/planner",
            "opencode_model": "abc-bench-proxy/planner",
        },
        "builder": {
            "base_url": "https://builder.example.com/v1",
            "api_key": "builder-key",
            "model": "provider/builder",
            "opencode_model": "abc-bench-proxy/builder",
        },
    }


def test_model_proxy_retries_transport_failures_and_exhausts(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0)

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            raise socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)
    monkeypatch.setattr("automaton.proxy.time.sleep", lambda _: None)

    with pytest.raises(EndpointRetryExhausted):
        proxy.forward(ProxyRequest("POST", "/chat/completions", {}, b"{}"))

    log_text = log_file.read_text()
    assert "retry 1" in log_text
    assert "endpoint exhausted" in log_text
    assert proxy.endpoint_exhausted is True
    assert proxy.last_error_kind == "endpoint_timeout"
    assert proxy.retry_count >= 1


def test_model_proxy_logs_upstream_http_errors(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0)

    class FakeResponse:
        status = 401
        reason = "Unauthorized"

        def read(self):
            return b'{"error": {"message": "bad key"}}'

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(ProxyRequest("POST", "/chat/completions", {}, b"{}"))

    assert response.status == 401
    assert proxy.endpoint_exhausted is False
    assert proxy.last_error_kind == "upstream_http_error"
    assert proxy.last_error_detail == "bad key"


def test_model_proxy_flags_context_length_exceeded(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0)

    class FakeResponse:
        status = 400
        reason = "Bad Request"

        def read(self):
            return json.dumps(
                {
                    "error": {
                        "code": "context_length_exceeded",
                        "type": "invalid_request_error",
                        "message": (
                            "This model's maximum context length is 200000 tokens. "
                            "However, your messages resulted in 210000 tokens."
                        ),
                    }
                }
            ).encode()

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(ProxyRequest("POST", "/chat/completions", {}, b"{}"))

    assert response.status == 400
    assert proxy.endpoint_exhausted is False
    assert proxy.context_length_exceeded is True
    assert proxy.last_error_kind == "context_length_exceeded"


def test_model_proxy_does_not_flag_non_context_http_errors(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0)

    class FakeResponse:
        status = 429
        reason = "Too Many Requests"

        def read(self):
            return b'{"error": {"code": "rate_limit_exceeded", "message": "slow down"}}'

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(ProxyRequest("POST", "/chat/completions", {}, b"{}"))

    assert response.status == 429
    assert proxy.context_length_exceeded is False
    assert proxy.last_error_kind == "upstream_http_error"


def test_model_proxy_rewrites_configured_model_to_upstream_and_records_usage(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    usage_records_file = tmp_path / "usage.jsonl"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0, usage_records_file=usage_records_file)

    requested = {}

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return json.dumps(
                {
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 4,
                        "total_tokens": 16,
                        "prompt_tokens_details": {"cached_tokens": 2},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    }
                }
            ).encode()

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            requested["method"] = method
            requested["path"] = path
            requested["body"] = json.loads(body.decode())
            requested["headers"] = headers

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"Content-Type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert response.status == 200
    assert requested["path"] == "/v1/chat/completions"
    assert requested["body"]["model"] == "provider/demo-model"
    assert requested["headers"]["Authorization"] == "Bearer key"
    assert proxy.usage_records[0]["configured_model"] == "demo-model"
    assert proxy.usage_records[0]["usage_status"] == "reported"
    assert proxy.usage_records[0]["cost"]["total_cost"] == 0.0
    assert proxy.usage_records[0]["duration_seconds"] is not None
    assert proxy.usage_records[0]["duration_seconds"] >= 0.0
    assert "tokens_per_second" in proxy.usage_records[0]
    assert usage_records_file.read_text().strip()


def test_model_proxy_rewrite_preserves_streaming_fields() -> None:
    proxy = _UpstreamProxyCore(_registry(), Path("/tmp/proxy.log"), request_deadline_seconds=1.0)

    rewritten = proxy._rewrite_request_model(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"Content-Type": "application/json"},
            json.dumps(
                {
                    "model": "demo-model",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
            ).encode(),
        ),
        "provider/demo-model",
    )

    body = json.loads(rewritten.body.decode())
    assert body["model"] == "provider/demo-model"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_model_proxy_routes_streaming_request_to_requested_model(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    proxy = _UpstreamProxyCore(_multi_registry(), log_file, request_deadline_seconds=1.0)

    requested = {}

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return b'{"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}'

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, host, *args, **kwargs):
            requested["host"] = host

        def request(self, method, path, body=None, headers=None):
            requested["method"] = method
            requested["path"] = path
            requested["body"] = json.loads(body.decode())
            requested["headers"] = headers

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"Content-Type": "application/json"},
            json.dumps({"model": "builder", "stream": True}).encode(),
        )
    )

    assert response.status == 200
    assert requested["host"] == "builder.example.com"
    assert requested["path"] == "/v1/chat/completions"
    assert requested["body"]["model"] == "provider/builder"
    assert requested["body"]["stream"] is True
    assert requested["headers"]["Authorization"] == "Bearer builder-key"
    assert proxy.usage_records[0]["configured_model"] == "builder"


def test_model_proxy_records_usage_from_event_stream_response(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    usage_records_file = tmp_path / "usage.jsonl"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0, usage_records_file=usage_records_file)

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return (
                b'data: {"id":"chunk-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'
                b'data: {"id":"chunk-2","object":"chat.completion.chunk","usage":{"prompt_tokens":12,"completion_tokens":4,"total_tokens":16,"prompt_tokens_details":{"cached_tokens":2},"completion_tokens_details":{"reasoning_tokens":1}}}\n\n'
                b'data: [DONE]\n'
            )

        def getheaders(self):
            return [("Content-Type", "text/event-stream; charset=utf-8")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"Content-Type": "application/json"},
            json.dumps({"model": "demo-model", "stream": True}).encode(),
        )
    )

    assert response.status == 200
    assert proxy.usage_records[0]["usage_status"] == "reported"
    assert proxy.usage_records[0]["usage"]["input_tokens_standard"] == 10
    assert proxy.usage_records[0]["usage"]["input_tokens_cache_read"] == 2
    assert proxy.usage_records[0]["usage"]["reasoning_tokens"] == 1
    assert usage_records_file.read_text().strip()


def test_model_proxy_records_usage_from_responses_event_stream(monkeypatch, tmp_path: Path) -> None:
    """Responses API streams usage nested under `response` in the terminal event.

    Earlier lifecycle events (e.g. `response.created`) also carry a `response`
    object but with a null usage; those must be ignored so the recorded usage
    comes from `response.completed`.
    """
    log_file = tmp_path / "model_proxy.log"
    usage_records_file = tmp_path / "usage.jsonl"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0, usage_records_file=usage_records_file)

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return (
                b'event: response.created\n'
                b'data: {"type":"response.created","response":{"id":"resp-1","usage":null},"sequence_number":0}\n\n'
                b'event: response.output_text.delta\n'
                b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                b'event: response.completed\n'
                b'data: {"type":"response.completed","response":{"id":"resp-1","usage":{"input_tokens":12,'
                b'"input_tokens_details":{"cached_tokens":2},"output_tokens":4,'
                b'"output_tokens_details":{"reasoning_tokens":1},"total_tokens":16}},"sequence_number":7}\n\n'
            )

        def getheaders(self):
            return [("Content-Type", "text/event-stream; charset=utf-8")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/responses",
            {"Content-Type": "application/json"},
            json.dumps({"model": "demo-model", "stream": True}).encode(),
        )
    )

    assert response.status == 200
    assert proxy.usage_records[0]["usage_status"] == "reported"
    assert proxy.usage_records[0]["usage"]["input_tokens_standard"] == 10
    assert proxy.usage_records[0]["usage"]["input_tokens_cache_read"] == 2
    assert proxy.usage_records[0]["usage"]["output_tokens"] == 4
    assert proxy.usage_records[0]["usage"]["reasoning_tokens"] == 1
    assert usage_records_file.read_text().strip()


def test_model_proxy_records_usage_from_anthropic_messages_event_stream(monkeypatch, tmp_path: Path) -> None:
    """Anthropic Messages streaming splits one request's usage across events.

    `message_start` carries the prompt and cache counts (nested under `message`)
    while the final output count only arrives on `message_delta`. Taking any
    single event would undercount: `message_start` reports output_tokens 1.
    The bytes below are a real `aws-gov.claude-opus-5` response, trimmed to the
    usage-bearing events.
    """
    log_file = tmp_path / "model_proxy.log"
    usage_records_file = tmp_path / "usage.jsonl"
    proxy = _UpstreamProxyCore(_anthropic_registry(), log_file, request_deadline_seconds=1.0, usage_records_file=usage_records_file)

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return (
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg_1","role":"assistant",'
                b'"usage":{"input_tokens":2,"cache_creation_input_tokens":301,'
                b'"cache_read_input_tokens":29609,"cache_creation":'
                b'{"ephemeral_5m_input_tokens":301,"ephemeral_1h_input_tokens":0},'
                b'"output_tokens":1,"service_tier":"standard"}}}\n\n'
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"hello"}}\n\n'
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                b'"usage":{"input_tokens":2,"cache_creation_input_tokens":301,'
                b'"cache_read_input_tokens":29609,"output_tokens":291,'
                b'"output_tokens_details":{"thinking_tokens":128}}}\n\n'
                b'event: message_stop\n'
                b'data: {"type":"message_stop","usage":{"input_tokens":2,"output_tokens":291,'
                b'"cache_read_input_tokens":29609,"cache_creation_input_tokens":301}}\n\n'
            )

        def getheaders(self):
            return [("Content-Type", "text/event-stream; charset=utf-8")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/v1/messages?beta=true",
            {"Content-Type": "application/json"},
            json.dumps({"model": "demo-model", "stream": True}).encode(),
        )
    )

    assert response.status == 200
    usage = proxy.usage_records[0]
    assert usage["usage_status"] == "reported"
    assert usage["usage"]["input_tokens_standard"] == 2
    assert usage["usage"]["input_tokens_cache_read"] == 29609
    assert usage["usage"]["input_tokens_cache_write"] == 301
    # From message_delta, not the output_tokens 1 of message_start.
    assert usage["usage"]["output_tokens"] == 291
    assert usage["usage"]["reasoning_tokens"] == 128
    assert usage["usage"]["cache_write_ttl"] == "5m"
    assert usage_records_file.read_text().strip()


def test_model_proxy_records_usage_from_gzip_encoded_json(monkeypatch, tmp_path: Path) -> None:
    import gzip

    log_file = tmp_path / "model_proxy.log"
    usage_records_file = tmp_path / "usage.jsonl"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0, usage_records_file=usage_records_file)

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return gzip.compress(
                json.dumps(
                    {
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 4,
                            "total_tokens": 16,
                            "prompt_tokens_details": {"cached_tokens": 2},
                            "completion_tokens_details": {"reasoning_tokens": 1},
                        }
                    }
                ).encode()
            )

        def getheaders(self):
            return [("Content-Type", "application/json"), ("Content-Encoding", "gzip")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"Content-Type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert response.status == 200
    assert proxy.usage_records[0]["usage_status"] == "reported"
    assert proxy.usage_records[0]["usage"]["input_tokens_standard"] == 10


def test_model_proxy_prints_usage_warning_for_unparseable_success_response(monkeypatch, tmp_path: Path) -> None:
    log_file = tmp_path / "model_proxy.log"
    proxy = _UpstreamProxyCore(_registry(), log_file, request_deadline_seconds=1.0)
    proxy.stream_errors = True

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return b"not json"

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    stdout = io.StringIO()
    monkeypatch.setattr("automaton.proxy.http.client.HTTPSConnection", FakeConnection)
    monkeypatch.setattr("sys.stdout", stdout)

    response = proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"Content-Type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert response.status == 200
    assert proxy.usage_records[0]["usage_status"] == "missing"
    assert "[model-proxy][usage-warning]" in stdout.getvalue()
    assert "could not parse usage response" in stdout.getvalue()


def _capturing_connection(captured: dict):
    """Return a fake HTTPSConnection that records the headers it is handed."""

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return b"{}"

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, body=None, headers=None):
            captured.clear()
            captured.update(headers or {})

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    return FakeConnection


def test_model_proxy_replaces_client_authorization_header(monkeypatch, tmp_path: Path) -> None:
    """A client-sent Authorization header must not be forwarded alongside ours.

    HTTP header names are case-insensitive. Setting ``Authorization`` without
    removing a lowercase ``authorization`` produces a request with two auth
    headers, which load balancers reject with an opaque HTML 400 before the
    model sees it. reqwest-based clients (including the Codex CLI) always send
    the lowercase form.
    """
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {"authorization": "Bearer client-token", "content-type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    auth_headers = [key for key in captured if key.lower() == "authorization"]
    assert auth_headers == ["Authorization"]
    assert captured["Authorization"] == "Bearer key"
    assert "client-token" not in json.dumps(captured)


@pytest.mark.parametrize("header_name", ["authorization", "Authorization", "AUTHORIZATION"])
def test_model_proxy_overrides_authorization_in_any_case(
    monkeypatch, tmp_path: Path, header_name: str
) -> None:
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {header_name: "Bearer client-token"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert [key for key in captured if key.lower() == "authorization"] == ["Authorization"]
    assert captured["Authorization"] == "Bearer key"


def test_model_proxy_preserves_other_client_headers(monkeypatch, tmp_path: Path) -> None:
    """Only auth and hop-by-hop headers are dropped.

    The upstream WAF can require client headers such as User-Agent, so the proxy
    must not strip anything beyond what it explicitly replaces.
    """
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/chat/completions",
            {
                "authorization": "Bearer client-token",
                "user-agent": "codex_exec/0.155.1",
                "originator": "codex_exec",
                "accept": "text/event-stream",
                "connection": "keep-alive",
            },
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert captured["user-agent"] == "codex_exec/0.155.1"
    assert captured["originator"] == "codex_exec"
    assert captured["accept"] == "text/event-stream"
    # Hop-by-hop headers are still dropped.
    assert not [key for key in captured if key.lower() == "connection"]


def test_model_proxy_authenticates_anthropic_messages_with_x_api_key(
    monkeypatch, tmp_path: Path
) -> None:
    """The Anthropic Messages API authenticates with x-api-key, not Bearer.

    Sending `Authorization: Bearer` to /v1/messages returns a bare 401 that is
    indistinguishable from an invalid credential, so this is keyed off the
    configured API style rather than discovered at runtime.
    """
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_anthropic_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/messages",
            {"content-type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert captured["x-api-key"] == "key"
    assert captured["anthropic-version"] == "2023-06-01"
    # The Bearer scheme must not also be sent: two credentials invite the
    # gateway to authenticate against the wrong one.
    assert not [key for key in captured if key.lower() == "authorization"]


@pytest.mark.parametrize("header_name", ["x-api-key", "X-API-Key", "X-Api-Key"])
def test_model_proxy_replaces_client_x_api_key_in_any_case(
    monkeypatch, tmp_path: Path, header_name: str
) -> None:
    """A client-sent x-api-key must be replaced, not duplicated.

    Header names are case-insensitive, so leaving a differently-cased client
    header in place would send two x-api-key values upstream.
    """
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_anthropic_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/messages",
            {header_name: "client-secret", "content-type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert [key for key in captured if key.lower() == "x-api-key"] == ["x-api-key"]
    assert captured["x-api-key"] == "key"
    assert "client-secret" not in json.dumps(captured)


def test_model_proxy_honors_client_anthropic_version(monkeypatch, tmp_path: Path) -> None:
    """A client that pins an API version keeps it; the proxy only fills a default."""
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_anthropic_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/messages",
            {"anthropic-version": "2024-10-22"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert captured["anthropic-version"] == "2024-10-22"


def test_model_proxy_rejects_messages_path_without_anthropic_api_style(
    monkeypatch, tmp_path: Path
) -> None:
    """A Messages request from a model that does not declare the style is refused.

    Authenticating this on a guess would send Bearer to an API that requires
    x-api-key, surfacing as an opaque 401. Failing loudly names the real cause.
    """
    proxy = _UpstreamProxyCore(_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection({})
    )

    with pytest.raises(ValueError, match="anthropic_messages"):
        proxy.forward(
            ProxyRequest(
                "POST",
                "/messages",
                {},
                json.dumps({"model": "demo-model"}).encode(),
            )
        )


def test_model_proxy_authenticates_anthropic_count_tokens_with_x_api_key(
    monkeypatch, tmp_path: Path
) -> None:
    """/messages/count_tokens is part of the Messages API and must be accepted.

    Claude Code calls it (with ?beta=true) before sending large tool results.
    Rejecting it killed the proxy bridge mid-run and every such run ended as
    HARNESS_ERROR.
    """
    captured: dict[str, str] = {}
    proxy = _UpstreamProxyCore(_anthropic_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection(captured)
    )

    proxy.forward(
        ProxyRequest(
            "POST",
            "/messages/count_tokens?beta=true",
            {"content-type": "application/json"},
            json.dumps({"model": "demo-model"}).encode(),
        )
    )

    assert captured["x-api-key"] == "key"
    assert captured["anthropic-version"] == "2023-06-01"
    assert not [key for key in captured if key.lower() == "authorization"]


@pytest.mark.parametrize(
    "path",
    ["/chat/completions", "/responses", "/messages/batches", "/count_tokens"],
)
def test_model_proxy_rejects_anthropic_style_on_non_messages_path(
    monkeypatch, tmp_path: Path, path: str
) -> None:
    """An Anthropic-configured model must not send its credential elsewhere.

    The allow-list is exact: only /messages and /messages/count_tokens.
    """
    proxy = _UpstreamProxyCore(_anthropic_registry(), tmp_path / "model_proxy.log")
    monkeypatch.setattr(
        "automaton.proxy.http.client.HTTPSConnection", _capturing_connection({})
    )

    with pytest.raises(ValueError, match="not an Anthropic Messages endpoint"):
        proxy.forward(
            ProxyRequest(
                "POST",
                path,
                {},
                json.dumps({"model": "demo-model"}).encode(),
            )
        )


def test_model_proxy_rejects_unknown_api_style_at_construction() -> None:
    """An unrecognized api style fails at startup, not mid-run.

    The proxy cannot authenticate a style it does not know, and discovering that
    after an agent has been provisioned wastes the whole attempt.
    """
    registry = _registry()
    registry["demo-model"]["api"] = "grpc"

    with pytest.raises(ValueError, match="unsupported api"):
        _UpstreamProxyCore(registry, Path("/dev/null"))
