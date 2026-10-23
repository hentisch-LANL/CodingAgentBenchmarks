"""Validate a full ranking: an exact permutation of the candidates, one JSON per line.

Used in two places, deliberately with the same code:

* by the rank stage, to accept or reject an agent's ``ranking.jsonl``;
* by the ranking agent itself, as a script copied into its workspace, so it can
  check its own output *before* finishing instead of learning from a failed run.

A check the agent runs but the stage does not (or vice versa) would let the two
disagree, so this module is the single definition. It is standard-library only,
because it runs inside the agent's container under whatever ``python3`` the image
ships.

Usage (as a script)::

    python3 check_ranking.py CANDIDATE_SHAS_FILE RANKING_JSONL

Exit status 0 means the ranking is acceptable; 1 lists every problem found.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Sequence

#: Fields every ranking line must carry, with a short description (also used to
#: render the schema into the prompt, so the two cannot drift).
REQUIRED_FIELDS: dict[str, str] = {
    "sha": "the candidate's full 40-character SHA, exactly as in its heading",
    "viability": "specific to this commit: how would it be graded, by what deterministic (ideally targeted) check, and how likely is construction to succeed?",
    "favorability": "specific to this commit: what makes it worth (or not worth) a slot in the suite?",
    "est_difficulty": "trivial | easy | medium | hard | research — your own judgement",
    "task_type": "list of task types — your own judgement, not a placeholder",
}

#: Optional fields. ``null`` (or absent) means "use the screening pass's version".
OPTIONAL_FIELDS: dict[str, str] = {
    "one_line": "a sharper one-sentence task description, or null",
    "proposed_task": "a refined task statement, or null",
    "proposed_verification": "a refined verification plan, or null",
    "concerns": "anything that could sink this task during construction, or null",
    "checked_with_subagent": "true only if you dispatched a subagent about this commit",
    "subagent_finding": "that subagent's one-or-two-sentence finding, or null",
}

#: Problems listed before truncation, so a badly broken file stays readable.
MAX_LISTED = 25


def parse_lines(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse JSON Lines. Returns ``(entries, problems)``; blank lines are ignored."""
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {number}: expected a JSON object, got {type(value).__name__}")
            continue
        value["_line"] = number
        entries.append(value)
    return entries, problems


def check(entries: Sequence[dict[str, Any]], candidate_shas: Iterable[str]) -> list[str]:
    """Return every problem with ``entries`` as a ranking of ``candidate_shas``.

    Acceptable means: every candidate appears exactly once, nothing else appears,
    SHAs are given in full, and every required field is present.
    """
    expected = list(candidate_shas)
    expected_set = set(expected)
    problems: list[str] = []
    seen: dict[str, int] = {}

    for entry in entries:
        where = f"line {entry.get('_line', '?')}"
        sha = entry.get("sha")
        if not isinstance(sha, str) or not sha.strip():
            problems.append(f"{where}: missing 'sha'")
            continue
        sha = sha.strip()
        if sha not in expected_set:
            prefix = [s for s in expected if s.startswith(sha)]
            if len(prefix) == 1:
                problems.append(
                    f"{where}: '{sha}' is abbreviated; write the full SHA {prefix[0]}"
                )
            else:
                problems.append(f"{where}: '{sha}' is not one of the candidates")
            continue
        if sha in seen:
            problems.append(f"{where}: {sha} is a duplicate (first at line {seen[sha]})")
            continue
        seen[sha] = entry.get("_line", 0)
        missing = [f for f in REQUIRED_FIELDS if entry.get(f) in (None, "", [])]
        if missing:
            problems.append(f"{where} ({sha[:12]}): missing {', '.join(missing)}")

    omitted = [s for s in expected if s not in seen]
    if omitted:
        shown = ", ".join(s[:12] for s in omitted[:10])
        more = f" and {len(omitted) - 10} more" if len(omitted) > 10 else ""
        problems.append(
            f"{len(omitted)} candidate(s) are missing from the ranking: {shown}{more}"
        )
    return problems


def summarize(problems: Sequence[str]) -> str:
    """Render problems as a bounded, human-readable block."""
    listed = list(problems[:MAX_LISTED])
    if len(problems) > MAX_LISTED:
        listed.append(f"... and {len(problems) - MAX_LISTED} more problem(s)")
    return "\n".join(f"  - {p}" for p in listed)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: check_ranking.py CANDIDATE_SHAS_FILE RANKING_JSONL", file=sys.stderr)
        return 2
    with open(args[0]) as f:
        shas = [line.strip() for line in f if line.strip()]
    try:
        with open(args[1]) as f:
            text = f.read()
    except OSError as exc:
        print(f"NOT OK: cannot read {args[1]}: {exc}")
        return 1
    entries, problems = parse_lines(text)
    problems += check(entries, shas)
    if problems:
        print(f"NOT OK: {len(problems)} problem(s) in {args[1]}:")
        print(summarize(problems))
        return 1
    print(f"OK: {len(entries)} entries, an exact ranking of all {len(shas)} candidates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
