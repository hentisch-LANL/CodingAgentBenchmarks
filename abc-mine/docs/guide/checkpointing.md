# Checkpointing, resume, and configuration drift

A sweep over a large history is not atomic. It may span several allocations across
days, be interrupted, and be resumed under a different parallel backend. Two
mechanisms make that safe: per-stage checkpoints, and a configuration lock.

## Resume by durable artifact

No stage relies on a backend's internal bookkeeping. Each resumes by re-reading
files a human can also read:

| Stage | Source of truth | Resume behaviour |
|---|---|---|
| screen | `screen/summaries/<sha>.json` | Commits with a summary are skipped. |
| rank | `rank/shortlist.json` | Single-shot; `--force` to redo. |
| build | `build/manifest.jsonl` | Tasks recorded `ok` are skipped; partial task dirs are removed and rebuilt. |

Consequences worth relying on:

- **Backend-independent.** Start serially to debug, resume under MPI. The checkpoint
  does not care.
- **All-or-nothing per item.** A commit either has a complete summary or nothing.
  There is no partial state, and no way for a half-finished judgement to reach the
  next stage.
- **Crash-safe.** Summaries are written temp-then-rename; a torn write cannot be
  mistaken for a completed item.

Just re-run the same command:

```bash
abc-mine screen --config mine.yaml     # continues where it stopped
abc-mine status --config mine.yaml     # where things stand
```

## Failed items are recorded, not forgotten

Failures append to `attempts.jsonl` with a reason, timestamp, and model id.

This closes a real gap in the predecessor pipeline, where a failed screen wrote
*nothing*: "never attempted" and "failed six times" were indistinguishable, retries
were unbounded across resumes, and the transcripts had already been deleted.

- Items that have failed `execution.max_attempts` times (default 2) are **skipped**
  rather than retried forever, so one pathological commit cannot consume a budget
  sized for a sweep. Raise the setting to give them another chance.
- `execution.keep_logs` controls transcript retention: `none`, `failed` (default),
  or `all`. The default keeps exactly what is needed to diagnose a failure without
  retaining gigabytes of logs for thousands of successes.

## The configuration lock

`init` writes `run.json` recording the resolved configuration — rubric, selection,
toolchain, prompt content digests, model id — plus provenance: codebase HEAD and
commit count, container digest, tool versions, who created it and when.

Every later stage compares its own configuration against that lock.

### Why

Without it, you can screen half a history at one set of rubric weights and the other
half at another, then rank the union as though the scores were comparable. They would
not be, and nothing in the output would say so. The lock makes that loud instead of
silent.

### Severity

| Severity | Examples | Behaviour |
|---|---|---|
| **critical** | rubric, selection, prompt content, toolchain commands, codebase, **model id** | **Blocks** with a diff |
| warning | workers, parallel backend, retention, timeouts, container digest | Reported, proceeds |
| exempt | endpoint URL, scratch directory | Noted only |

Model id is critical on purpose: screening half a history with a cheap model and half
with a frontier model produces incomparable scores — precisely what the lock exists to
prevent. The endpoint *URL* is exempt because it legitimately rotates.

Unknown keys are treated as critical (fail-closed), so a field added in a future
version cannot slip through unnoticed.

Prompt drift is detected by **content digest**, not path — editing a slot file in
place counts, because it changes what every subsequent agent is told.

### Resources are recorded, not gated

One category is deliberately **outside** the fingerprint: how much compute a stage
was given. CPU counts, worker counts, and `BUILD_JOBS` change how *fast* a run goes,
not *what it decides* — screening a commit with an 8-wide or a 224-wide build should
reach the same verdict. Gating on them would make it impossible to resume a sweep on
a differently-sized allocation, which is exactly what resumability exists for.

They are not entirely inert, though, which is why they are still recorded. A build's
measured duration depends on its width, and that duration reaches the agents:
`toolchain.full_build_seconds` is rendered into every prompt, and the rubric's
`verify_cost` axis asks agents to weigh verification cost against it. A figure
measured with a whole node's cores and one measured under a modest cgroup make
"expensive" mean different things.

So the measured timings sit in the fingerprint at **warning** severity — a change is
reported, never blocking — and each stage invocation records the resource environment
that produced it:

```bash
abc-mine status --config mine.yaml --verbose
```

```text
Stage history:
  2026-09-27T09:01:14Z  screen     on cn883 job 111
    build_jobs=16 cpus_per_task=16
    model=... parallel=local workers=8
  2026-09-27T09:01:18Z  screen     on cn901 job 222
    build_jobs=8 cpus_per_task=64 ntasks=4
    model=... parallel=local workers=8
```

That is enough to interpret a changed timing after the fact without preventing the
resume that produced it.

### What a block looks like

```text
error: CONFIGURATION DRIFT (critical) — these change how items are judged,
so existing and future results would not be comparable:
    model.id: cheap-model  ->  frontier-model

Refusing to continue. Either restore the settings above, start a new
run (a fresh run.id keeps each set of results internally consistent),
or, if you understand that this run's outputs will be a mixture,
re-invoke with --allow-config-drift.
```

### Accepting drift

`--allow-config-drift` proceeds and records the change permanently:

```json
"accepted_drift": [
  {"at": "2026-09-22T04:21:14Z",
   "changes": [{"key": "model.id", "from": "cheap-model", "to": "frontier-model"}]}
]
```

The lock is then updated to the accepted state, so the *next* invocation is compared
against what is actually in force. `status` flags such runs as mixed.

## Provenance, not reproducibility

The manifest records what was used. It cannot make a run reproducible: agents are
nondeterministic, and a model id can be repointed behind a stable name. Per-item
records carry their own model id and timestamp, so the mix is auditable at item
granularity — but re-running will not reproduce a run byte-for-byte. See
[`limitations.md`](limitations.md#3-provenance-is-not-reproducibility).

## Stage history

Every stage invocation is appended with timestamp, host, and Slurm job id, so a run
assembled from several allocations can be reconstructed:

```bash
abc-mine status --config mine.yaml --verbose
```

## Scratch and output

Heavy throwaway state — worktrees, build trees, transcripts — goes to
`execution.scratch_dir` (default `/var/tmp`), which should be **node-local**, not a
shared or Lustre filesystem. Only durable artifacts land under `data_root`, which
keeps a long sweep from accumulating many gigabytes of unread material on shared
storage.
