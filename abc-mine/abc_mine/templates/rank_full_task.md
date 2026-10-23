# Ranking task — order every candidate

You are given candidate commits from the screening pass of
**@@codebase_name@@**. Put **every one of them** in order, from the commit that
would make the best agentic-coding benchmark to the one that would make the
worst, and record your reasoning for each.

@@candidate_scope@@

## How this ranking is used — and what "good" means here

Your ranking is a **work queue**. There is no shortlist and no cut-off: the
construction stage starts at your first line and builds a benchmark from each
commit in turn, in exactly your order, for as long as it has budget — and we do
not know in advance how far down that will be. It may stop after 30 commits or
continue past 300. Each construction attempt is an expensive agent session, and
some attempts fail (the verifier cannot be made to discriminate, the old tree
will not build, the task turns out ill-posed).

So the goal is to **maximise the value we get wherever the queue happens to
stop.** That means the order has to be good *at every depth*, not only at the
top:

- Every position should hold the best remaining candidate. Swapping two
  neighbours at positions 180 and 181 matters just as much as swapping positions
  1 and 2, if the budget ends between them.
- "Value" is the chance that construction **succeeds** (a real, discriminating,
  deterministic check on a clean starting state) times how **worthwhile** the
  resulting benchmark is. A likely-to-succeed solid task generally beats a
  brilliant one that probably cannot be verified.
- Favour **breadth** as you go down: after several strong tasks from one
  subsystem or of one type, a comparable candidate that adds something new to
  the suite is worth more than another of the same.
- The bottom of the list still needs a real order. Among weak candidates, which
  are *least* hopeless? That is exactly where a long-running build will end up.

## Inputs

- **The candidate list** is at the end of this prompt, under **Candidates**:
  one block per commit, headed by its full SHA, carrying the screening pass's
  summary — subject, score, difficulty, task types, confidence, build notes, the
  one-line description, the proposed task and verification, and each rubric
  axis with its score and reasoning.

  The list is sorted by screening score, descending. That score is useful
  evidence, but **do not trust it blindly** — see below.

- **Candidates:** @@n_candidates@@ &nbsp;|&nbsp; **already discarded as unusable
  by screening:** @@n_disqualified@@ &nbsp;|&nbsp; **run:** `@@run_id@@`
- **Source tree:** `@@source_path@@` — full history, readable by you and by any
  subagent you dispatch.
- **The candidate SHAs, one per line:** `@@candidate_shas_path@@` — the exact set
  your ranking must cover.
- **A checker:** `@@checker_path@@` (see below).
- No internet access; model traffic goes through a local proxy.

## Do not trust the screening score blindly

Each candidate's score comes from a fast screening pass in which one agent saw
one commit in isolation, with no view of the others. It is a reasonable first
signal and it is why the list arrives in the order it does — but it is coarse
(many ties, a handful of distinct values), uncalibrated across commits, and
often wrong in the details that decide whether construction will succeed.
**Your whole value here is the judgement the score cannot make:** reading each
summary against every other one.

In particular:

- **Do not reproduce the score order**, in whole or in part. Wherever you place
  a candidate, it must be because *you* judged it better than what is below it,
  not because the screening pass did. Expect to move many candidates a long way.
- Read the reasoning, not the number: a 60 whose summary names an existing,
  sharply discriminating test can beat an 80 whose verification is speculative.
- Within a run of tied scores, the input order carries **no information at
  all** — you must order them yourself.

## Your job

1. Read the entire candidate list below. Every candidate you are to rank is in
   this prompt; there is no other copy to open.
2. Judge **every** candidate on its own summary, against the priorities in the
   system guidance and the value criterion above.
3. **Optionally**, for the few candidates near the top that hinge on one
   unresolved fact, dispatch the research subagent to settle that single
   question. Sparingly — not as a routine check on every candidate.
4. Decide the full order, then write it (see the output instructions), run the
   checker, and fix every problem it reports.

## Every entry must be a real judgement

This is a long, laborious task, and it is **supposed to be**: roughly
@@n_candidates@@ individual assessments. That is the job — there is no cheaper
version of it that is still useful. Specifically, do **not**:

- judge only the top few dozen and leave the rest in screening order;
- sort candidates into tiers or buckets and order within them mechanically;
- give many entries the same, or templated, `viability` / `favorability` text,
  or a placeholder `task_type` or `est_difficulty`;
- stop early because the remaining candidates look weak — their relative order
  is exactly what a long build will depend on.

Each entry's `viability` and `favorability` must be specific to **that** commit:
name the check (a test, a tolerance, a behaviour) and say what makes it worth,
or not worth, building. A reader should be able to tell from the text alone
which commit it describes. Short is fine; generic is not.

## Output — two files

### 1. `@@ranking_path@@` — the complete ranking

**JSON Lines: one JSON object per line, best candidate first.** No surrounding
array, no code fences, no prose, no blank-line separators needed.

```json
@@ranking_example@@
```

Fields:

@@ranking_fields@@

## THE RANKING MUST BE COMPLETE AND EXACT

**Your ranking must contain every one of the @@n_candidates@@ candidates, each
exactly once, and nothing else.** This is checked mechanically, and a ranking
that fails the check is **rejected in full**: there is no partial credit, no
repair, and no fallback. The whole ranking is discarded and must be redone.
Specifically, the ranking is rejected if:

- **any candidate is missing** — including the weak ones. Rank every candidate,
  however poor; a poor candidate belongs at the bottom, not off the list;
- **any candidate appears more than once**;
- **any SHA is not one of these candidates**, or is abbreviated — always copy the
  full 40-character SHA from the candidate's `###` heading;
- **any line is not a valid JSON object**, or lacks a required field.

Line order *is* the ranking: the first line is rank 1. Do not include a `rank`
field; it is assigned from the line order.

**Before you finish, run the checker and do not stop until it prints `OK`:**

```bash
python3 @@checker_path@@ @@candidate_shas_path@@ <your ranking file>
```

Run it on the temporary file while you work, and once more on
`@@ranking_path@@` after renaming.

It reports every missing, duplicated, unknown, or abbreviated SHA and every
malformed line. If it reports problems, fix them and run it again.

**How to write it.** The file will be about @@n_candidates@@ lines, which is
more than fits in a single response, so write it over **many turns**: append
blocks of about 50 entries at a time to a temporary file (e.g. with
`cat >> ... <<'EOF'`), in best-to-worst order, until every candidate is placed.
Expect this to take ten or more writes — that is normal and intended, and you
have ample time and budget for it. Give the last block the same care as the
first. Then run the checker on the temporary file, fix anything it reports, and
rename it into place.

### 2. `@@rationale_path@@`

Markdown, written for a human deciding whether to trust this ranking. Cover:

- **The top of the order:** for roughly the first twenty, why each earned its
  place, and any subagent finding you relied on.
- **How value changes down the order:** what characterises candidates in
  different stretches of your list, so a human deciding how long to keep
  building knows what they are getting at each depth.
- **Notable judgement calls:** candidates you ranked far from where their
  screening score would put them, and why.
- **Reservations:** highly ranked candidates with a doubt that could sink them
  during construction, and what would resolve it.

## Candidates

@@candidates_block@@
