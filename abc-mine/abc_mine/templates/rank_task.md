# Ranking task — curate the shortlist

You are given candidate commits from the screening pass of
**@@codebase_name@@**. Select the subset that would make genuinely good
agentic-coding benchmarks, rank them, and report on what you found.

@@candidate_scope@@

## Inputs

- **The candidate list** is at the end of this prompt, under **Candidates**:
  one block per commit, headed by its full SHA, carrying the screening pass's
  summary — subject, score, difficulty, task types, confidence, build notes, the
  one-line description, the proposed task and verification, and each rubric
  axis with its score and reasoning.

  The list is sorted by score descending **purely as a reading order**. As the
  system guidance explains, those scores are a triage signal, not a ranking to
  reproduce.

- **Candidates:** @@n_candidates@@ &nbsp;|&nbsp; **already discarded as unusable
  by screening:** @@n_disqualified@@ &nbsp;|&nbsp; **run:** `@@run_id@@`
- **Source tree:** `@@source_path@@` — full history, readable by you and by any
  subagent you dispatch.
- No internet access; model traffic goes through a local proxy.

## Your job

1. Read the entire candidate list below. Every candidate you are to judge is in
   this prompt; there is no other copy to open.
2. Judge each candidate on its summary, against the priorities in the system
   guidance.
3. **Optionally**, for the few candidates that look promising but hinge on one
   unresolved fact, dispatch the research subagent to settle that single question.
   Sparingly — not as a routine check on every candidate.
4. Select the commits you judge genuinely good, rank them, and record *why* for
   each. Prefer a spread of task types; do not pad to a number.

## Diversity

Prefer a spread of task types across the shortlist, so the resulting suite
exercises different skills rather than five variations of the same bug fix. Do not
sacrifice a clearly stronger candidate for variety's sake — but between two
comparable candidates, pick the one that broadens the suite.

## How many to select

There is **no target count.** Select as many as you judge genuinely good, and no
more. Do not pad the list to reach a number, and do not cut a strong candidate to
stay under one. If only three are good, select three; if twenty are excellent,
select twenty. An honest short list is far more useful than a long padded one:
every task selected here costs real effort to construct and review downstream.

If you genuinely find nothing suitable, still emit a valid shortlist with an empty
selection and explain why in the rationale. That is a legitimate outcome, and a
far better one than selecting weak candidates to appear productive.

## Output — two files

### 1. `@@shortlist_path@@`

**Strict JSON. No code fences, no surrounding prose.**

```json
{
  "run_id": "@@run_id@@",
  "selected": [
    {
      "sha": "full commit SHA",
      "rank": 1,
      "one_line": "one-sentence description of the benchmark task",
      "viability": "why this can be graded by a deterministic, ideally targeted, check",
      "favorability": "why this is a compelling benchmark worth a slot in the suite",
      "est_difficulty": "trivial|easy|medium|hard|research",
      "task_type": ["bug_fix"],
      "proposed_task": "what the solving agent will be asked to do",
      "proposed_verification": "the concrete check that should grade it",
      "checked_with_subagent": false,
      "subagent_finding": null
    }
  ],
  "n_candidates": @@n_candidates@@,
  "n_disqualified": @@n_disqualified@@,
  "n_selected": 0
}
```

Field notes:

- `rank` — 1 for your best pick, incrementing by 1 with no gaps or ties.
- `viability` vs `favorability` — keep these distinct. Viability is *can we grade
  it*; favorability is *is it worth grading*. A commit needs both, and separating
  them forces the question of whether a compelling change is actually checkable.
- `proposed_task` / `proposed_verification` — refine the screening pass's
  sketches where you can. These become the starting point for task construction,
  so precision here saves work and prevents drift later.
- `checked_with_subagent` / `subagent_finding` — `true` plus a one-or-two-sentence
  summary only where you actually dispatched a subagent; `false` and `null`
  otherwise.
- `n_selected` must equal the length of `selected`.
- Include only commits you genuinely recommend. An empty `selected` with a clear
  explanation is a valid result.

### 2. `@@rationale_path@@`

Markdown, written for a human deciding whether to trust this shortlist. Cover:

- **Each selected commit:** why it is a good benchmark, and any subagent finding
  you relied on. Be specific about the intended check.
- **The strongest rejections:** the three to five that came closest, and what
  specifically fell short. This is genuinely valuable — it tells the reader where
  the bar ended up sitting, and lets them second-guess it.
- **Any reservations:** candidates you selected but have doubts about, and what
  would resolve them. Flagging a shaky pick now is far cheaper than discovering
  the problem after a task has been constructed and reviewed.

Write both files atomically (temporary file, then rename).

## Candidates

@@candidates_block@@
