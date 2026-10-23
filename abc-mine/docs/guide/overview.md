# Workflow overview

How a run proceeds, what each stage produces, and why the stages are shaped the way
they are.

## The funnel

```text
git history
     │
     │  selection: mechanical filters (merges, size, paths)  ── free, no model
     ▼
  SCREEN      one fresh agent per commit, in parallel
     │        reads diff + message + surrounding source
     │        scores the rubric; Python computes the total
     ▼
  per-commit summaries          ── gate-disqualified commits drop out
     │
     ▼
  RANK        one agent, all viable candidates in one context
     │        may dispatch research subagents for specific facts
     ▼
  ranked shortlist + rationale
     │
     ▼
  BUILD       one agent per shortlisted commit, in parallel
     │        authors task statement, env setup, verifier
     │        pipeline builds the starting state (no agent)
     │        polarity re-checked on the shipped artifact
     ▼
  ABC Bench tasks
```

Each stage reduces volume and increases scrutiny. That ordering is the whole point:
screening is cheap per item and runs on everything; construction is expensive per
item and runs only on what survived two rounds of judgement.

## Why three stages rather than one

A single agent asked to "find good benchmarks in this repository and build them"
would have to hold the whole history in context, compare candidates it saw at
different times, and construct tasks — all in one session. Splitting the work buys
three things:

1. **Screening parallelizes.** One commit per agent, no shared state, so a 1,600-commit
   history is wall-clock bounded by worker count rather than by context.
2. **Ranking gets a genuine comparison.** The ranking agent is the first party to see
   candidates side by side, which is a judgement no per-commit pass can make.
3. **Construction gets a focused brief.** Each construction agent works on one commit
   with the earlier stages' reasoning as context, and is judged on one artifact.

The cost is that screening scores are made in isolation and are therefore only a
triage signal — which is why ranking is explicitly told it may disagree with them.

## Stage 0 — `preflight`

Not part of the funnel, but run first. It executes the configured build and test
commands in the real container against the real repository, proves that a *targeted*
test selection works, and times everything.

The workflow's value rests on a precondition that "a repo and a container" does not
capture: the container must be able to **verify a change deterministically**. If it
cannot, every later stage still runs and produces unusable output. Preflight turns
that from a late, expensive discovery into a five-minute check, and — with
`--write-config` — replaces guesswork about build cost with a measurement every
stage then quotes.

## Stage 1 — `screen`

**Input:** selected commits. **Output:** one summary JSON per commit.

Each agent receives the diff, the full commit message, the list of touched files, and
a worktree checked out at that commit. It may compile and run targeted tests, but is
told to judge most commits by reading — a build is for settling a specific doubt, not
a step to tick.

The agent supplies per-axis scores (0–4) and justifications. **The total and the
disqualification decision are computed in Python**, so scoring is deterministic and a
run can be re-weighted later without re-invoking any agent. Two axes are gates: a zero
on either disqualifies outright, because a benchmark you cannot verify — or from which
no coherent task can be extracted — is worthless regardless of its other merits.

See [The scoring rubric](rubric.md).

## Stage 2 — `rank`

**Input:** every non-disqualified summary. **Output:** `shortlist.json` +
`rationale.md`.

One agent reads all viable candidates and selects those genuinely worth building,
ranked, with separate `viability` (can we grade it) and `favorability` (is it worth
grading) reasoning. It may dispatch research subagents to settle a single
decision-changing fact about a promising candidate, but is told to do so sparingly.

There is no fixed target count. An honest short list beats a padded one: every
selected task costs real effort to construct and review.

This stage is **single-context by design** — no chunk-and-merge. The assumption is
enforced with a token budget rather than hoped for; see
[Limitations](limitations.md#1-ranking-is-single-context-only).

## Stage 3 — `build`

**Input:** the shortlist. **Output:** complete abc-bench task directories.

Each agent authors three artifacts: the task statement, the environment setup, and the
hidden verifier. The fourth — the starting state — is built by the **pipeline**, not an
agent, via a git bundle-cutoff at the parent commit. An agent asked to "remove the
change" would introduce unauditable variation and might leave the answer behind; the
cutoff procedure instead asserts that no post-change object is present.

Verifier polarity is then checked **twice**: once by the authoring agent, and again by
the pipeline on the shipped artifact in an environment that mirrors the real grader and
provides none of the conveniences that existed during authoring. That second check is
not redundant — it catches the most common way a plausible verifier is broken, namely
depending on a path that only exists while the agent is working.

A task reaches `ok` only when its verifier fails on the starting state and passes on
the change. `export` copies only those.

## What is enforced, and what is not

**Enforced automatically:**

- The starting state is a real repository snapshot with no post-cutoff objects.
- The verifier discriminates between the pre- and post-change trees.
- All four task components exist.

**Not enforced — and requiring human review:**

- **Fairness** — whether everything the verifier needs is derivable from the task
  statement alone. The construction brief is emphatic about this and asks for a
  self-check, but a passing polarity check does not prove a fair task.
- **Shortcut resistance** — whether a solver could satisfy the verifier without doing
  the work.
- **Difficulty and interest.**

Polarity is a floor, not a ceiling. Read the emitted `prompt.md` and
`verify_solution.sh` before trusting a task.

## Output layout

```text
data/<run-id>/
  run.json                        manifest: config lock, provenance, stage history
  preflight.json                  measured build/test costs
  screen/
    summaries/<short-sha>.json    durable per-commit verdicts (resume keys on these)
    attempts.jsonl                every attempt, with failure reasons
    logs/<short-sha>/             agent transcripts (retention per keep_logs)
  rank/
    shortlist.json
    rationale.md
  build/
    manifest.jsonl                append-only build record
    tasks/<group>/<task-id>/
      prompt.md
      build_env.sh
      workspace/
      verification/verify_solution.sh
```

## Resumability

Every stage resumes from its own durable artifacts, so an interrupted run continues
where it stopped and can even switch parallel backends mid-run. See
[Checkpointing and resume](checkpointing.md).
