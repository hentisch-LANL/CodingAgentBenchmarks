"""Tests for automaton.usage (ported from abc_bench/tests/test_usage_accounting.py).

automaton.usage is a verbatim port of abc_bench.usage, so the only change is
the import path: ``abc_bench.usage`` -> ``automaton.usage``.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure the repo root is on sys.path (also covered by conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from automaton.usage import aggregate_usage, compute_cost, normalize_usage


def test_normalize_usage_for_chat_completions_tracks_cache_read_and_reasoning_tokens() -> None:
    normalized = normalize_usage(
        "/chat/completions",
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 5},
            }
        },
    )

    assert normalized == {
        "input_tokens_standard": 100,
        "input_tokens_cache_read": 20,
        "input_tokens_cache_write": 0,
        "output_tokens": 30,
        "reasoning_tokens": 5,
        "total_input_tokens": 120,
        "total_tokens": 150,
        "raw_usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 20},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
        "cache_write_ttl": None,
        "schema_anomaly": False,
        "cache_detail_available": True,
    }


def test_normalize_usage_for_responses_detects_explicit_cache_write_tokens() -> None:
    normalized = normalize_usage(
        "/responses",
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 25,
                "total_tokens": 145,
                "input_tokens_details": {
                    "cached_tokens": 40,
                    "cache_write_tokens": 20,
                },
                "output_tokens_details": {"reasoning_tokens": 1},
            },
            "cache_write_ttl": "5m",
        },
    )

    assert normalized == {
        "input_tokens_standard": 60,
        "input_tokens_cache_read": 40,
        "input_tokens_cache_write": 20,
        "output_tokens": 25,
        "reasoning_tokens": 1,
        "total_input_tokens": 120,
        "total_tokens": 145,
        "raw_usage": {
            "input_tokens": 100,
            "output_tokens": 25,
            "total_tokens": 145,
            "input_tokens_details": {
                "cached_tokens": 40,
                "cache_write_tokens": 20,
            },
            "output_tokens_details": {"reasoning_tokens": 1},
        },
        "cache_write_ttl": "5m",
        "schema_anomaly": False,
        "cache_detail_available": True,
    }


def test_normalize_usage_for_bedrock_style_usage_tracks_cache_write_bucket() -> None:
    normalized = normalize_usage(
        "/converse",
        {
            "usage": {
                "inputTokens": 100,
                "cacheReadInputTokens": 40,
                "cacheWriteInputTokens": 60,
                "outputTokens": 25,
                "totalTokens": 225,
            },
            "cache_write_ttl": "5m",
        },
    )

    assert normalized == {
        "input_tokens_standard": 100,
        "input_tokens_cache_read": 40,
        "input_tokens_cache_write": 60,
        "output_tokens": 25,
        "reasoning_tokens": 0,
        "total_input_tokens": 200,
        "total_tokens": 225,
        "raw_usage": {
            "inputTokens": 100,
            "cacheReadInputTokens": 40,
            "cacheWriteInputTokens": 60,
            "outputTokens": 25,
            "totalTokens": 225,
        },
        "cache_write_ttl": "5m",
        "schema_anomaly": False,
        "cache_detail_available": True,
    }


def test_normalize_usage_for_anthropic_messages_adds_cache_buckets_to_input() -> None:
    """Anthropic reports cache tokens as buckets separate from `input_tokens`.

    This is the opposite of OpenAI, where cache reads are counted *inside*
    `input_tokens` and must be subtracted out. The payload here is a real
    `aws-gov.claude-opus-5` response captured from a Claude Code run.
    """
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 301,
        "cache_read_input_tokens": 29609,
        "output_tokens": 291,
        "output_tokens_details": {"thinking_tokens": 128},
        "cache_creation": {
            "ephemeral_5m_input_tokens": 301,
            "ephemeral_1h_input_tokens": 0,
        },
        "service_tier": "standard",
    }

    normalized = normalize_usage("/v1/messages", {"usage": usage})

    assert normalized == {
        "input_tokens_standard": 2,
        "input_tokens_cache_read": 29609,
        "input_tokens_cache_write": 301,
        "output_tokens": 291,
        # Extended thinking is billed as output, so it is a subset of
        # output_tokens rather than an additional bucket.
        "reasoning_tokens": 128,
        "total_input_tokens": 29912,
        # The Messages API reports no total, so it is derived.
        "total_tokens": 30203,
        "raw_usage": usage,
        "cache_write_ttl": "5m",
        "schema_anomaly": False,
        "cache_detail_available": True,
    }


def test_normalize_usage_for_anthropic_messages_ignores_query_string() -> None:
    """The Claude Code CLI requests `/v1/messages?beta=true`.

    A query string must not prevent the endpoint from being recognized, or the
    whole run would be accounted as `not_applicable` and report zero tokens.
    """
    normalized = normalize_usage(
        "/v1/messages?beta=true",
        {"usage": {"input_tokens": 10, "output_tokens": 4}},
    )

    assert normalized is not None
    assert normalized["input_tokens_standard"] == 10
    assert normalized["output_tokens"] == 4
    assert normalized["total_tokens"] == 14
    assert normalized["cache_detail_available"] is False


def test_normalize_usage_for_anthropic_messages_infers_cache_write_ttl() -> None:
    """The cache-write TTL is priced differently per retention window.

    Anthropic does not label the TTL directly; it reports per-window cache-write
    counts under `cache_creation`. A single written window identifies the TTL.
    """
    normalized = normalize_usage(
        "/v1/messages",
        {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": 700,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 700,
                },
            }
        },
    )

    assert normalized is not None
    assert normalized["cache_write_ttl"] == "1h"


def test_normalize_usage_for_anthropic_messages_leaves_ttl_unset_when_ambiguous() -> None:
    """Writes spanning both retention windows have no single TTL.

    Guessing one would silently misprice the request, so the TTL is left unset
    and `compute_cost` falls back to the default cache-write rate.
    """
    normalized = normalize_usage(
        "/v1/messages",
        {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": 300,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 200,
                },
            }
        },
    )

    assert normalized is not None
    assert normalized["cache_write_ttl"] is None


def test_normalize_usage_for_anthropic_messages_prefers_explicit_ttl() -> None:
    """An explicitly supplied TTL wins over the inferred one."""
    normalized = normalize_usage(
        "/v1/messages",
        {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": 100,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
            "cache_write_ttl": "1h",
        },
    )

    assert normalized is not None
    assert normalized["cache_write_ttl"] == "1h"


def test_normalize_usage_for_anthropic_messages_rejects_missing_token_counts() -> None:
    with pytest.raises(ValueError, match="missing required token counts"):
        normalize_usage("/v1/messages", {"usage": {"input_tokens": 10}})


def test_normalize_usage_defaults_missing_cache_details_to_zero() -> None:
    normalized = normalize_usage(
        "/responses",
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
                "output_tokens_details": {"reasoning_tokens": 1},
            }
        },
    )

    assert normalized is not None
    assert normalized["input_tokens_standard"] == 10
    assert normalized["input_tokens_cache_read"] == 0
    assert normalized["input_tokens_cache_write"] == 0
    assert normalized["cache_detail_available"] is False


def test_normalize_usage_rejects_invalid_usage_shapes() -> None:
    with pytest.raises(ValueError, match="missing required token counts"):
        normalize_usage("/chat/completions", {"usage": {"prompt_tokens": 10}})

    with pytest.raises(ValueError, match="cached input exceeds input"):
        normalize_usage(
            "/chat/completions",
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                    "prompt_tokens_details": {"cached_tokens": 11},
                }
            },
        )


def test_compute_cost_defaults_to_zero_without_pricing() -> None:
    cost = compute_cost(
        {
            "input_tokens_standard": 100,
            "input_tokens_cache_read": 25,
            "input_tokens_cache_write": 30,
            "output_tokens": 40,
        },
        None,
    )

    assert cost == {
        "currency": "USD",
        "input_tokens_standard_cost": 0.0,
        "input_tokens_cache_read_cost": 0.0,
        "input_tokens_cache_write_cost": 0.0,
        "output_cost": 0.0,
        "total_cost": 0.0,
    }


def test_compute_cost_accounts_for_cache_reads_and_writes() -> None:
    cost = compute_cost(
        {
            "input_tokens_standard": 100,
            "input_tokens_cache_read": 25,
            "input_tokens_cache_write": 50,
            "output_tokens": 40,
            "cache_write_ttl": "5m",
        },
        {
            "currency": "USD",
            "input_per_token": 2.0,
            "cache_read_input_per_token": 0.5,
            "cache_write_input_per_token": 0.25,
            "cache_write_input_per_token_by_ttl": {"5m": 1.5, "1h": 2.5},
            "output_per_token": 3.0,
        },
    )

    assert cost == {
        "currency": "USD",
        "input_tokens_standard_cost": 200.0,
        "input_tokens_cache_read_cost": 12.5,
        "input_tokens_cache_write_cost": 75.0,
        "output_cost": 120.0,
        "total_cost": 407.5,
    }


def test_aggregate_usage_summarizes_reported_records_by_configured_model() -> None:
    summary = aggregate_usage(
        [
            {
                "configured_model": "planner-model",
                "model": "provider/planner",
                "endpoint": "/chat/completions",
                "usage_status": "reported",
                "duration_seconds": 2.0,
                "usage": {
                    "input_tokens_standard": 90,
                    "input_tokens_cache_read": 10,
                    "input_tokens_cache_write": 0,
                    "output_tokens": 20,
                    "reasoning_tokens": 3,
                    "total_input_tokens": 100,
                    "total_tokens": 120,
                },
                "cost": {
                    "input_tokens_standard_cost": 0.08,
                    "input_tokens_cache_read_cost": 0.01,
                    "input_tokens_cache_write_cost": 0.0,
                    "output_cost": 0.03,
                    "total_cost": 0.12,
                },
            },
            {
                "configured_model": "builder-model",
                "model": "provider/builder",
                "endpoint": "/responses",
                "usage_status": "reported",
                "duration_seconds": 3.0,
                "usage": {
                    "input_tokens_standard": 40,
                    "input_tokens_cache_read": 0,
                    "input_tokens_cache_write": 15,
                    "output_tokens": 15,
                    "reasoning_tokens": 2,
                    "total_input_tokens": 55,
                    "total_tokens": 70,
                },
                "cost": {
                    "input_tokens_standard_cost": 0.03,
                    "input_tokens_cache_read_cost": 0.0,
                    "input_tokens_cache_write_cost": 0.02,
                    "output_cost": 0.05,
                    "total_cost": 0.1,
                },
            },
            {
                "configured_model": "planner-model",
                "model": "provider/planner",
                "endpoint": "/chat/completions",
                "usage_status": "missing",
                "usage_missing_reason": "http_error_or_incomplete_response",
            },
        ]
    )

    assert summary["usage_available"] is True
    assert summary["totals"] == {
        "input_tokens_standard": 130,
        "input_tokens_cache_read": 10,
        "input_tokens_cache_write": 15,
        "output_tokens": 35,
        "reasoning_tokens": 5,
        "total_input_tokens": 155,
        "total_tokens": 190,
        "input_tokens_standard_cost": 0.11,
        "input_tokens_cache_read_cost": 0.01,
        "input_tokens_cache_write_cost": 0.02,
        "output_cost": 0.08,
        "total_cost": 0.22,
        "duration_seconds": 5.0,
        "tokens_per_second": 7.0,
    }
    assert summary["by_model"] == {
        "builder-model": {
            "input_tokens_standard": 40,
            "input_tokens_cache_read": 0,
            "input_tokens_cache_write": 15,
            "output_tokens": 15,
            "reasoning_tokens": 2,
            "total_input_tokens": 55,
            "total_tokens": 70,
            "input_tokens_standard_cost": 0.03,
            "input_tokens_cache_read_cost": 0.0,
            "input_tokens_cache_write_cost": 0.02,
            "output_cost": 0.05,
            "total_cost": 0.1,
            "requests": 1,
            "duration_seconds": 3.0,
            "tokens_per_second": 5.0,
        },
        "planner-model": {
            "input_tokens_standard": 90,
            "input_tokens_cache_read": 10,
            "input_tokens_cache_write": 0,
            "output_tokens": 20,
            "reasoning_tokens": 3,
            "total_input_tokens": 100,
            "total_tokens": 120,
            "input_tokens_standard_cost": 0.08,
            "input_tokens_cache_read_cost": 0.01,
            "input_tokens_cache_write_cost": 0.0,
            "output_cost": 0.03,
            "total_cost": 0.12,
            "requests": 1,
            "duration_seconds": 2.0,
            "tokens_per_second": 10.0,
        },
    }
    assert summary["status_counts"] == {"missing": 1, "reported": 2}
    assert summary["endpoint_counts"] == {"/chat/completions": 2, "/responses": 1}


def test_aggregate_usage_reports_zero_throughput_without_durations() -> None:
    summary = aggregate_usage(
        [
            {
                "configured_model": "demo-model",
                "model": "provider/demo",
                "endpoint": "/chat/completions",
                "usage_status": "reported",
                "usage": {
                    "input_tokens_standard": 10,
                    "input_tokens_cache_read": 0,
                    "input_tokens_cache_write": 0,
                    "output_tokens": 5,
                    "reasoning_tokens": 0,
                    "total_input_tokens": 10,
                    "total_tokens": 15,
                },
                "cost": {"total_cost": 0.0},
            }
        ]
    )

    assert summary["totals"]["duration_seconds"] == 0.0
    assert summary["totals"]["tokens_per_second"] == 0.0
    assert summary["by_model"]["demo-model"]["tokens_per_second"] == 0.0
