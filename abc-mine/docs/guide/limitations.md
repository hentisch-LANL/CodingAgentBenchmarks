# Limitations

What `abc-mine` deliberately does not do, and what it cannot promise. These
are scoping decisions and honest caveats, not a backlog of defects — though some
are candidates for future work, marked as such.

---

## 1. Ranking is single-context only

**By default the ranking stage assumes every viable candidate fits in one model
context.** There is no comparative chunk-and-merge, no multi-round tournament, no
hierarchical reduction, and no multi-agent deliberation. The one exception is the
opt-in `interleaving` method (below), which splits randomly and never compares
across groups.

### Why this is out of scope

Splitting candidates across agents and merging the results is not a matter of
adding a loop. It raises questions with no obviously correct answer:

- How do you compare two candidates that never appeared in the same context? A
  score assigned in isolation is exactly the signal ranking exists to improve on.
- How do you merge partial orderings without letting a merge agent simply defer to
  whichever chunk it read last?
- How do you stop per-chunk normalization from promoting the best of a weak chunk
  over an also-rans of a strong one?
- How do you keep the merge from becoming a second, unaudited ranking pass with
  its own biases?

These are real design problems. Implementing them badly would be worse than not
implementing them, because the output would look identical — a ranked shortlist —
while quietly being the product of arbitrary chunk boundaries.

### How the assumption is enforced

It is **checked, not hoped for**. Before any agent runs, the full ranking prompt —
harness system prompt, instructions, and every candidate — is counted exactly
through the endpoint and compared against `rank.max_prompt_tokens` (default
300,000). Exceeding it is a hard error with a menu of options, not a warning, and a
count that cannot be obtained stops the run rather than being estimated.

Silently overflowing a context and returning a shortlist drawn from the first 60%
of candidates would be far worse than refusing to start, because nothing in the
output would reveal it happened.

If you hit the budget:

1. **Screen more selectively** — `selection.paths`, `selection.max_files_changed`.
   Best option: fewer, better candidates.
2. **Set `rank.max_candidates`** — keep the top-scoring N. Blunt, but the drop is
   reported rather than hidden.
3. **Raise `rank.max_prompt_tokens`** if your model genuinely has the context.
   Keep it below the hard limit: the agent's own work shares the same window.
4. **Rank disjoint commit ranges as separate runs** and combine the shortlists
   yourself. You then own the cross-pass comparison — which is precisely the hard
   part the tool declines to fake.

5. **Use `rank.method: interleaving`** — rank random groups separately and interleave
   the results. Simple, but it rests on an assumption rather than a comparison: that
   random groups are equally strong. See
   [Configuration](configuration.md#method-interleaving).

*Future work.* Merging that actually compares candidates across groups needs its
own design work on comparability and merge fairness.

---

## 2. Ranking has no mid-stage resume

One agent doing one long task either finishes or does not. If it dies forty minutes
in, re-running starts over. Per-item checkpointing is meaningless when there is one
item, and partial progress is not recoverable without the multi-agent machinery
described above.

Bound the exposure with `execution.timeout_seconds`. Screen and build, which have
many items, checkpoint per item and resume cleanly.

---

## 3. Provenance is not reproducibility

The run manifest records what was used: configuration, rubric, prompts with content
digests, model id, tool versions, codebase HEAD. It **cannot** make a run
reproducible.

- Agents are nondeterministic. The same commit screened twice may score differently.
- A model id is not a fixed artifact. An endpoint can be repointed while the id
  stays the same.
- A resumed run spanning days may have judged different commits at different times.

Per-item records carry their own model id and timestamp, so the *mix* is auditable
at item granularity. Treat the manifest as an audit trail, not a guarantee.

Configuration drift detection narrows the problem — it refuses to continue a run
whose rubric, prompts, or model changed — but it cannot make agents deterministic.

---

## 4. Charliecloud only

Containers are Charliecloud SquashFS images or unpacked image directories.
`automaton.Sandbox` shells out to `ch-run`; Docker, Podman, and Apptainer images
are not interchangeable.

You must also supply an image that already contains the toolchain. Nothing is
injected at run time, for a specific reason: the emitted verifiers run under
abc-bench at grading time, in an environment with even fewer host paths available.
A toolchain that only exists because mining mounted it would produce verifiers that
work during construction and fail in production.

---

## 5. The codebase must be verifiable, and that is a real precondition

"A git repo and a container" understates what the workflow needs. It needs a
codebase whose changes can be **checked deterministically**, ideally by a *targeted*
test.

Poor fits:

- No automated tests, or tests that cannot be selected individually.
- Predominantly non-deterministic outcomes (timing, unseeded randomness,
  tolerance-only numerical comparison).
- Verification requiring hardware, licences, or services unavailable in-container.
- Very long builds with no targeted compile path. Not fatal — `verify_cost` is
  weighted, not a gate — but it will thin the results.

Run `preflight` first. It is cheap and converts this from a discovery made after
hundreds of agent invocations into a five-minute check.

---

## 6. Mechanical commit filters are coarse

`exclude_merges`, `max_files_changed`, `max_diff_lines`, and `paths` are cheap
heuristics applied before any model sees a commit, and they will occasionally
discard something good — a large mechanical refactor containing one genuine fix, for
instance.

This is a deliberate trade: filtering in Python costs nothing, whereas paying an
agent to score a commit 0 costs real money. Loosen them if you suspect
over-filtering; the selection report always states what each filter removed.

Merge commits are excluded by default because a merge's "change" is rarely a
coherent authored task and its first-parent diff is misleading about what happened.

---

## 7. Task quality still requires human review

The workflow produces *candidate* benchmarks. Enforced automatically:

- the starting state is a genuine repository snapshot with no post-cutoff objects;
- the verifier fails on the starting state and passes on the change (checked twice,
  the second time in a grader-like environment);
- all four task components exist.

**Not** enforced automatically — and not automatable with any confidence:

- **Fairness.** Whether everything the verifier requires is derivable from the task
  statement alone. The construction agent is instructed at length on this, and is
  asked to self-check, but a passing polarity check does not prove a fair task.
- **Shortcut resistance.** Whether a solver could satisfy the verifier without doing
  the work.
- **Difficulty calibration.** Whether the task is actually interesting.
- **Prompt leakage.** Whether the statement reveals too much of the reference
  approach.

Polarity is a floor, not a ceiling. Read the emitted `prompt.md` and
`verify_solution.sh` before trusting a task, and expect to discard some.

---

## 8. Cost is not capped

There is no built-in budget limit. A sweep over a large history is many agent
invocations, and the tool will run all of them.

Mitigations available to you: `selection` to size the corpus,
`execution.timeout_seconds` per agent, `execution.max_attempts` to bound retries,
and `--only` for targeted runs. Token usage is recorded per item and aggregated by
`status`, so spend is attributable after the fact — but it is not enforced before
it happens.

Validate on a small slice (`selection: {mode: recent, count: 20}`) before committing
to a full sweep.

---

## 9. Screening judges each commit in isolation

Each screening agent sees exactly one commit, with no view of the alternatives. It
cannot know whether its 75 is generous or harsh relative to the field.

This is what makes screening parallelizable at all, and the design accounts for it:
scores are treated as a **triage signal**, and the ranking stage — the first party to
see candidates side by side — is explicitly told it may disagree. Do not read
`benchmark_score` as a calibrated cross-commit ranking.

---

## 10. An unexplained Charliecloud bind-mount discrepancy

The predecessor pipeline recorded a genuine, unresolved anomaly: two stages used a
nested bind-mount pattern that, per the diagnosed `ch-run --write-fake` behaviour
and per manual reproduction, *should* have failed — yet they produced hundreds of
real results. The scratch directories and logs were cleaned up before anyone could
determine what command those working runs actually issued.

`abc-mine` sidesteps this rather than relying on behaviour nobody could
account for: **every** stage uses a single, non-nested mount, which was verified to
work. Everything an agent sees is materialized inside one host directory.

The consequence is section 4's requirement: the container must carry its own
toolchain, because nothing is injected beneath the mount point.

---

## 11. Not exercised end to end at scale in this form

Being straightforward about validation status:

- **Tested:** 378 offline tests covering the rubric scorer, prompt composition and
  the override gate, config validation, commit selection, the bundle-cutoff
  invariants (including the solution-leak assertion), and the verifier PASS/FAIL
  contract. Plus CLI smoke tests against the real Truchas repository and container.
- **Not yet exercised:** a full multi-thousand-commit sweep with live agents, and
  the build stage's polarity re-check against real agent-authored verifiers. Those
  need scheduler time, credentials, and budget.

The logic is carried over from a pipeline that did complete a real run (213 commits
screened, 15 selected, tasks constructed), but that pipeline's own notes record that
its Layer-2 path had not been re-run since a late bind-mount fix. Treat the first
full run here as a validation exercise, and start with a bounded slice.
