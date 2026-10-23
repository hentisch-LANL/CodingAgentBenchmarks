"""Usage normalization, accounting, and aggregation for Automaton."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


TOKEN_KEYS = [
    "input_tokens_standard",
    "input_tokens_cache_read",
    "input_tokens_cache_write",
    "output_tokens",
    "reasoning_tokens",
    "total_input_tokens",
    "total_tokens",
]

COST_KEYS = [
    "input_tokens_standard_cost",
    "input_tokens_cache_read_cost",
    "input_tokens_cache_write_cost",
    "output_cost",
    "total_cost",
]


def _coerce_int(value: Any, *, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("malformed usage object: missing required token counts")
        return default
    if isinstance(value, bool):
        raise ValueError("malformed usage object: boolean is not a token count")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed usage object: invalid token count {value!r}") from exc
    if isinstance(value, float) and number != value:
        raise ValueError(f"malformed usage object: invalid token count {value!r}")
    return number


def _extract_cache_write_ttl(response_json: dict[str, Any], usage: dict[str, Any]) -> str | None:
    for source in (response_json, usage):
        if not isinstance(source, dict):
            continue
        value = source.get("cache_write_ttl")
        if isinstance(value, str) and value.strip():
            return value.strip()
        prompt_cache_retention = source.get("prompt_cache_retention")
        if isinstance(prompt_cache_retention, str) and prompt_cache_retention.strip():
            return prompt_cache_retention.strip()
    return None


def _infer_anthropic_cache_write_ttl(usage: dict[str, Any]) -> str | None:
    """Infer the cache-write TTL from Anthropic's per-TTL breakdown.

    The Messages API reports cache writes split by retention window under
    ``cache_creation`` (``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``),
    which are priced differently. The TTL is only unambiguous when exactly one
    window was written to; when a single request writes to both, no single TTL
    describes it, so ``None`` is returned and the caller falls back to the
    default cache-write rate rather than guessing.
    """
    cache_creation = usage.get("cache_creation")
    if not isinstance(cache_creation, dict):
        return None

    written: list[str] = []
    for key, ttl in (("ephemeral_5m_input_tokens", "5m"), ("ephemeral_1h_input_tokens", "1h")):
        if _coerce_int(cache_creation.get(key), default=0) > 0:
            written.append(ttl)

    if len(written) == 1:
        return written[0]
    return None


def _endpoint_path(endpoint: str) -> str:
    """Return an endpoint's path, without any query string or fragment.

    Clients may append query parameters to an API path (the Claude Code CLI
    requests ``/v1/messages?beta=true``), which would otherwise defeat the
    suffix matching used to identify the API shape.
    """
    return endpoint.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def normalize_usage(endpoint: str, response_json: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a provider's usage object into Automaton's token buckets.

    The API shape is selected from the request path, ignoring any query string.
    Four shapes are recognized:

    - **OpenAI Responses** (``/responses``) and **Chat Completions**
      (``/chat/completions``). Cache reads are counted *inside* the prompt token
      total, so they are subtracted to get the uncached remainder.
    - **Anthropic Messages** (``/messages``). Cache reads and writes are
      reported as buckets *separate* from ``input_tokens``, so they are added
      instead. There is no total, so it is derived; thinking tokens are a subset
      of ``output_tokens``, matching how they are billed. The cache-write TTL is
      inferred from the per-window ``cache_creation`` breakdown when
      unambiguous, since the windows are priced differently.
    - **Bedrock-style camelCase** (``inputTokens``/``outputTokens``), matched on
      the payload shape rather than the path.

    Parameters
    ----------
    endpoint : str
        Request path as seen by the proxy, e.g. ``/v1/messages?beta=true``.
    response_json : dict[str, Any]
        Parsed response body, or for a streamed response the payload assembled
        by the proxy's event-stream decoder.

    Returns
    -------
    dict[str, Any] | None
        Normalized token buckets, or ``None`` when the response carries no
        usage object or the shape is not recognized (accounted as
        ``not_applicable`` rather than as an error).

    Raises
    ------
    ValueError
        If a usage object is present but internally inconsistent, e.g. missing
        required counts, negative counts, or cache reads exceeding the input.

    Examples
    --------
    >>> normalize_usage(
    ...     "/v1/messages",
    ...     {"usage": {"input_tokens": 10, "output_tokens": 4}},
    ... )["total_tokens"]
    14
    """
    usage = response_json.get("usage")
    if not usage:
        return None
    if not isinstance(usage, dict):
        raise ValueError("malformed usage object: usage must be a mapping")

    cache_write_ttl = _extract_cache_write_ttl(response_json, usage)
    endpoint_path = _endpoint_path(endpoint)

    if endpoint_path.endswith("/responses"):
        input_tokens = _coerce_int(usage.get("input_tokens"))
        output_tokens = _coerce_int(usage.get("output_tokens"))
        total_tokens = _coerce_int(usage.get("total_tokens"))
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        cache_read_tokens = _coerce_int(input_details.get("cached_tokens"), default=0)
        cache_write_tokens = _coerce_int(input_details.get("cache_write_tokens"), default=0)
        reasoning_tokens = _coerce_int(output_details.get("reasoning_tokens"), default=0)

        # OpenAI counts cache reads inside `input_tokens`, so the uncached
        # remainder is the difference.
        input_tokens_standard = input_tokens - cache_read_tokens
        total_input_tokens = input_tokens + cache_write_tokens
        cache_keys_reported = "cached_tokens" in input_details or "cache_write_tokens" in input_details
    elif endpoint_path.endswith("/chat/completions"):
        input_tokens = _coerce_int(usage.get("prompt_tokens"))
        output_tokens = _coerce_int(usage.get("completion_tokens"))
        total_tokens = _coerce_int(usage.get("total_tokens"))
        input_details = usage.get("prompt_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or {}
        cache_read_tokens = _coerce_int(input_details.get("cached_tokens"), default=0)
        cache_write_tokens = _coerce_int(input_details.get("cache_write_tokens"), default=0)
        reasoning_tokens = _coerce_int(output_details.get("reasoning_tokens"), default=0)

        input_tokens_standard = input_tokens - cache_read_tokens
        total_input_tokens = input_tokens + cache_write_tokens
        cache_keys_reported = "cached_tokens" in input_details or "cache_write_tokens" in input_details
    elif endpoint_path.endswith("/messages"):
        # Anthropic Messages API. Unlike OpenAI, `input_tokens` counts only the
        # uncached prompt: cache reads and cache writes are reported as separate,
        # non-overlapping buckets, so they are added rather than subtracted.
        # There is no `total_tokens` field, so it is derived.
        input_tokens_standard = _coerce_int(usage.get("input_tokens"))
        cache_read_tokens = _coerce_int(usage.get("cache_read_input_tokens"), default=0)
        cache_write_tokens = _coerce_int(usage.get("cache_creation_input_tokens"), default=0)
        output_tokens = _coerce_int(usage.get("output_tokens"))
        output_details = usage.get("output_tokens_details") or {}
        # Extended thinking is billed as output; Anthropic reports it as a
        # subset of `output_tokens` rather than as an extra bucket.
        reasoning_tokens = _coerce_int(output_details.get("thinking_tokens"), default=0)

        total_input_tokens = input_tokens_standard + cache_read_tokens + cache_write_tokens
        total_tokens = total_input_tokens + output_tokens
        cache_keys_reported = (
            "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
        )
        if cache_write_ttl is None:
            cache_write_ttl = _infer_anthropic_cache_write_ttl(usage)
    elif all(key in usage for key in ["inputTokens", "outputTokens"]):
        input_tokens_standard = _coerce_int(usage.get("inputTokens"))
        cache_read_tokens = _coerce_int(usage.get("cacheReadInputTokens"), default=0)
        cache_write_tokens = _coerce_int(usage.get("cacheWriteInputTokens"), default=0)
        output_tokens = _coerce_int(usage.get("outputTokens"))
        total_input_tokens = input_tokens_standard + cache_read_tokens + cache_write_tokens
        total_tokens = _coerce_int(usage.get("totalTokens"), default=total_input_tokens + output_tokens)
        reasoning_tokens = _coerce_int(usage.get("reasoningTokens"), default=0)
        cache_keys_reported = False
    else:
        return None

    if input_tokens_standard < 0:
        raise ValueError("malformed usage object: cached input exceeds input")

    normalized = {
        "input_tokens_standard": input_tokens_standard,
        "input_tokens_cache_read": cache_read_tokens,
        "input_tokens_cache_write": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_input_tokens": total_input_tokens,
        "total_tokens": total_tokens,
        "raw_usage": usage,
        "cache_write_ttl": cache_write_ttl,
        "schema_anomaly": total_tokens != total_input_tokens + output_tokens,
        "cache_detail_available": (
            cache_read_tokens > 0
            or cache_write_tokens > 0
            or cache_keys_reported
            or "cacheWriteInputTokens" in usage
            or "cacheReadInputTokens" in usage
        ),
    }

    for key in TOKEN_KEYS:
        if normalized[key] < 0:
            raise ValueError("malformed usage object: negative token counts")
    if normalized["reasoning_tokens"] > normalized["output_tokens"]:
        raise ValueError("malformed usage object: reasoning exceeds output")

    return normalized


def compute_cost(normalized_usage: dict[str, Any], pricing: dict[str, Any] | None) -> dict[str, Any]:
    if not pricing:
        return {
            "currency": "USD",
            "input_tokens_standard_cost": 0.0,
            "input_tokens_cache_read_cost": 0.0,
            "input_tokens_cache_write_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
        }

    cache_write_ttl = normalized_usage.get("cache_write_ttl")
    cache_write_rates = dict(pricing.get("cache_write_input_per_token_by_ttl", {}) or {})
    cache_write_rate = float(cache_write_rates.get(str(cache_write_ttl), pricing.get("cache_write_input_per_token", 0.0)) or 0.0)
    cache_read_rate = float(
        pricing.get("cache_read_input_per_token", pricing.get("cached_input_per_token", pricing["input_per_token"]))
    )

    input_tokens_standard_cost = normalized_usage["input_tokens_standard"] * float(pricing["input_per_token"])
    input_tokens_cache_read_cost = normalized_usage["input_tokens_cache_read"] * cache_read_rate
    input_tokens_cache_write_cost = normalized_usage["input_tokens_cache_write"] * cache_write_rate
    output_cost = normalized_usage["output_tokens"] * float(pricing["output_per_token"])
    return {
        "currency": pricing.get("currency", "USD"),
        "input_tokens_standard_cost": input_tokens_standard_cost,
        "input_tokens_cache_read_cost": input_tokens_cache_read_cost,
        "input_tokens_cache_write_cost": input_tokens_cache_write_cost,
        "output_cost": output_cost,
        "total_cost": input_tokens_standard_cost + input_tokens_cache_read_cost + input_tokens_cache_write_cost + output_cost,
    }


def aggregate_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "input_tokens_standard": 0,
        "input_tokens_cache_read": 0,
        "input_tokens_cache_write": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_input_tokens": 0,
        "total_tokens": 0,
        "input_tokens_standard_cost": 0.0,
        "input_tokens_cache_read_cost": 0.0,
        "input_tokens_cache_write_cost": 0.0,
        "output_cost": 0.0,
        "total_cost": 0.0,
        "duration_seconds": 0.0,
        "tokens_per_second": 0.0,
    }
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            **{key: 0 for key in TOKEN_KEYS},
            **{key: 0.0 for key in COST_KEYS},
            "requests": 0,
            "duration_seconds": 0.0,
            "tokens_per_second": 0.0,
        }
    )
    status_counts: dict[str, int] = defaultdict(int)
    endpoint_counts: dict[str, int] = defaultdict(int)

    for record in records:
        status = str(record.get("usage_status", "missing"))
        status_counts[status] += 1
        endpoint_counts[str(record.get("endpoint", ""))] += 1
        if status != "reported":
            continue

        usage = dict(record.get("usage", {}))
        cost = dict(record.get("cost", {}))
        model_name = str(record.get("configured_model", record.get("model", "unknown")))
        for key in TOKEN_KEYS:
            totals[key] += int(usage.get(key, 0) or 0)
            by_model[model_name][key] += int(usage.get(key, 0) or 0)
        for key in COST_KEYS:
            totals[key] += float(cost.get(key, 0.0) or 0.0)
            by_model[model_name][key] += float(cost.get(key, 0.0) or 0.0)
        duration = float(record.get("duration_seconds", 0.0) or 0.0)
        totals["duration_seconds"] += duration
        by_model[model_name]["duration_seconds"] += duration
        by_model[model_name]["requests"] += 1

    totals["tokens_per_second"] = (
        totals["output_tokens"] / totals["duration_seconds"] if totals["duration_seconds"] > 0 else 0.0
    )
    for model_totals in by_model.values():
        model_totals["tokens_per_second"] = (
            model_totals["output_tokens"] / model_totals["duration_seconds"]
            if model_totals["duration_seconds"] > 0
            else 0.0
        )

    return {
        "usage_available": status_counts.get("reported", 0) > 0,
        "totals": totals,
        "by_model": dict(sorted(by_model.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "endpoint_counts": dict(sorted(endpoint_counts.items())),
    }
