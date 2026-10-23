#!/usr/bin/env python3
"""Render a Claude Code ``--output-format stream-json`` event stream for ABC Bench.

Reads Claude Code JSONL events on stdin and does three things:

1. writes every raw line verbatim to the events file (argv[1]) so the exact
   machine-readable stream is preserved for later analysis;
2. prints a human-readable transcript to stdout, flushed per line, so a long
   run is observable live in ``ch-run.log``/``harness.log``;
3. writes a small summary (argv[2]) with the result status, session id, token
   usage, permission denials, and counts of malformed lines.

Unknown event types and unknown fields are preserved in the raw file and
reported generically rather than dropped, so a future Claude Code version does
not silently lose information here.

The summary deliberately records ``is_error`` and ``terminal_reason`` separately
from ``result_subtype``: an API error still reports subtype ``success`` while
setting ``is_error`` true, so the subtype alone must not be read as a verdict.

This always exits 0: the harness reads Claude Code's own status from
``PIPESTATUS[0]``, so a rendering problem must not masquerade as an agent
failure.
"""

import json
import sys


def _truncate(text, limit=2000):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, {len(text)} chars total]"


def _tool_summary(name, tool_input):
    """Return a one-line description of a tool call's salient argument."""
    if not isinstance(tool_input, dict):
        return _truncate(tool_input, 300)

    # Show the argument that identifies what the call actually did, rather than
    # dumping a whole file's contents from a Write call.
    for key in ("command", "file_path", "pattern", "path", "url", "prompt"):
        if key in tool_input:
            detail = _truncate(tool_input[key], 600)
            extra = ""
            if name == "Bash" and tool_input.get("description"):
                extra = f" ({_truncate(tool_input['description'], 200)})"
            return f"{detail}{extra}"
    return _truncate(json.dumps(tool_input, sort_keys=True), 400)


def _describe_content_block(block):
    """Return transcript lines for one assistant/user message content block."""
    if not isinstance(block, dict):
        return ["[content] " + _truncate(block, 400)]

    kind = block.get("type")

    if kind == "text":
        text = block.get("text") or ""
        return ["[assistant] " + _truncate(text)] if text.strip() else []
    if kind == "thinking":
        text = block.get("thinking") or ""
        return ["[thinking] " + _truncate(text, 600)] if text.strip() else []
    if kind == "tool_use":
        name = block.get("name", "?")
        return [f"[tool:{name}] " + _tool_summary(name, block.get("input"))]
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            # Structured results (e.g. text blocks) are flattened for display.
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text") or "")
                else:
                    parts.append(json.dumps(part, sort_keys=True))
            content = "\n".join(parts)
        marker = "tool_result error" if block.get("is_error") else "tool_result"
        rendered = _truncate(content or "", 1500)
        return [f"[{marker}] " + rendered.rstrip()] if rendered.strip() else [f"[{marker}]"]

    # Unknown block type: report it rather than silently skipping.
    return ["[content:" + str(kind) + "] " + _truncate(block, 400)]


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: render_events.py EVENTS_JSONL SUMMARY_JSON\n")
        return 0

    events_path, summary_path = sys.argv[1], sys.argv[2]

    result_subtype = None
    is_error = None
    terminal_reason = None
    api_error_status = None
    session_id = None
    claude_code_version = None
    model = None
    available_tools = None
    num_turns = None
    total_cost_usd = None
    usage = None
    model_usage = None
    permission_denials = []
    api_retries = 0
    error_messages = []
    malformed_lines = 0
    counts = {}

    with open(events_path, "w", encoding="utf-8") as raw:
        for line in sys.stdin:
            raw.write(line)
            raw.flush()

            stripped = line.strip()
            if not stripped:
                continue

            try:
                event = json.loads(stripped)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
            except (ValueError, UnicodeDecodeError):
                malformed_lines += 1
                # Not valid JSON: surface it, since the CLI may print a plain
                # diagnostic line into stdout on some failures.
                print("[raw] " + _truncate(stripped, 600), flush=True)
                continue

            kind = event.get("type", "unknown")
            subtype = event.get("subtype")
            counts[f"{kind}.{subtype}" if subtype else kind] = (
                counts.get(f"{kind}.{subtype}" if subtype else kind, 0) + 1
            )

            if kind == "system" and subtype == "init":
                session_id = event.get("session_id")
                claude_code_version = event.get("claude_code_version")
                model = event.get("model")
                available_tools = event.get("tools")
                print(
                    f"[init] session={session_id} version={claude_code_version} "
                    f"model={model} mode={event.get('permissionMode')}",
                    flush=True,
                )
                # Record the tool set actually offered to the model: the harness
                # restricts it, and a mismatch is worth seeing in the log.
                print("[tools] " + _truncate(available_tools, 600), flush=True)
            elif kind == "system" and subtype == "permission_denied":
                denial = {
                    "tool_name": event.get("tool_name"),
                    "message": event.get("message"),
                    "decision_reason_type": event.get("decision_reason_type"),
                }
                permission_denials.append(denial)
                print(
                    f"[permission_denied] {denial['tool_name']}: "
                    + _truncate(denial["message"], 400),
                    flush=True,
                )
            elif kind == "system" and subtype == "api_retry":
                # The CLI retries transient upstream errors itself (the proxy
                # retries only transport failures, not HTTP 5xx). These are
                # counted and surfaced so a run that succeeded only after
                # fighting a flaky endpoint is visible rather than looking clean.
                api_retries += 1
                print(
                    f"[api_retry] attempt={event.get('attempt')} "
                    f"status={event.get('error_status')} error={event.get('error')}",
                    flush=True,
                )
            elif kind in ("assistant", "user"):
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    for text in _describe_content_block(block):
                        print(text, flush=True)
            elif kind == "result":
                result_subtype = subtype
                is_error = bool(event.get("is_error"))
                terminal_reason = event.get("terminal_reason")
                api_error_status = event.get("api_error_status")
                num_turns = event.get("num_turns")
                total_cost_usd = event.get("total_cost_usd")
                session_id = event.get("session_id") or session_id
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if isinstance(event.get("modelUsage"), dict):
                    model_usage = event["modelUsage"]
                if event.get("permission_denials"):
                    for denial in event["permission_denials"]:
                        permission_denials.append(denial)
                if is_error:
                    error_messages.append(_truncate(event.get("result") or event, 1000))
                print(
                    f"[result] subtype={result_subtype} is_error={is_error} "
                    f"terminal_reason={terminal_reason} turns={num_turns} "
                    f"cost_usd={total_cost_usd}",
                    flush=True,
                )
                if event.get("result"):
                    print("[final] " + _truncate(event["result"]), flush=True)
            else:
                print(f"[event:{kind}] " + _truncate(event, 400), flush=True)

    summary = {
        "result_subtype": result_subtype,
        "is_error": is_error,
        "terminal_reason": terminal_reason,
        "api_error_status": api_error_status,
        "session_id": session_id,
        "claude_code_version": claude_code_version,
        "model": model,
        "available_tools": available_tools,
        "num_turns": num_turns,
        "total_cost_usd": total_cost_usd,
        "usage": usage,
        "model_usage": model_usage,
        "permission_denials": permission_denials,
        "api_retries": api_retries,
        "malformed_lines": malformed_lines,
        "event_counts": counts,
        "errors": error_messages,
    }
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
