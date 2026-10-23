#!/usr/bin/env python3
"""Render a Codex ``--json`` event stream for ABC Bench.

Reads Codex JSONL events on stdin and does three things:

1. writes every raw line verbatim to the events file (argv[1]) so the exact
   machine-readable stream is preserved for later analysis;
2. prints a human-readable transcript to stdout, flushed per line, so a long
   run is observable live in ``ch-run.log``/``harness.log``;
3. writes a small summary (argv[2]) with the terminal event, thread id, token
   usage, and counts of malformed lines.

Unknown event types and unknown fields are preserved in the raw file and
reported generically rather than dropped, so a future Codex version does not
silently lose information here.

This always exits 0: the harness reads Codex's own status from
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


def _describe_item(item, started=False):
    """Return a list of transcript lines for one Codex item.

    ``started`` marks the ``item.started`` event, where only the command itself
    is known; the matching ``item.completed`` reports the exit code and output
    and so does not repeat the command line.
    """
    kind = item.get("type", "unknown")

    if kind == "agent_message":
        return ["[assistant] " + _truncate(item.get("text", ""))]
    if kind == "reasoning":
        text = item.get("text") or item.get("summary") or ""
        return ["[reasoning] " + _truncate(text, 600)] if text else []
    if kind == "command_execution":
        if started:
            return ["[command] " + _truncate(item.get("command", ""), 600)]
        lines = []
        exit_code = item.get("exit_code")
        output = _truncate(item.get("aggregated_output", ""), 1500)
        lines.append(f"[command exit={exit_code}]")
        if output.strip():
            lines.append("[output] " + output.rstrip())
        return lines
    if kind == "file_change":
        changes = item.get("changes") or []
        described = []
        for change in changes:
            if isinstance(change, dict):
                described.append(
                    f"{change.get('kind', 'change')} {change.get('path', '?')}"
                )
            else:
                described.append(str(change))
        return ["[file_change] " + (", ".join(described) or _truncate(item))]
    if kind == "mcp_tool_call":
        return [
            "[tool] "
            + _truncate(f"{item.get('server', '?')}.{item.get('tool', '?')}", 300)
        ]
    if kind == "error":
        return ["[error] " + _truncate(item.get("message", ""))]
    if kind == "todo_list":
        return ["[todo] " + _truncate(item.get("items", ""), 600)]

    # Unknown item type: report it rather than silently skipping.
    return ["[item:" + kind + "] " + _truncate(item, 600)]


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: render_events.py EVENTS_JSONL SUMMARY_JSON\n")
        return 0

    events_path, summary_path = sys.argv[1], sys.argv[2]

    terminal = None
    thread_id = None
    usage = None
    usage_events = []
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
                # Not valid JSON: surface it, since Codex may print a plain
                # diagnostic line into stdout on some failures.
                print("[raw] " + _truncate(stripped, 600), flush=True)
                continue

            kind = event.get("type", "unknown")
            counts[kind] = counts.get(kind, 0) + 1

            if kind == "thread.started":
                thread_id = event.get("thread_id")
                print(f"[thread] {thread_id}", flush=True)
            elif kind == "turn.started":
                print("[turn started]", flush=True)
            elif kind in ("turn.completed", "turn.failed"):
                terminal = kind
                if "usage" in event and isinstance(event["usage"], dict):
                    usage = event["usage"]
                    usage_events.append(event["usage"])
                print(f"[{kind}] " + _truncate(event.get("usage", ""), 400), flush=True)
                if kind == "turn.failed":
                    detail = event.get("error") or event
                    error_messages.append(_truncate(detail, 1000))
                    print("[turn.failed detail] " + _truncate(detail, 1000), flush=True)
            elif kind in ("item.started", "item.updated"):
                # Only narrate started commands; completion carries the output.
                item = event.get("item") or {}
                if item.get("type") == "command_execution" and kind == "item.started":
                    for text in _describe_item(item, started=True):
                        print(text, flush=True)
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "error":
                    error_messages.append(_truncate(item.get("message", ""), 1000))
                for text in _describe_item(item):
                    print(text, flush=True)
            elif kind == "error":
                message = event.get("message", "")
                error_messages.append(_truncate(message, 1000))
                print("[error] " + _truncate(message), flush=True)
            else:
                print(f"[event:{kind}] " + _truncate(event, 400), flush=True)

    summary = {
        "last_terminal_event": terminal,
        "thread_id": thread_id,
        "usage": usage,
        "usage_events": usage_events,
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
