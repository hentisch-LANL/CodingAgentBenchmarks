# Task: build an ABC Bench task from commit @@sha@@

## Commit information

| Field | Value |
|-------|-------|
| SHA (the change / fixed state) | `@@sha@@` |
| Parent (pre-change state) | `@@parent@@` |
| Ranking description | @@subject@@ |
| Rank | @@rank@@ |
| Estimated difficulty | @@est_difficulty@@ |
| Task type | @@task_type@@ |

> **Read the real commit message** at `@@message_path@@` before you write
> anything. The description above is a paraphrase from an earlier stage, not the
> author's own words, and **the real message calibrates how much your task
> statement should reveal**. Anything the author specified there — a named
> algorithm, method, formula, or code location — must be carried into the
> statement.

## Screening summary

The per-commit assessment that first flagged this commit, with its rubric scores
and reasoning. Use it to understand *why* this is considered verifiable and what a
check should target.

@@screening_summary@@

## Ranking entry

The ranking stage's record for this commit, including its viability and
favorability reasoning (and any concerns it raised).

@@shortlist_entry@@

## Proposed task (a starting point — revise as needed)

@@proposed_task@@

## Proposed verification (a starting point — revise as needed)

@@proposed_verification@@

## Your job

Author **three** deliverables. The starting-state tree is built deterministically
afterwards; do not create it.

### 1. `@@out_prompt@@` — the task statement

The only instruction the solving agent ever receives. Per the golden rule in the
system brief: everything your verifier requires must be derivable from this file
alone.

State the substantive requirements — behaviour, interfaces, outputs, values, edge
cases — specifically and completely. Do **not** reveal the reference
implementation. Calibrate scope to the real commit message: carry through anything
it specified, add detail where it was terse, and do not scaffold the solution. For
code locations the message did not name, letting the solver find them is correct
and fair.

Include the no-internet constraint and the fact that a hidden check grades the
result.

### 2. `@@out_build_env@@` — environment setup

A shell script that prepares the environment, run inside the container before the
solving agent starts. If the image is already self-contained, this can be
essentially a no-op — do not invent work for it. Make it executable.

### 3. `@@out_verify@@` — the deterministic check

A hidden verifier operating on `$ABC_BENCH_WORKSPACE`, ending in a `PASS`/`FAIL`
line with a matching exit code. Prefer a targeted build and a focused test. General
shape:

```bash
#!/bin/bash
set -euo pipefail
SRC="${ABC_BENCH_WORKSPACE:?ABC_BENCH_WORKSPACE not set}"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/verify.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT

# Configure/build/test using the commands from the system brief, pointed at $SRC.
# ... your checks here ...

echo PASS
```

At grading time the code tree is bind-mounted onto `/workspace`, so
`$ABC_BENCH_WORKSPACE` is `/workspace` and nothing the image placed beneath that
path is visible. Use the build and test commands as given, make your own scratch
directories, and never reference the directory you are authoring into.

The verifier must test everything substantive the statement asks for and nothing
it does not; be invariant to arbitrary choices; and resist shortcut solutions
without ever rejecting a legitimately correct alternative.

### The PASS/FAIL contract

- The **last** line whose stripped content is exactly `PASS` or exactly `FAIL`
  decides the result.
- `PASS` requires that line to be `PASS` **and** exit code 0.
- `FAIL` is declared if that line is `FAIL`, **or** the exit code is non-zero even
  when nothing printed `FAIL`.
- End with `echo PASS` on success, after your `set -e` guards, so the printed line
  and the exit code cannot disagree.

## Required before you finish

**Prove polarity**, using the full-history tree at `@@source_path@@`:

1. `git -C @@source_path@@ checkout @@sha@@` → verifier must `PASS` (exit 0).
2. `git -C @@source_path@@ checkout @@parent@@` → verifier must `FAIL`.

Iterate until both hold. This is re-checked independently on the shipped artifact
in a clean environment; if it does not hold there, the task is rejected and the
work is wasted.

**Then do the fairness pass.** Polarity is not fairness. Re-read your task
statement as the solver would — no diff, no memory of the reference — and confirm
every condition your verifier requires could be derived from it. Substantive gaps:
add them to the statement. Arbitrary details the verifier keys on: make the
verifier invariant. Genuinely unsure: specify it.

## Paths summary

```
Reference diff:  @@reference_diff_path@@
Commit message:  @@message_path@@
Source tree:     @@source_path@@   (checkout @@sha@@ or @@parent@@)

Write: @@out_prompt@@
Write: @@out_build_env@@
Write: @@out_verify@@
```

Do **not** create a starting-state tree — it is built for you.
