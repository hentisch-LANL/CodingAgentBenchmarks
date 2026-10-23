# Understanding Results

Guide to reading benchmark output and diagnosing failures.

ABC Bench records a small set of benchmark outcomes. It does not presently try
to assign richer grades such as partial credit or score vectors. Instead, it
writes the result together with the logs, workspace, and metadata needed to
understand how that result was produced.

## Result Layout

Each benchmark run creates a directory organized by agent archive, task group,
and task id:

```text
results/
  <agent_archive_dir>/
    <task-group>/
      <task-id>/
        <timestamp>_<agent>_<task-id>/
          workspace/
          harness_output/
          verification/
          build_env.log
          verify.log
          prompt.md
          build_env.sh
          harness/
          models_config.yaml
          harness_config.yaml
          result.json
          metadata.json
          usage.json
```

## Start With These Files

- `result.json`: top-level benchmark outcome
- `metadata.json`: expanded run metadata and file locations
- `usage.json`: aggregated token and cost accounting, including prompt-cache reads and writes when reported
- `workspace/solution.py`: the agent's implementation
- `harness_output/ch-run.log`: container process output
- `harness_output/model_proxy.log`: proxied API traffic and upstream errors
- `verify.log`: verification output

## `result.json`

`result.json` is the primary machine-readable result.

Example:

```json
{
  "task": "eos-metropolis-1953",
  "task_group": "smoke_test",
  "task_ref": "smoke_test/eos-metropolis-1953",
  "agent": "opencode-hybrid-nemotron-gemma-4-31b",
  "harness": "opencode-hybrid",
  "models": ["example-model", "example-small"],
  "status": "PASS",
  "usage": {
    "usage_available": true,
    "totals": {
      "input_tokens_standard": 18342,
      "input_tokens_cache_read": 8192,
      "input_tokens_cache_write": 2048,
      "input_tokens_cache_read_cost": 0.0006,
      "input_tokens_cache_write_cost": 0.0031,
      "input_tokens_standard_cost": 0.0183,
      "output_tokens": 2147,
      "output_cost": 0.0107,
      "reasoning_tokens": 0,
      "total_input_tokens": 28582,
      "total_tokens": 30729,
      "total_cost": 0.0327,
      "duration_seconds": 41.2,
      "tokens_per_second": 52.1
    }
  }
}
```

`usage.json` is aggregated from per-request records in
`harness_output/usage.jsonl`. Token accounting distinguishes:

- `input_tokens_standard`: input tokens billed at the normal input rate
- `input_tokens_cache_read`: input tokens read from a prompt cache
- `input_tokens_cache_write`: input tokens written into a prompt cache when the provider reports them separately
- `output_tokens`: total output/completion tokens, including reasoning-token subsets when reported

Throughput accounting captures how fast tokens were generated:

- `duration_seconds`: summed wall-clock time of the underlying upstream model
  requests (per request: from sending the request to reading the full response)
- `tokens_per_second`: output tokens per second of request time
  (`output_tokens / duration_seconds`); `0.0` when no request duration is
  available

Cost accounting follows the same split:

- `input_tokens_standard_cost`: cost for standard input tokens
- `input_tokens_cache_read_cost`: cost to read prompt tokens from cache on cache hits
- `input_tokens_cache_write_cost`: cost to write prompt tokens into cache when the provider reports and bills them separately
- `output_cost`: cost for model output tokens

By default, cache-write pricing is zero unless the model config sets
`cache_write_input_per_1m`. That keeps OpenAI-style accounting simple while also
supporting providers that bill cache writes separately.

For a fuller picture, `usage.json` also includes:

- `by_model`: the same token, cost, and throughput breakdown grouped by configured model name
- `status_counts`: how many proxied requests had reported, missing, or malformed usage
- `endpoint_counts`: how many requests went through each proxied endpoint

For streaming providers, ABC Bench preserves the harness-visible streaming
behavior and separately parses usage from the streamed response payload when the
provider includes usage information in SSE events.

## `metadata.json`

`metadata.json` includes the same core identifiers plus elapsed time, failure
phase, paths for workspace and logs, and host-machine metadata.

This is usually the best file for automation or postprocessing.

## Result Statuses

- `PASS`: verification succeeded
- `FAIL`: verification failed, including verification crashes. This also covers
  the case where the agent exhausted the model's context window (an upstream
  "context length exceeded" error): running out of context is treated as the
  agent failing the task, not as a harness/infrastructure bug. Such runs record
  `failure_phase: "context_length"` in `metadata.json`.
- `ENDPOINT_ERROR`: the model proxy could not obtain a complete upstream
  response within the 15-minute total deadline
- `HARNESS_ERROR`: the benchmark failed before verification while building the
  task environment or while the model was working inside the harness/runtime
  stack

Host-machine metadata includes:

- `hostname`: the host running `abc-bench`
- `host_metadata.os`: host OS details
- `host_metadata.cpu`: host CPU model, architecture, and logical core count
- `host_metadata.memory.total_bytes`: total host memory in bytes
- `host_metadata.gpus`: NVIDIA GPU details when `nvidia-smi` is available

## Verification Rule

Verification returns `PASS` only when both are true:

1. the verification command exits with code `0`
2. the last `PASS` or `FAIL` line in the log is `PASS`

Otherwise verification returns `FAIL`. This avoids false positives from scripts
that print `PASS` but fail overall.

## Common Failure Triage

### The agent started, then nothing useful happened

Check:

- `harness_output/model_proxy.log`

This is the first place to look for:

- TLS failures
- authentication failures
- provider access errors
- upstream model errors
- proxy retries and endpoint deadline exhaustion

### The agent wrote code but the benchmark failed

Check:

- `workspace/solution.py`
- `verify.log`
- `verification/test_solution.py`

### The task environment failed to build

Check:

- `build_env.log`

## Group-Aware Aggregation

Because results include `task_group` and `task_ref`, aggregating by group is
straightforward.

Example:

```bash
jq -r '"\(.task_group)\t\(.task)\t\(.status)"' results/*/*/*/*/metadata.json
```

## Manual Verification

To rerun verification inside an existing result directory:

```bash
cd results/<agent_archive_dir>/<task-group>/<task-id>/<run>/workspace
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
```

## See Also

- [Running Benchmarks](running-benchmarks.md)
- [Verification](../reference/verification.md)
- [Adding Tasks](../developer-guide/adding-tasks.md)
