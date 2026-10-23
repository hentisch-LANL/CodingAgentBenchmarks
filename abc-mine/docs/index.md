# abc-mine

Mine the git history of an **arbitrary codebase** for agentic-coding benchmarks and
emit ready-to-run ABC Bench tasks (the sibling `../abc-bench` component).

Given a git repository and a container that can build and test it, three stages funnel
commit history down to finished benchmarks:

```text
screen   every selected commit   ->  per-commit rubric summary      (parallel)
rank     all viable summaries    ->  ranked shortlist               (one agent)
build    each shortlisted commit ->  a complete abc-bench task      (parallel)
```

Each stage increases scrutiny while reducing volume. Every agent runs inside your
container, so it reads the real source tree and — when warranted — compiles and tests
the code.

## Quick start

```bash
pip install -e ../automaton && pip install -e .

export LLM_BASE_URL=https://your-endpoint/v1
export LLM_MODEL=your-model-id
export LLM_API_KEY=...

abc-mine init      --config mine.yaml
abc-mine preflight --config mine.yaml --write-config
abc-mine screen    --config mine.yaml --parallel local --workers 8
abc-mine rank      --config mine.yaml
abc-mine build     --config mine.yaml --parallel local --workers 4
abc-mine export    --config mine.yaml --dest ../abc-bench
```

## Start with `preflight`

The workflow rests on a precondition that "a repo and a container" does not capture:
**the container must be able to verify a change deterministically**, ideally by a
targeted test. If it cannot, the workflow still runs — it just produces unusable
output, and you discover that after paying for hundreds of agent invocations.

`preflight` runs your configure/build/test commands in the real container, proves a
targeted test selection works, and *measures* the cost so every stage reasons from a
real number instead of a guess.

## Design commitments

**Scoring is deterministic.** Agents supply per-axis judgements; the total and the
disqualification decision are computed in Python. A run can be re-weighted afterwards
without re-invoking a single agent.

**The rubric is single-sourced.** The scorer, the prompt tables, and the JSON schema
agents are told to emit all derive from one definition, so they cannot drift apart.

**Starting states are built without agents.** A benchmark's "before" tree comes from a
git bundle-cutoff at the parent commit, with real history and provably no later
objects — the reference solution cannot be recovered from it.

**Verifier polarity is checked twice.** Once by the authoring agent, then again by the
pipeline on the shipped artifact in an environment mirroring the real grader. Only
verified tasks are exported.

**Configuration is locked per run.** A sweep may span days. Changes that would make
results incomparable — rubric, prompts, model — are refused with a diff rather than
silently producing a mixed run.

**Serial by default.** No MPI required. Parallelism is opt-in and degrades gracefully.

## Documentation

| Document | Contents |
|---|---|
| [Configuration](guide/configuration.md) | Every config key |
| [Prompt customization](guide/prompt-customization.md) | Slots and gated overrides |
| [The scoring rubric](guide/rubric.md) | Axes, gates, re-weighting |
| [Checkpointing and resume](guide/checkpointing.md) | Resume, drift detection, provenance |
| [Limitations](guide/limitations.md) | **What this deliberately does not do** |
| [API reference](api-reference/index.md) | Module documentation |

Read [Limitations](guide/limitations.md) before a first real run. It is explicit about
single-context ranking, what human review is still required for, and what has not yet
been exercised at scale.
