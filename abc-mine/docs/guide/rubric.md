# The scoring rubric

Screening scores each commit on weighted axes. The **model supplies per-axis scores
and justifications; the total and the disqualification decision are computed in
Python.** That split keeps scoring auditable, and lets a run be re-weighted after
the fact without re-invoking a single agent.

## Default axes

| # | Axis | Weight | Gate | Measures |
|---|---|---|---|---|
| 1 | Verifiability | 0.30 | **yes** | Can success be checked deterministically, ideally by a targeted test? |
| 2 | Task well-definedness | 0.20 | **yes** | Is there one coherent, self-contained task here? |
| 3 | Starting-state cleanliness | 0.15 | no | How cleanly does the pre-change tree stand alone? |
| 4 | Self-containedness / scope | 0.15 | no | Localized enough for one session — and not trivial? |
| 5 | Difficulty appropriateness | 0.10 | no | Non-trivial but tractable reasoning? |
| 6 | Cost to verify | 0.10 | no | How expensive is the check, against *measured* build cost? |

Each axis is scored 0–4.

## Scoring

```text
benchmark_score = round(Σ(weight_i × score_i) / 4 × 100)      # 0–100
```

A **0 on any gate axis** forces `benchmark_score = 0` and `disqualified = true`,
regardless of the other axes. A benchmark you cannot verify, or from which no
coherent task can be extracted, is worthless however interesting the change.

Weights must sum to 1.0 — validated at load, because otherwise the "0–100" score
would not span 0–100 and scores would not be comparable across runs.

Missing or unparseable axis scores count as **0**. On a gate axis that disqualifies.
That is deliberate: a screening agent that did not answer the gate questions has not
produced a usable verdict, and treating its silence as a pass would let unusable
commits through to a stage that trusts them.

## Verifiability dominates

The heaviest weight and a gate. An interesting change nobody can grade is worth less
than a mundane one that can be. `verify_cost` then rewards *targeted* checks, which
is what keeps the emitted suite affordable to run.

## Re-weighting

Override by axis key in your config:

```yaml
rubric:
  axes:
    verify_cost:
      weight: 0.05
    verifiability:
      weight: 0.35
  task_types: [bug_fix, feature_add, refactor]
```

You may override `weight`, `gate`, `title`, and `guidance`. You may **not** invent
new axes: an unknown key is an error, since a typo'd axis name would otherwise be
scored as a missing axis (0) and quietly disqualify everything.

The rubric is single-sourced. The scorer, the prompt tables, and the JSON schema the
agent is told to emit are all generated from the same definition, so they cannot
drift apart. Changing a weight is one edit.

Re-weighting is **critical drift**: it is refused mid-run, because commits scored
under different weights are not comparable. Start a new run id, or re-score from
saved axis scores.

## Task-type tags

Recorded but **not scored**: `bug_fix`, `feature_add`, `refactor`,
`perf_optimization`, `numerical_method`, `io_format`, `build_system`, `test_only`,
`api_change`, `docs_or_cosmetic`. They drive shortlist diversity during ranking.

## Cross-commit comparability

Each screening agent sees one commit in isolation and cannot know whether its 75 is
generous relative to the field. Treat `benchmark_score` as a **triage signal**, not a
calibrated ranking — which is exactly how the ranking stage is instructed to treat
it. See [`limitations.md`](limitations.md#9-screening-judges-each-commit-in-isolation).

## Summary fields

```json
{
  "sha": "...", "parent": "...", "subject": "...",
  "benchmark_score": 79,
  "disqualified": false,
  "axes": {"verifiability": {"score": 4, "gate": true, "why": "..."}},
  "task_type": ["bug_fix"],
  "est_difficulty": "medium",
  "one_line": "...",
  "proposed_task": "...",
  "proposed_verification": "...",
  "build_attempted": false,
  "build_outcome": null,
  "rationale": "...",
  "confidence": "high",
  "screened_at": "...",
  "usage": {"requests": 12, "input_tokens": 48000, "output_tokens": 3100}
}
```

Because `axes` is retained, a run can be re-scored under new weights without
re-invoking any agent.
