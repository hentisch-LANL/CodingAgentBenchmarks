# Running Benchmarks

Guide to executing grouped benchmarks with ABC Bench.

## Basic Syntax

```bash
abc-bench run --task TASK --agent AGENT_ID [OPTIONS]
```

## Task Selection

Tasks can be referenced either as canonical `group/task` refs or as short task
ids.

Task groups are the project's way of organizing benchmarks that share a general
software stack or belong together operationally. A task group's `group.yaml`
defines group-level metadata, and the container system uses the group to choose
the final runtime container used for the benchmark.

Canonical form:

```bash
abc-bench run --task smoke_test/eos-metropolis-1953 ...
```

Short form with explicit group:

```bash
abc-bench run --task eos-metropolis-1953 --task-group smoke_test ...
```

Use `--task-group` when the short id is ambiguous or when you want to be
explicit.

## Required Arguments

| Argument | Description |
|----------|-------------|
| `--task` | Canonical task ref or short task id |
| `--agent` | Agent identifier from config |

## Common Options

| Argument | Description |
|----------|-------------|
| `--task-group GROUP` | Explicit task group when `--task` is not canonical |
| `--timeout SECONDS` | Maximum execution time |
| `--dry-run` | Validate setup without running |
| `--no-stream-output` | Disable live log streaming |
| `--no-clean-env` | Preserve task environment |
| `--no-sandbox-network` | Disable loopback-only sandboxing for harness and verification |
| `--results-dir PATH` | Organized archive root |
| `--output-dir PATH` | Standalone output directory |

## Examples

Smoke test:

```bash
abc-bench run \
  --task smoke_test/hello-world-python \
  --agent opencode-default
```

Short task id plus group:

```bash
abc-bench run \
  --task eos-metropolis-1953 \
  --task-group smoke_test \
  --agent opencode-default
```

Dry run:

```bash
abc-bench run \
  --task smoke_test/eos-metropolis-1953 \
  --agent opencode-default \
  --dry-run
```

## Runtime Selection

The runner resolves the final runtime container from:

```text
(harness, task_group)
```

If the matching runtime is missing, build it first:

```bash
abc-bench build-container opencode/smoke_test
abc-bench build-container opencode-hybrid/smoke_test
```

## Network Sandboxing

By default, `build_env.sh` runs with host networking, but the harness and
verification run in a loopback-only network namespace. This prevents the agent
from reaching external networks directly while still allowing it to use a local
model endpoint at `127.0.0.1`.

The reason for this extra indirection is simple: the benchmark should control
exactly what network access the agent gets, rather than letting a model-driven
tool escape the isolated run and call arbitrary external services directly.

That localhost endpoint is sandbox-local. ABC Bench bridges it to a host-side
broker that owns credentials and talks to the real upstream model API.

Internally, ABC Bench stages a small Python helper into the per-run directory
and launches it inside the sandbox namespace. That helper serves the
sandbox-local proxy and then runs both the harness and verification commands.

Use `--no-sandbox-network` to opt out and run harness and verification with host
networking.

## What A Run Produces

Each run creates a directory organized by agent archive, task group, and task:

```text
results/
  <agent_archive_dir>/
    <task-group>/
      <task-id>/
        <timestamp>_<agent>_<task-id>/
```

Useful files inside a run directory:

- `workspace/`
- `harness_output/`
- `verify.log`
- `result.json`
- `metadata.json` (including wall-clock timing and host-machine metadata)
- `usage.json` (aggregated tokens and cost)

## Monitoring Runs

Most useful files while debugging:

- `workspace/solution.py`
- `harness_output/harness.log`
- `harness_output/ch-run.log`
- `harness_output/ch-run.sandbox.log`
- `harness_output/model_proxy.log`
- `harness_output/usage.jsonl`
- `verify.log`

If a run appears to stall right after the agent starts, inspect
`harness_output/model_proxy.log` first. Authentication, TLS, and model-access
errors tend to show up there. Proxy retries and endpoint deadline exhaustion are
also logged there and printed live to the terminal, but they are not written
into the harness logs.

If a sandboxed run appears quiet in the terminal, inspect
`harness_output/ch-run.log`, `harness_output/ch-run.sandbox.log`, and `harness_output/harness.log`. Those files should
contain the agent session even when the harness is running under the network
sandbox.

If model requests appear to succeed but cost/token accounting looks wrong,
inspect `harness_output/usage.jsonl` together with `harness_output/model_proxy.log`.
ABC Bench records per-request parsed usage there before aggregating it into
`usage.json`.

If `--timeout` is omitted, ABC Bench does not impose a benchmark-level timeout
by default. The model proxy still enforces a 15-minute per-attempt response
timeout and a 1-hour total deadline for any single upstream query.

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Benchmark completed; inspect `result.json` for PASS, FAIL, ENDPOINT_ERROR, or HARNESS_ERROR |
| `1` | Execution error |
| `2` | Invalid arguments or configuration |
| `124` | Harness timeout |

## Multiple Runs

For batch execution, use `run-tasks` with quoted glob patterns over canonical
`group/task` refs:

```bash
abc-bench run-tasks 'smoke_test/*' --agent opencode-default
```

Multiple patterns are unioned, duplicates are removed, and tasks run once in
sorted order:

```bash
abc-bench run-tasks \
  'smoke_test/hello-world-*' \
  'smoke_test/eos-*' \
  --agent opencode-default
```

Quote patterns so your shell passes them through unchanged. Matching is done
only against canonical task refs like `smoke_test/eos-metropolis-1953`, not
against filesystem paths under `tasks/`.

If some patterns match nothing, `abc-bench` warns and continues as long as at
least one pattern matched. If none of the supplied patterns match any canonical
task ref, the command exits with an error.

## Attempts And Resumability

A single coding-agent session is stochastic, so a task is usually run several
times. Use `--attempts N` to run each matched task `N` times:

```bash
abc-bench run-tasks 'smoke_test/*' --agent opencode-default --attempts 4
```

Each attempt is stored under its own `attempt_NN/` subdirectory beneath the
task's archive directory (a single attempt keeps the flat layout). Resumability
is intrinsic to the attempt formalism, so there is no separate skip flag:

- A batch targets **exactly** `N` attempts per task and never exceeds it.
- Attempts that finished with a **conclusive** verdict (`PASS`/`FAIL`) are kept.
- Attempts that are missing or ended **inconclusively** (endpoint/harness/
  internal error) are deleted and re-run.

So re-running the same command with the same `--attempts N` only fills the gaps:
if three of four attempts completed and the fourth crashed, only the fourth is
run again. (The former `--no-repeat-tasks` flag has been removed; `--attempts`
subsumes it.)

## Parallel Execution With MPI

For large batches, distribute the `(task, attempt)` jobs across MPI ranks with
`--mpi`, launched under an MPI launcher (rank 0 both dispatches work and runs
jobs; no rank sits idle):

```bash
srun --partition=shared-spr --nodes=4 \
  abc-bench run-tasks 'smoke_test/*' --agent opencode-default --attempts 4 --mpi
```

This requires the `mpi4py` dependency (`pip install 'abc-bench[mpi]'`). Every
rank builds the runtime containers it needs into its node-local image store and
then synchronizes at a barrier before any dispatch begins, so heterogeneous or
node-local scratch is fine. Resumability works identically under `--mpi`.

Lower-level sequential shell loop example:

```bash
for task in hello-world-python hello-world-prank-python eos-metropolis-1953; do
  abc-bench run --task "$task" --task-group smoke_test --agent opencode-default
done
```

## See Also

- [Quickstart](quickstart.md)
- [Managing Containers](managing-containers.md)
- [Understanding Results](results.md)
- [CLI Reference](../reference/cli.md)
