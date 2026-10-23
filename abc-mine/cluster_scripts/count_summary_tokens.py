#!/usr/bin/env python3
"""Count the tokens held in a run's screening summaries.

Answers "how much text did screening actually produce?" — useful for sizing a
ranking prompt, which must fit every summary into one context, and for gauging
how much an agent wrote across a sweep.

Run by hand; nothing in the pipeline imports this. Standard library only, so it
works in any environment that can reach the endpoint.

Counting uses Anthropic's ``/v1/messages/count_tokens``, which performs no
inference and is free. It is *not* a cost report: it measures the summaries as
they sit on disk, not what was billed to produce them (those figures are in each
item's ``usage.jsonl``).

Usage
-----
Point it at a summaries directory, or at a run directory containing one::

    export LLM_BASE_URL=https://llm.example.org/v1
    export LLM_MODEL=your-anthropic-model-id
    export LLM_API_KEY=...

    ./count_summary_tokens.py data/truchas-v1
    ./count_summary_tokens.py data/truchas-v1/screen/summaries --per-file
    ./count_summary_tokens.py data/truchas-v1 --field rationale
    ./count_summary_tokens.py data/truchas-v1 --estimate        # no network

Exit status is nonzero if any file could not be counted, so a partial total is
never mistaken for a complete one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

#: Anthropic's documented version header. Required on every request.
ANTHROPIC_VERSION = "2023-06-01"

#: A gateway WAF may reject requests that omit a User-Agent with an opaque 403.
USER_AGENT = "abc-mine-count-summary-tokens/1.0"

#: Tokens the API adds to frame a single-message request, measured empirically.
#: Subtracted so a reported count reflects the *text*, not the envelope.
FRAMING_TOKENS = 7

#: Characters per token used by --estimate. Measured at ~3.9 on real summaries;
#: 4.0 is the conventional round number and errs slightly low.
CHARS_PER_TOKEN = 4.0


class CountError(RuntimeError):
    """A file's tokens could not be counted."""


def find_summaries(target: Path) -> list[Path]:
    """Return summary files under ``target``.

    Accepts either a summaries directory or a run directory, so the caller does
    not have to remember the layout. The nested locations are tried *first*: a
    run directory also contains ``run.json``, and globbing the top level would
    silently count the manifest as if it were a summary.

    Raises
    ------
    SystemExit
        If the path does not exist or holds no summaries.
    """
    if not target.exists():
        raise SystemExit(f"error: no such path: {target}")

    candidates = [target / "screen" / "summaries", target / "summaries", target]
    for directory in candidates:
        if directory.is_dir():
            found = sorted(directory.glob("*.json"))
            if found:
                return found

    tried = "\n".join(f"  {c}" for c in candidates)
    raise SystemExit(f"error: found no *.json summaries in any of:\n{tried}")


def read_text(path: Path, field: str | None) -> str:
    """Return the text to count for one summary.

    With ``field``, counts just that key — so "how big are the rationales?" can
    be asked separately from the whole record. Nested dicts and lists are
    re-serialized as JSON, matching how they would reach a model.

    Files are read with ``errors="replace"``: some commits in a long history
    predate a UTF-8 conversion, and a single undecodable byte should not abort a
    1300-file total.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    if field is None:
        return raw

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CountError(f"not valid JSON: {exc}") from exc
    if field not in data:
        raise CountError(f"no field {field!r}")

    value = data[field]
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def count_via_api(text: str, *, base_url: str, model: str, api_key: str,
                  timeout: float, retries: int = 3) -> int:
    """Return the API's input-token count for ``text``, minus request framing.

    Retries transient upstream failures: the gateway returns occasional 5xx, and
    a single blip should not discard a long run. A 4xx is not retried, since
    repeating a malformed or unauthorized request cannot help.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": text}],
    }).encode()
    api_root = base_url.rstrip('/')
    if not api_root.endswith('/v1'):
        api_root += '/v1'
    request = urllib.request.Request(
        f"{api_root}/messages/count_tokens",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "user-agent": USER_AGENT,
        },
    )

    backoff = 1.0
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace").strip()
            if exc.code < 500 or attempt == retries:
                raise CountError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise CountError(f"{type(exc).__name__}: {exc}") from exc
        time.sleep(backoff)
        backoff *= 2

    if "input_tokens" not in payload:
        raise CountError(f"response had no input_tokens: {str(payload)[:120]}")
    # Never report negative tokens for a near-empty field.
    return max(0, int(payload["input_tokens"]) - FRAMING_TOKENS)


def humanize(n: int) -> str:
    """Render a token count with a magnitude hint for large values."""
    if n >= 1_000_000:
        return f"{n:,} (~{n / 1_000_000:.2f}M)"
    if n >= 10_000:
        return f"{n:,} (~{n / 1_000:.0f}k)"
    return f"{n:,}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Count tokens across a run's screening summaries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Counting is free and runs no inference. Requires LLM_BASE_URL, "
               "LLM_MODEL, and LLM_API_KEY unless --estimate is given.",
    )
    parser.add_argument(
        "path", type=Path,
        help="a run directory (e.g. data/truchas-v1) or a summaries directory",
    )
    parser.add_argument(
        "--field", metavar="KEY", default=None,
        help="count only this top-level key of each summary (e.g. rationale)",
    )
    parser.add_argument(
        "--per-file", action="store_true",
        help="list every file's count, largest first, before the total",
    )
    parser.add_argument(
        "--top", type=int, default=0, metavar="N",
        help="with --per-file, show only the N largest",
    )
    parser.add_argument(
        "--estimate", action="store_true",
        help=f"approximate locally at {CHARS_PER_TOKEN:g} chars/token; no network, "
             "no credential, instant, and roughly 5%% low",
    )
    parser.add_argument(
        "--workers", type=int, default=8, metavar="N",
        help="concurrent API requests (default: 8)",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, metavar="SECONDS",
        help="per-request timeout (default: 60)",
    )
    args = parser.parse_args(argv)

    files = find_summaries(args.path)

    base_url = model = api_key = ""
    if not args.estimate:
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        model = os.environ.get("LLM_MODEL", "").strip()
        api_key = os.environ.get("LLM_API_KEY", "")
        missing = [
            name for name, value in (
                ("LLM_BASE_URL", base_url),
                ("LLM_MODEL", model),
                ("LLM_API_KEY", api_key),
            ) if not value
        ]
        if missing:
            print(
                "error: set " + ", ".join(missing)
                + "\nThese are the same variables the screening scripts use. "
                  "Or pass --estimate to approximate locally with no network.",
                file=sys.stderr,
            )
            return 2

    scope = f"field {args.field!r}" if args.field else "whole summary"
    how = (
        f"local estimate at {CHARS_PER_TOKEN:g} chars/token"
        if args.estimate else f"{model} via count_tokens"
    )
    print(f"{len(files)} summaries in {files[0].parent}")
    print(f"counting {scope}; {how}\n")

    def measure(path: Path) -> tuple[Path, int, int, str | None]:
        """Return (path, tokens, characters, error)."""
        try:
            text = read_text(path, args.field)
        except CountError as exc:
            return path, 0, 0, str(exc)
        if args.estimate:
            return path, int(len(text) / CHARS_PER_TOKEN), len(text), None
        try:
            return path, count_via_api(
                text, base_url=base_url, model=model, api_key=api_key,
                timeout=args.timeout,
            ), len(text), None
        except CountError as exc:
            return path, 0, len(text), str(exc)

    started = time.monotonic()
    if args.estimate:
        results = [measure(p) for p in files]
    else:
        # Counting is network-bound, so overlap the requests. Threads suffice.
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = list(pool.map(measure, files))
    elapsed = time.monotonic() - started

    failures = [(p, err) for p, _, _, err in results if err]
    ok = [(p, tokens, chars) for p, tokens, chars, err in results if not err]

    if args.per_file and ok:
        ranked = sorted(ok, key=lambda row: row[1], reverse=True)
        shown = ranked[: args.top] if args.top > 0 else ranked
        width = max(len(p.name) for p, _, _ in shown)
        print(f"{'file':{width}s} {'tokens':>9s} {'chars':>9s}")
        for path, tokens, chars in shown:
            print(f"{path.name:{width}s} {tokens:9,d} {chars:9,d}")
        if args.top > 0 and len(ranked) > args.top:
            print(f"... {len(ranked) - args.top:,} more")
        print()

    total_tokens = sum(tokens for _, tokens, _ in ok)
    total_chars = sum(chars for _, _, chars in ok)

    print(f"files counted    {len(ok):,}" + (f" of {len(files):,}" if failures else ""))
    print(f"total tokens     {humanize(total_tokens)}")
    print(f"total characters {total_chars:,}")
    if ok:
        print(f"mean per file    {total_tokens / len(ok):,.0f} tokens")
        largest = max(ok, key=lambda row: row[1])
        print(f"largest          {largest[1]:,} tokens ({largest[0].name})")
    if total_tokens:
        print(f"chars per token  {total_chars / total_tokens:.2f}")
    if not args.estimate:
        print(f"elapsed          {elapsed:.1f}s")

    if failures:
        print(f"\n{len(failures)} file(s) could not be counted:", file=sys.stderr)
        for path, err in failures[:10]:
            print(f"  {path.name}: {err}", file=sys.stderr)
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more", file=sys.stderr)
        print(
            "\nThe total above covers only the files that succeeded.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
