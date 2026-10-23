# Agent Pool

The **agent pool** coordinates a batch of embarrassingly-parallel agent *jobs*
and gathers their outputs under a single checkpoint root, with crash-safe
resume. It is deliberately *content-free*: the caller supplies a `runner`
callable that performs the work for one job (typically building a
[`Sandbox`][automaton.sandbox.Sandbox] run and staging assets) and writes all
artifacts into that job's `output_dir`.

## Concepts

- **`AgentJob`** — a small canonical wrapper the pool assigns around a
  caller-defined, opaque `payload`. Its `job_id` (zero-padded, derived from
  insertion order) and `output_dir` (`<checkpoint_root>/<job_id>/`) are frozen
  the moment jobs are handed to the pool.
- **`Runner`** — `Callable[[AgentJob], JobResult | None]`. Does the work, writes
  artifacts into `job.output_dir`, and returns a small `JobResult`. Raising is
  treated as failure.
- **`JobResult`** — the *only* thing that crosses the MPI boundary. Heavy
  artifacts stay on the (shared) filesystem; `JobResult` carries paths as
  strings plus a small JSON-serializable `summary` (e.g. usage totals).
- **Checkpoint store** — one subdirectory per job under a single root, plus an
  aggregated `manifest.jsonl`. Completion is recorded so a re-run can resume.

## Resume semantics

`pending_jobs()` decides which jobs still need to run. By default a job is
"done" iff a valid `DONE.json` marker exists in its `output_dir` (written on
success only). Unfinished job directories are wiped and re-run — an unfinished
agent is never resumed mid-flight.

### Custom completion (caller-owned records)

A caller that records completion in its own on-disk format (e.g. a
`result.json` it writes itself) can override the default marker logic:

```python
from automaton.pool import SerialAgentPool

def done_check(job):
    # "Done" iff our own result exists and passed.
    result = job.output_dir / "result.json"
    return result.is_file() and "\"status\": \"PASS\"" in result.read_text()

def clean_job(job):
    # Wipe a stale/incomplete attempt before re-running it.
    ...

pool = SerialAgentPool(
    payloads, runner,
    checkpoint_root=root,
    done_check=done_check,
    clean_job=clean_job,
    write_marker=False,   # we track completion ourselves; no DONE.json
)
```

When `write_marker=False`, `completed_results()` synthesizes a success
`JobResult` for each already-done job so the returned batch still covers the
whole job set.

## Schedulers

The base `AgentPool` owns canonicalization, resume selection, and safe
single-job execution; concrete subclasses implement `run()`:

### `SerialAgentPool`

Runs pending jobs one at a time in the current process. This is a first-class
scheduler for single-process execution — **not** a fallback. The pool never
silently degrades from MPI to serial; you choose the scheduler explicitly.

```python
from automaton.pool import SerialAgentPool

results = SerialAgentPool(payloads, runner, checkpoint_root=root).run()
```

### `MPIAgentPool`

Runs jobs across `MPI.COMM_WORLD`. **Rank 0 coordinates *and* works**: a
background dispatcher thread hands out pending job indices and collects results
while rank 0's main thread also executes jobs (fed over an in-process queue).
Ranks 1..N-1 are pure workers. Only `JobResult` metadata crosses MPI; artifacts
live on the shared filesystem.

```python
from automaton.pool import MPIAgentPool  # requires the automaton[mpi] extra

# collective: EVERY rank must call run()
results = MPIAgentPool(payloads, runner, checkpoint_root=shared_root).run()
```

Launch with an MPI launcher, e.g. `mpiexec -n <ranks> python your_driver.py` or
`srun -n <ranks> python your_driver.py`. A single-rank communicator
(`size == 1`) is valid: rank 0 executes every job itself. `run()` is a
**collective** operation — every rank must call it together or the coordinator
will block.

`MPIAgentPool` requires `mpi4py` (the `automaton[mpi]` extra); it is imported
lazily so the rest of `automaton.pool` works without it.

!!! note "Shared filesystem"
    The MPI pool assumes each job's `output_dir` is on a filesystem visible to
    all ranks (results are aggregated there). If ranks run on separate nodes
    with node-local scratch, ensure the checkpoint root is on shared storage, or
    have the runner build any node-local prerequisites (e.g. containers) on
    every rank before the pool dispatches.
