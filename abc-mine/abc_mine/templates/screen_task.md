# Screening task

## Commit under review

| Field | Value |
|-------|-------|
| SHA | `@@sha@@` |
| Parent | `@@parent@@` |
| Subject | @@subject@@ |
| Date | @@author_date@@ |
| Files changed | @@files_changed@@ |
| Diff size | @@diff_lines@@ lines |

## Materials available to you

- **The diff** introduced by this commit: `@@diff_path@@`@@diff_truncation_note@@
- **The commit message**, in full: `@@message_path@@`
- **Files touched by this commit:**

@@touched_files@@

- **The full source tree** at `@@source_path@@`, checked out at this commit. Read
  anything you need for context; the diff alone rarely tells the whole story.

## Your job

1. Read the diff, the commit message, and as much surrounding code as you need to
   understand what actually changed and why.
2. Decide whether this commit implies a good agentic-coding benchmark: a
   well-scoped task an agent could solve, whose correctness can be checked
   deterministically.
3. **Optionally** compile or run a targeted test — only if that settles a real
   question, and only once the commit already looks promising. Record what you did
   honestly.
4. Score every rubric axis from 0 to 4. Remember that a 0 on a gate axis
   disqualifies the commit; use it when genuinely warranted, and not otherwise.
5. Write your verdict as strict JSON to `@@summary_path@@`.

## Required output

Write a JSON object with **exactly** these fields to `@@summary_path@@`:

```json
{
  "axes": {
@@rubric_axes_schema@@
  },
  "task_type": ["<one or more tags>"],
  "est_difficulty": "trivial|easy|medium|hard|research",
  "one_line": "One sentence describing the benchmark task this commit implies.",
  "proposed_task": "2-4 sentences: what the solving agent would be asked to do.",
  "proposed_verification": "2-4 sentences: the concrete, ideally targeted, check that would grade it.",
  "build_attempted": false,
  "build_outcome": null,
  "rationale": "A short paragraph tying your axis scores together.",
  "confidence": "low|medium|high"
}
```

### Field notes

- `task_type` — one or more of: @@task_types@@
- `est_difficulty` — difficulty *for a solving agent*, not for you.
- `one_line` — describe the **task**, not the commit. "Fix the off-by-one in the
  flux limiter" is a task; "Bug fix" is not.
- `proposed_task` — what the agent must achieve, not how the original author
  achieved it.
- `proposed_verification` — be concrete. Name the kind of check and, if you know
  it, the specific test or target. This is the single most useful field you write:
  later stages lean on it heavily.
- `build_attempted` / `build_outcome` — `true` only if you actually ran something;
  a short factual result string, or `null`.
- `confidence` — your own certainty. `low` is a legitimate and useful answer;
  overstating confidence is worse than admitting doubt.
- Each axis `why` — one sentence justifying that score. Write it so a reader who
  disagrees can see exactly what you were weighing.

## Important

- **Strict JSON only** at `@@summary_path@@`: no code fences, no leading or
  trailing commentary.
- Do **not** include a total score or a `disqualified` field. Both are computed
  from your axis scores afterwards.
- Write the file before you finish. A run that ends without it counts as a
  failure and will be retried, wasting the work you just did.
