"""Loopback model proxy for Automaton.

A host-side broker forwards HTTP requests from a sandboxed harness to the
upstream model API while a sandbox-local HTTP proxy serves the harness on
sandbox localhost. This split design lets Automaton inject authentication
credentials, honor provider TLS configuration, and retry dropped upstream
requests within a bounded deadline without exposing the host network to the
sandbox.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import random
import ssl
import socket
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .usage import compute_cost, normalize_usage


MODEL_PROXY_ATTEMPT_TIMEOUT_SECONDS = 900.0
MODEL_PROXY_REQUEST_DEADLINE_SECONDS = 3600.0

#: API style that authenticates with Anthropic's ``x-api-key`` header and
#: requires a version header, rather than ``Authorization: Bearer``.
ANTHROPIC_MESSAGES_API = "anthropic_messages"

#: API styles the proxy knows how to authenticate. A model configured with any
#: other value is rejected rather than authenticated on a guess: sending the
#: wrong scheme upstream returns an opaque 401 that reads like a bad credential.
KNOWN_API_STYLES = frozenset({"chat_completions", "responses", ANTHROPIC_MESSAGES_API})

#: Version header required on every Anthropic Messages request. The API treats
#: this as mandatory, so the proxy supplies it rather than trusting the client.
ANTHROPIC_VERSION = "2023-06-01"

#: Request paths the Anthropic Messages API serves. Used to reject a model whose
#: declared API style contradicts the endpoint the harness actually called, which
#: would otherwise authenticate correctly against the wrong protocol. Claude Code
#: calls /messages/count_tokens before sending large tool results.
ANTHROPIC_MESSAGES_PATH_SUFFIXES = ("/messages", "/messages/count_tokens")

# Tool entries an agent may declare that are executed by the *model provider*
# rather than by the agent inside the sandbox.
#
# This distinction is a security boundary, not a stylistic one. A locally
# executed tool (OpenAI "function" / Anthropic "custom") runs in the container:
# the sandbox network namespace confines it and its invocation is visible in the
# harness transcript. A provider-executed tool is performed by the model's own
# infrastructure *inside the single HTTP response* this proxy forwards. Nothing
# egresses from the container, so neither the network namespace nor this proxy's
# per-request logging reveals that a fetch happened at all.
#
# For a benchmark that may run export-controlled code, that is an untraceable
# data-egress path: `web_search` can retrieve remote content, and anything the
# model was given can end up in a provider-side search query. These are
# therefore rejected unless a caller opts in explicitly.
#
# Names are the wire `type` values, matched case-insensitively.
PROVIDER_EXECUTED_TOOL_TYPES = frozenset(
    {
        # OpenAI Responses API native tools
        "web_search",
        "web_search_preview",
        "web_search_2025_08_26",
        "code_interpreter",
        "file_search",
        "computer_use",
        "computer_use_preview",
        "image_generation",
        "local_shell",
        "mcp",
        # Anthropic server-side tools
        "web_search_20250305",
        "web_fetch_20250910",
        "code_execution_20250522",
        "bash_20250124",
    }
)

# Tool `type` values that are executed locally by the agent and are therefore
# always permitted. Anything not in this set and not obviously a local function
# is reported rather than silently allowed, so a new provider-side tool type
# fails closed instead of slipping through.
LOCALLY_EXECUTED_TOOL_TYPES = frozenset({"function", "custom", "namespace"})


class ProviderExecutedToolBlocked(RuntimeError):
    """Raised when a request declares a provider-executed tool that is not allowed.

    See :data:`PROVIDER_EXECUTED_TOOL_TYPES` for why these are blocked by
    default.
    """

    def __init__(self, tool_types: list[str], detail: str):
        self.tool_types = tool_types
        self.detail = detail
        super().__init__(detail)


def find_provider_executed_tools(body: Any) -> list[str]:
    """Return the provider-executed tool types declared in a request body.

    Recurses into Responses-API ``namespace`` tools, which nest a ``tools`` list.
    Unknown tool types are reported too: a tool that is neither a recognized
    local type nor a known provider type is treated as suspect so that new
    provider-side capabilities fail closed rather than passing silently.

    Parameters
    ----------
    body : Any
        Parsed JSON request body. Non-dict input yields an empty list.

    Returns
    -------
    list[str]
        Sorted, de-duplicated tool types that are not locally executed.

    Examples
    --------
    >>> find_provider_executed_tools({"tools": [{"type": "web_search"}]})
    ['web_search']
    >>> find_provider_executed_tools({"tools": [{"type": "function", "name": "ls"}]})
    []
    """
    if not isinstance(body, dict):
        return []

    found: set[str] = set()

    def walk(tools: Any) -> None:
        if not isinstance(tools, list):
            return
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            raw_type = tool.get("type")
            tool_type = str(raw_type).strip().lower() if raw_type is not None else ""

            if tool_type == "namespace":
                # Namespaces nest their own tool list; recurse into it.
                walk(tool.get("tools"))
                continue

            if tool_type in LOCALLY_EXECUTED_TOOL_TYPES:
                continue

            if tool_type in PROVIDER_EXECUTED_TOOL_TYPES:
                found.add(tool_type)
                continue

            # Unrecognized type. A locally executed tool carries a callable
            # signature (a name plus parameters/input schema); a provider-side
            # capability is typically just a bare type. Treat anything without a
            # local call signature as provider-executed so new native tools fail
            # closed.
            has_local_signature = "name" in tool and (
                "parameters" in tool or "input_schema" in tool
            )
            if not has_local_signature:
                found.add(tool_type or "<missing type>")

    walk(body.get("tools"))
    # Chat Completions nests the callable under "function"; those are local and
    # already skipped by type == "function" above.
    return sorted(found)



class EndpointRetryExhausted(RuntimeError):
    """Raised when the model proxy cannot obtain a complete upstream response."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ProxyRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes


def _write_frame(sock: socket.socket, payload: bytes) -> None:
    header = struct.pack("!Q", len(payload))
    sock.sendall(header)
    sock.sendall(payload)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("unexpected EOF on proxy socket")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_frame(sock: socket.socket) -> bytes:
    size = struct.unpack("!Q", _read_exact(sock, 8))[0]
    return _read_exact(sock, size)


def _encode_request(request: ProxyRequest) -> bytes:
    envelope = {
        "method": request.method,
        "path": request.path,
        "headers": request.headers,
        "body": request.body.decode("latin1"),
    }
    return json.dumps(envelope).encode()


def _decode_request(payload: bytes) -> ProxyRequest:
    envelope = json.loads(payload.decode())
    return ProxyRequest(
        method=str(envelope["method"]),
        path=str(envelope["path"]),
        headers={str(key): str(value) for key, value in dict(envelope.get("headers", {})).items()},
        body=str(envelope.get("body", "")).encode("latin1"),
    )


def _encode_response(response: ProxyResponse) -> bytes:
    envelope = {
        "status": response.status,
        "reason": response.reason,
        "headers": response.headers,
        "body": response.body.decode("latin1"),
    }
    return json.dumps(envelope).encode()


def _decode_response(payload: bytes) -> ProxyResponse:
    envelope = json.loads(payload.decode())
    return ProxyResponse(
        status=int(envelope["status"]),
        reason=str(envelope["reason"]),
        headers={str(key): str(value) for key, value in dict(envelope.get("headers", {})).items()},
        body=str(envelope.get("body", "")).encode("latin1"),
    )


class _UpstreamProxyCore:
    """Forward buffered HTTP requests to the configured upstream model API."""

    def __init__(
        self,
        model_registry: dict[str, dict[str, Any]],
        log_file: Path,
        request_deadline_seconds: float = 900.0,
        usage_records_file: Path | None = None,
        allowed_provider_executed_tools: frozenset[str] | set[str] | None = None,
    ):
        self.model_registry = {}
        for model_name, config in model_registry.items():
            upstream = urlsplit(str(config["base_url"]).rstrip("/"))
            if upstream.scheme not in {"http", "https"} or not upstream.netloc:
                raise ValueError(f"Invalid model base_url: {config['base_url']}")
            api_style = config.get("api")
            if api_style is not None and str(api_style) not in KNOWN_API_STYLES:
                raise ValueError(
                    f"Model '{model_name}' declares unsupported api "
                    f"{str(api_style)!r}. The proxy must know the API style to "
                    "authenticate correctly; supported styles are "
                    f"{sorted(KNOWN_API_STYLES)}."
                )
            self.model_registry[model_name] = {**config, "_upstream": upstream}
        self.log_file = log_file
        self.last_error_status: int | None = None
        self.last_error_detail: str | None = None
        self.stream_errors = False
        self._last_reported_error: tuple[int | None, str | None] | None = None
        self.request_deadline_seconds = request_deadline_seconds
        self.endpoint_exhausted = False
        self.context_length_exceeded = False
        self.last_error_kind: str | None = None
        self.retry_count = 0
        self.attempt_count = 0
        self.usage_records: list[dict[str, Any]] = []
        self.usage_records_file = usage_records_file
        self.allowed_provider_executed_tools = frozenset(
            str(name).strip().lower() for name in (allowed_provider_executed_tools or ())
        )
        self.blocked_provider_executed_tools: list[str] = []

    def forward(self, request: ProxyRequest) -> ProxyResponse:
        self.endpoint_exhausted = False
        self.context_length_exceeded = False
        self.last_error_kind = None
        self.retry_count = 0
        self.attempt_count = 0
        self._enforce_provider_executed_tool_policy(request)
        target_model_name, target = self._resolve_target(request)
        request = self._rewrite_request_model(request, str(target.get("model", "")))
        upstream = target["_upstream"]
        upstream_path = self._upstream_path(request.path)
        headers = {}
        skip_headers = {
            "connection",
            "content-length",
            "host",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
        }
        for key, value in request.headers.items():
            if key.lower() in skip_headers:
                continue
            # The proxy supplies the upstream credential itself, so drop any
            # client-sent credential header rather than forwarding it alongside
            # ours. HTTP header names are case-insensitive, so a client that
            # sends lowercase "authorization" (reqwest, and hence the Codex CLI,
            # always does) would otherwise produce a request carrying two
            # Authorization headers. Load balancers reject that with an opaque
            # HTML 400 before the model ever sees the request. The same applies
            # to "x-api-key" on the Anthropic path.
            if key.lower() in {"authorization", "x-api-key"}:
                continue
            headers[key] = value
        headers["Host"] = upstream.netloc
        self._apply_upstream_auth(headers, target, target_model_name, upstream_path)
        if request.body:
            headers["Content-Length"] = str(len(request.body))

        start_time = time.monotonic()
        backoff_seconds = 1.0
        while True:
            elapsed = time.monotonic() - start_time
            remaining = self.request_deadline_seconds - elapsed
            if remaining <= 0:
                break

            self.attempt_count += 1
            attempt_timeout = min(max(remaining, 0.1), MODEL_PROXY_ATTEMPT_TIMEOUT_SECONDS)
            conn: http.client.HTTPConnection | http.client.HTTPSConnection
            if upstream.scheme == "https":
                conn = http.client.HTTPSConnection(
                    upstream.netloc,
                    timeout=attempt_timeout,
                    context=self._ssl_context(target.get("cert")),
                )
            else:
                conn = http.client.HTTPConnection(upstream.netloc, timeout=attempt_timeout)

            try:
                self._log(
                    f"{request.method} {request.path} attempt {self.attempt_count} -> {upstream.scheme}://{upstream.netloc}{upstream_path}"
                )
                attempt_start = time.monotonic()
                conn.request(request.method, upstream_path, body=request.body or None, headers=headers)
                response = conn.getresponse()
                response_body = response.read()
                attempt_duration = time.monotonic() - attempt_start
                response_headers = {}
                for key, value in response.getheaders():
                    if key.lower() not in skip_headers | {"content-length"}:
                        response_headers[key] = value
                self._log(f"{request.method} {request.path} <- {response.status} {response.reason}")
                if self.retry_count:
                    self._report_live_proxy_event(
                        f"Model proxy recovered after {self.retry_count} retr{'y' if self.retry_count == 1 else 'ies'} for {request.method} {request.path}"
                    )
                if response.status >= 400 and response_body:
                    error_detail = self._decode_response_body(response_body)
                    self.last_error_status = response.status
                    self.last_error_detail = error_detail
                    self.last_error_kind = "upstream_http_error"
                    if self._is_context_length_error(response.status, response_body):
                        self.context_length_exceeded = True
                        self.last_error_kind = "context_length_exceeded"
                    self._log(f"{request.method} {request.path} body: {self._truncate_log_text(error_detail)}")
                    self._report_live_error(
                        f"Model proxy upstream error {response.status} {response.reason}: {self._truncate_log_text(error_detail)}"
                    )
                elif response.status >= 400:
                    self.last_error_status = response.status
                    self.last_error_detail = f"{response.status} {response.reason}"
                    self.last_error_kind = "upstream_http_error"
                    self._report_live_error(f"Model proxy upstream error {response.status} {response.reason}")
                self._record_usage(
                    request=request,
                    response_status=response.status,
                    response_body=response_body,
                    response_headers=response_headers,
                    configured_model_name=target_model_name,
                    upstream_model_name=str(target.get("model", "")),
                    pricing=target.get("cost"),
                    duration_seconds=attempt_duration,
                )
                return ProxyResponse(
                    status=response.status,
                    reason=response.reason,
                    headers=response_headers,
                    body=response_body,
                )
            except Exception as exc:
                self.last_error_status = None
                self.last_error_kind = "endpoint_transport_error"
                self.last_error_detail = f"{type(exc).__name__}: {exc}"
                self._log(f"{request.method} {request.path} !! {type(exc).__name__}: {exc}")
                remaining = self.request_deadline_seconds - (time.monotonic() - start_time)
                if remaining <= 0:
                    break
                sleep_for = min(backoff_seconds + random.uniform(0.0, 0.25 * backoff_seconds), remaining)
                self.retry_count += 1
                self._log(
                    f"{request.method} {request.path} retry {self.retry_count} in {sleep_for:.2f}s after {type(exc).__name__}: {exc}"
                )
                self._report_live_proxy_event(
                    f"Model proxy retry {self.retry_count} for {request.method} {request.path} after {type(exc).__name__}: {exc} (sleep {sleep_for:.2f}s)"
                )
                self._record_usage(
                    request=request,
                    response_status=None,
                    response_body=None,
                    response_headers=None,
                    configured_model_name=target_model_name,
                    upstream_model_name=str(target.get("model", "")),
                    pricing=target.get("cost"),
                    usage_status="missing",
                    usage_missing_reason="http_error_or_incomplete_response",
                )
                time.sleep(sleep_for)
                backoff_seconds = min(backoff_seconds * 2.0, 30.0)
            finally:
                conn.close()

        self.endpoint_exhausted = True
        detail = (
            f"Model proxy could not obtain a complete upstream response within "
            f"{int(self.request_deadline_seconds)}s after {self.attempt_count} attempt"
            f"{'s' if self.attempt_count != 1 else ''}. Last error: {self.last_error_detail or 'unknown'}"
        )
        self.last_error_detail = detail
        self.last_error_kind = "endpoint_timeout"
        self._log(f"{request.method} {request.path} !! endpoint exhausted: {detail}")
        self._report_live_proxy_event(detail)
        raise EndpointRetryExhausted(detail)

    def _enforce_provider_executed_tool_policy(self, request: ProxyRequest) -> None:
        """Reject requests declaring provider-executed tools unless allowed.

        Runs before the upstream connection is opened, so a blocked request never
        reaches the provider. See :data:`PROVIDER_EXECUTED_TOOL_TYPES`.

        Raises
        ------
        ProviderExecutedToolBlocked
            If a disallowed provider-executed tool type is declared.
        """
        if not request.body:
            return
        try:
            body = json.loads(request.body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            # Not JSON we can inspect. Tool declarations only appear in JSON
            # bodies, so there is nothing to enforce here.
            return

        declared = find_provider_executed_tools(body)
        if not declared:
            return

        disallowed = [
            tool for tool in declared if tool not in self.allowed_provider_executed_tools
        ]
        if not disallowed:
            self._log(
                "[tool-policy] allowing explicitly permitted provider-executed "
                f"tool(s): {', '.join(declared)}"
            )
            return

        self.blocked_provider_executed_tools = disallowed
        self.last_error_kind = "provider_executed_tool_blocked"
        detail = (
            "Model proxy blocked a request declaring provider-executed tool(s): "
            f"{', '.join(disallowed)}. These run on the model provider's "
            "infrastructure, not inside the sandbox, so the sandbox network "
            "namespace cannot confine them and their activity does not appear in "
            "the harness transcript. Disable the tool in the harness, or allow it "
            "explicitly (allowed_provider_executed_tools) if this benchmark "
            "genuinely requires it."
        )
        self.last_error_detail = detail
        self._log(f"[tool-policy] BLOCKED {request.method} {request.path}: {detail}")
        self._report_live_error(detail)
        raise ProviderExecutedToolBlocked(disallowed, detail)

    def _apply_upstream_auth(
        self,
        headers: dict[str, str],
        target: dict[str, Any],
        model_name: str,
        upstream_path: str,
    ) -> None:
        """Set the upstream credential headers for the target's API style.

        The Anthropic Messages API authenticates with ``x-api-key`` and requires
        ``anthropic-version``; the OpenAI styles use ``Authorization: Bearer``.
        Sending the wrong scheme upstream returns a bare 401 that is
        indistinguishable from an invalid credential, so the style is taken from
        configuration rather than inferred, and a declared style that contradicts
        the requested path is rejected outright instead of being papered over.

        Parameters
        ----------
        headers : dict[str, str]
            Outgoing header map, mutated in place.
        target : dict[str, Any]
            Registry entry for the resolved model.
        model_name : str
            Registry key, used only in error messages.
        upstream_path : str
            Path this request will be sent to, without query string.

        Raises
        ------
        ValueError
            If the declared API style and the requested path disagree about
            whether this is an Anthropic Messages request.
        """
        api_style = target.get("api")
        api_style = str(api_style) if api_style is not None else None
        request_path = upstream_path.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        is_messages_path = request_path.endswith(ANTHROPIC_MESSAGES_PATH_SUFFIXES)

        if api_style == ANTHROPIC_MESSAGES_API:
            if not is_messages_path:
                raise ValueError(
                    f"Model '{model_name}' is configured as "
                    f"{ANTHROPIC_MESSAGES_API!r} but the harness requested "
                    f"{request_path!r}, which is not an Anthropic Messages "
                    "endpoint. Refusing to send Anthropic credentials to a "
                    "different protocol."
                )
            headers["x-api-key"] = str(target["api_key"])
            headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
            return

        if is_messages_path:
            declared = api_style if api_style is not None else "unset"
            raise ValueError(
                f"Model '{model_name}' requested the Anthropic Messages "
                f"endpoint {request_path!r} but declares api {declared!r}. Set "
                f"api: {ANTHROPIC_MESSAGES_API} so the proxy sends the "
                "x-api-key credential this API requires."
            )

        headers["Authorization"] = f"Bearer {target['api_key']}"

    def _upstream_path(self, request_path: str, upstream=None) -> str:
        upstream = upstream or next(iter(self.model_registry.values()))["_upstream"]
        base_path = upstream.path.rstrip("/")
        if not base_path:
            return request_path
        if request_path == base_path or request_path.startswith(f"{base_path}/"):
            return request_path
        if request_path.startswith("/"):
            return f"{base_path}{request_path}"
        return f"{base_path}/{request_path}"

    def _log(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with open(self.log_file, "a") as f:
            f.write(f"{timestamp} {message}\n")

    def _report_live_error(self, message: str) -> None:
        if not self.stream_errors:
            return
        state = (self.last_error_status, message)
        if self._last_reported_error == state:
            return
        self._last_reported_error = state
        sys.stderr.write(f"[model-proxy] {message}\n")
        sys.stderr.flush()

    def _report_live_proxy_event(self, message: str) -> None:
        if not self.stream_errors:
            return
        sys.stdout.write(f"[model-proxy] {message}\n")
        sys.stdout.flush()

    def _report_live_usage_warning(self, message: str) -> None:
        if not self.stream_errors:
            return
        sys.stdout.write(f"[model-proxy][usage-warning] {message}\n")
        sys.stdout.flush()

    def _truncate_log_text(self, text: str, limit: int = 500) -> str:
        text = text.replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    # Substrings that indicate the request exceeded the model's context window.
    # Matched case-insensitively against structured error codes/types and the
    # human-readable message so we catch the common provider variants.
    _CONTEXT_LENGTH_CODES = (
        "context_length_exceeded",
        "context_length",
        "context_window_exceeded",
        "string_above_max_length",
        "invalid_prompt",
    )
    _CONTEXT_LENGTH_PHRASES = (
        "context length",
        "context window",
        "maximum context",
        "max_tokens",
        "maximum number of tokens",
        "too many tokens",
        "prompt is too long",
        "input is too long",
        "reduce the length",
    )

    def _is_context_length_error(self, status: int, content: bytes) -> bool:
        """Return True when an upstream 4xx indicates context-window exhaustion.

        Providers signal an oversized prompt with an HTTP 400 (occasionally 413)
        and an error ``code``/``type`` such as ``context_length_exceeded`` or a
        message mentioning the model's maximum context length. Server errors
        (5xx) and auth/rate-limit errors are intentionally not matched here.
        """
        if status not in (400, 413, 422):
            return False
        text = content.decode(errors="replace")
        if not text.strip():
            return False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            fields: list[str] = []
            if isinstance(error, dict):
                for key in ("code", "type", "message"):
                    value = error.get(key)
                    if isinstance(value, str):
                        fields.append(value)
            for key in ("code", "type", "message"):
                value = data.get(key)
                if isinstance(value, str):
                    fields.append(value)
            lowered_fields = [f.lower() for f in fields]
            if any(code in f for f in lowered_fields for code in self._CONTEXT_LENGTH_CODES):
                return True
            if any(phrase in f for f in lowered_fields for phrase in self._CONTEXT_LENGTH_PHRASES):
                return True
            return False
        lowered = text.lower()
        return any(phrase in lowered for phrase in self._CONTEXT_LENGTH_PHRASES) or any(
            code in lowered for code in self._CONTEXT_LENGTH_CODES
        )

    def _decode_response_body(self, content: bytes) -> str:
        text = content.decode(errors="replace").strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return text

    def _ssl_context(self, cert_path: str | None) -> ssl.SSLContext:
        cafile = self._ca_bundle_path(cert_path)
        if cafile:
            return ssl.create_default_context(cafile=cafile)
        return ssl.create_default_context()

    def _ca_bundle_path(self, cert_path: str | None) -> str | None:
        for path in [
            cert_path,
            os.environ.get("REQUESTS_CA_BUNDLE"),
            os.environ.get("CURL_CA_BUNDLE"),
            os.environ.get("SSL_CERT_FILE"),
        ]:
            if path:
                return str(path)
        return None

    def _resolve_target(self, request: ProxyRequest) -> tuple[str, dict[str, Any]]:
        requested_model = ""
        try:
            body = json.loads(request.body.decode() or "{}")
            requested_model = str(body.get("model", ""))
        except Exception:
            requested_model = next(iter(self.model_registry.keys()))

        for model_name, config in self.model_registry.items():
            if requested_model in {model_name, str(config.get("model", "")), str(config.get("opencode_model", ""))}:
                return model_name, config
        first_name = next(iter(self.model_registry.keys()))
        return first_name, self.model_registry[first_name]

    def _rewrite_request_model(self, request: ProxyRequest, upstream_model_name: str) -> ProxyRequest:
        if not upstream_model_name:
            return request
        try:
            body = json.loads(request.body.decode() or "{}")
        except Exception:
            return request
        if not isinstance(body, dict) or "model" not in body:
            return request
        rewritten = dict(body)
        rewritten["model"] = upstream_model_name
        return ProxyRequest(
            method=request.method,
            path=request.path,
            headers=request.headers,
            body=json.dumps(rewritten).encode(),
        )

    def _record_usage(
        self,
        request: ProxyRequest,
        response_status: int | None,
        response_body: bytes | None,
        response_headers: dict[str, str] | None,
        configured_model_name: str,
        upstream_model_name: str,
        pricing: dict[str, Any] | None,
        usage_status: str | None = None,
        usage_missing_reason: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        endpoint = request.path
        record: dict[str, Any] = {
            "configured_model": configured_model_name,
            "model": upstream_model_name,
            "endpoint": endpoint,
            "stream": False,
            "http_status": response_status,
            "duration_seconds": duration_seconds,
        }
        if usage_status is not None:
            record["usage_status"] = usage_status
            if usage_missing_reason is not None:
                record["usage_missing_reason"] = usage_missing_reason
        elif response_status is None or response_body is None:
            record["usage_status"] = "missing"
            record["usage_missing_reason"] = "http_error_or_incomplete_response"
        else:
            try:
                response_json = self._decode_json_response(response_body, response_headers)
            except Exception as exc:
                record["usage_status"] = "missing"
                record["usage_missing_reason"] = "non_json_response"
                if response_headers:
                    self._log(
                        f"{request.method} {endpoint} usage parse headers: {self._truncate_log_text(json.dumps(response_headers, sort_keys=True))}"
                    )
                try:
                    preview = self._decode_response_preview(response_body, response_headers)
                except Exception as preview_exc:
                    preview = f"<preview failed: {type(preview_exc).__name__}: {preview_exc}>"
                self._log(f"{request.method} {endpoint} usage parse body preview: {self._truncate_log_text(preview)}")
                self._report_live_usage_warning(
                    f"could not parse usage response for {configured_model_name} {endpoint}: {type(exc).__name__}: {exc}"
                )
            else:
                try:
                    normalized = normalize_usage(endpoint, response_json)
                except Exception as exc:
                    record["usage_status"] = "malformed"
                    record["usage_missing_reason"] = str(exc)
                    record["raw_usage"] = response_json.get("usage")
                    self._report_live_usage_warning(
                        f"malformed usage payload for {configured_model_name} {endpoint}: {exc}"
                    )
                else:
                    if normalized is None:
                        record["usage_status"] = "not_applicable"
                    else:
                        record["usage_status"] = "reported"
                        record["usage"] = {key: normalized[key] for key in normalized if key != "raw_usage"}
                        record["raw_usage"] = normalized["raw_usage"]
                        record["cost"] = compute_cost(normalized, pricing)
                        if duration_seconds is not None and duration_seconds > 0:
                            record["tokens_per_second"] = normalized["output_tokens"] / duration_seconds
        self.usage_records.append(record)
        if self.usage_records_file is not None:
            with open(self.usage_records_file, "a") as f:
                json.dump(record, f)
                f.write("\n")

    def _decode_json_response(self, response_body: bytes, response_headers: dict[str, str] | None) -> dict[str, Any]:
        content = self._decode_response_content(response_body, response_headers)
        content_type = ""
        if response_headers:
            for key, value in response_headers.items():
                if key.lower() == "content-type":
                    content_type = value.lower().strip()
                    break

        if "text/event-stream" in content_type:
            return self._decode_event_stream_json(content.decode(errors="replace"))

        return json.loads(content.decode())

    def _decode_response_content(self, response_body: bytes, response_headers: dict[str, str] | None) -> bytes:
        content = response_body
        content_encoding = ""
        if response_headers:
            for key, value in response_headers.items():
                if key.lower() == "content-encoding":
                    content_encoding = value.lower().strip()
                    break

        if content_encoding in {"gzip", "x-gzip"}:
            content = gzip.decompress(content)
        elif content_encoding == "deflate":
            content = zlib.decompress(content)

        return content

    def _decode_response_preview(self, response_body: bytes, response_headers: dict[str, str] | None) -> str:
        content = self._decode_response_content(response_body, response_headers)
        return content.decode(errors="replace")

    # Anthropic Messages streaming events that carry usage. `message_start`
    # establishes the input/cache counts (nested under `message`) while the
    # output count is only final on `message_delta`/`message_stop`, so usage has
    # to be accumulated across events instead of taken from any single one.
    _ANTHROPIC_USAGE_EVENTS = frozenset({"message_start", "message_delta", "message_stop"})

    def _decode_event_stream_json(self, text: str) -> dict[str, Any]:
        usage_payload: dict[str, Any] | None = None
        final_payload: dict[str, Any] | None = None
        anthropic_usage: dict[str, Any] | None = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            payload = json.loads(data)
            if not isinstance(payload, dict):
                continue

            # Anthropic Messages streaming splits one request's usage across
            # several events: `message_start` reports the prompt and cache
            # counts, and the final output count arrives on `message_delta`
            # (with `message_stop` sometimes repeating it). Merge them so the
            # request is accounted once with complete counts. Later events
            # override earlier ones key by key, since the output count grows as
            # the response is generated.
            if payload.get("type") in self._ANTHROPIC_USAGE_EVENTS:
                event_usage = payload.get("usage")
                if not isinstance(event_usage, dict):
                    message = payload.get("message")
                    if isinstance(message, dict):
                        event_usage = message.get("usage")
                if isinstance(event_usage, dict):
                    anthropic_usage = {
                        **(anthropic_usage or {}),
                        **{key: value for key, value in event_usage.items() if value is not None},
                    }

            # Chat Completions streaming carries usage at the top level of the
            # final chunk. Responses API streaming carries usage nested under
            # the `response` object of the terminal `response.completed` event
            # (earlier lifecycle events include `response` but with a null
            # usage). Normalize both into a payload that exposes a top-level
            # `usage` key for `normalize_usage`.
            if isinstance(payload.get("usage"), dict):
                usage_payload = payload
            else:
                response = payload.get("response")
                if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                    usage_payload = response
            final_payload = payload

        if anthropic_usage is not None:
            return {"type": "message", "usage": anthropic_usage}
        if usage_payload is not None:
            return usage_payload
        if final_payload is not None:
            return final_payload
        raise json.JSONDecodeError("No JSON events found in event stream", text, 0)


class ModelProxy:
    """Loopback-only split proxy for sandboxed harness execution.

    A host-side broker forwards requests to the upstream model API while a
    sandbox-local HTTP proxy serves the harness on sandbox localhost.

    Parameters
    ----------
    model_registry : dict[str, dict[str, Any]]
        Mapping of configured model names to upstream connection details
    log_file : Path
        Path to log file for request/response logging
    request_deadline_seconds : float
        Total time budget for one upstream request including retries
    usage_records_file : Path | None
        Optional JSONL file to append per-request usage records to
    allowed_provider_executed_tools : Iterable[str] | None
        Provider-executed tool types to permit. Empty/None (the default) blocks
        all of them: they run on the provider's infrastructure rather than in the
        sandbox, so the sandbox network namespace cannot confine them and their
        activity is invisible to the harness transcript. See
        :data:`PROVIDER_EXECUTED_TOOL_TYPES`.

    Attributes
    ----------
    usage_records : list[dict[str, Any]]
        Recorded per-request usage accounting records

    Examples
    --------
    >>> registry = {"demo": {"base_url": "https://api.example.com/v1", "api_key": "key", "model": "demo/model"}}  # doctest: +SKIP
    >>> with ModelProxy(registry, Path("proxy.log")) as proxy:  # doctest: +SKIP
    ...     len(proxy.usage_records)
    0
    """

    def __init__(
        self,
        model_registry: dict[str, dict[str, Any]],
        log_file: Path,
        request_deadline_seconds: float = 900.0,
        usage_records_file: Path | None = None,
        allowed_provider_executed_tools: frozenset[str] | set[str] | None = None,
    ):
        self.core = _UpstreamProxyCore(
            model_registry,
            log_file,
            request_deadline_seconds=request_deadline_seconds,
            usage_records_file=usage_records_file,
            allowed_provider_executed_tools=allowed_provider_executed_tools,
        )
        self.log_file = log_file
        self._broker_thread: threading.Thread | None = None
        self._socketpair: tuple[socket.socket, socket.socket] | None = None
        self._broker_lock = threading.Lock()

    def __enter__(self) -> "ModelProxy":
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log(
            "[host-broker] starting proxy bridge for multi-model registry"
        )
        host_sock, sandbox_sock = socket.socketpair()
        self._socketpair = (host_sock, sandbox_sock)

        def broker_loop() -> None:
            try:
                while True:
                    with self._broker_lock:
                        payload = _read_frame(host_sock)
                        request = _decode_request(payload)
                        try:
                            response = self.core.forward(request)
                        except ProviderExecutedToolBlocked as exc:
                            # Answer the sandbox with a definitive refusal rather
                            # than letting this kill the broker thread, which
                            # would hang the agent on a socket that never
                            # replies. The agent sees a 403 and fails; the run is
                            # classified from blocked_provider_executed_tools.
                            body = json.dumps(
                                {
                                    "error": {
                                        "message": exc.detail,
                                        "type": "provider_executed_tool_blocked",
                                        "blocked_tools": exc.tool_types,
                                    }
                                }
                            ).encode()
                            response = ProxyResponse(
                                status=403,
                                reason="Forbidden",
                                headers={"Content-Type": "application/json"},
                                body=body,
                            )
                        _write_frame(host_sock, _encode_response(response))
            except EOFError:
                self._log("[host-broker] client disconnected")
            except OSError as exc:
                self._log(f"[host-broker] socket error: {type(exc).__name__}: {exc}")
            finally:
                host_sock.close()

        self._broker_thread = threading.Thread(target=broker_loop, daemon=True)
        self._broker_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._socketpair is not None:
            try:
                self._socketpair[1].close()
            except OSError:
                pass
            try:
                self._socketpair[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socketpair[0].close()
        if self._broker_thread is not None:
            self._broker_thread.join(timeout=5)
        self._log("[host-broker] stopped proxy bridge")

    @property
    def base_url(self) -> str:
        raise RuntimeError("Sandbox-local proxy URL is resolved during sandbox launch")

    @property
    def last_error_detail(self) -> str | None:
        return self.core.last_error_detail

    @property
    def endpoint_exhausted(self) -> bool:
        return self.core.endpoint_exhausted

    @property
    def context_length_exceeded(self) -> bool:
        return self.core.context_length_exceeded

    @property
    def last_error_kind(self) -> str | None:
        return self.core.last_error_kind

    @property
    def blocked_provider_executed_tools(self) -> list[str]:
        """Provider-executed tool types this proxy refused to forward."""
        return list(self.core.blocked_provider_executed_tools)

    @property
    def usage_records(self) -> list[dict[str, Any]]:
        return list(self.core.usage_records)

    @property
    def stream_errors(self) -> bool:
        return self.core.stream_errors

    @stream_errors.setter
    def stream_errors(self, value: bool) -> None:
        self.core.stream_errors = value

    def sandbox_socket(self) -> socket.socket:
        if self._socketpair is None:
            raise RuntimeError("Proxy bridge is not running")
        return self._socketpair[1]

    def _log(self, message: str) -> None:
        self.core._log(message)
