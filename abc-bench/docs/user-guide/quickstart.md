# Quickstart

Run your first benchmark end to end.

## 1. Install

```bash
git clone https://github.com/example/CodingAgentBenchmarks.git
cd CodingAgentBenchmarks/abc-bench
pip install -e ../automaton
pip install -e ".[dev]"
```

You also need Charliecloud available on the host.

## 2. Configure Models And An Agent

Create or edit `~/.abc-bench/config.yaml`.

This file tells ABC Bench how to reach the model APIs you want a harness to use.
In other words, it is the connection between a benchmark run and an actual
model endpoint. The configuration names the models you want available to
harnesses, and describes how each of those names should be resolved into an API base
URL, credentials, and model identifier.

In the provider-based form, you define a provider once and then reference it
from one or more models. That is useful when several model names share the same
service endpoint and credential source.

Example:

```yaml
providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null

models:
  example-model:
    provider: example
    model: your-model-id

agents:
  opencode-default:
    harness: opencode
    models:
      - example-model
    harness_config: {}
```

Then export the API key required by `api_key_env`:

```bash
export MODEL_API_KEY="your-api-key"
```

## 3. Build the Runtime Container

Build the container for the harness and task group you want to run:

ABC Bench does not run tasks directly in your host Python environment. Instead,
it builds a Charliecloud runtime container that supplies the software stack for
the selected task group and harness. The point of this is to make task
execution, harness execution, and verification happen in a controlled runtime
environment.

```bash
abc-bench build-container opencode/smoke_test
```

This automatically builds parent layers as needed.

Useful alternatives:

```bash
abc-bench build-container base
abc-bench build-container opencode
abc-bench build-container opencode-hybrid
abc-bench build-container --all
```

## 4. Run a Smoke Test

```bash
abc-bench run \
  --task smoke_test/hello-world-python \
  --agent opencode-default
```

You can also use the short task id when it is unambiguous:

```bash
abc-bench run \
  --task hello-world-python \
  --task-group smoke_test \
  --agent opencode-default
```

## 5. Inspect Results

Results are organized by agent archive, task group, and task id.

```bash
cat results/*/smoke_test/hello-world-python/*/result.json
cat results/*/smoke_test/hello-world-python/*/workspace/solution.py
cat results/*/smoke_test/hello-world-python/*/verify.log
```

The most useful files are usually:

- `result.json`
- `metadata.json`
- `usage.json`
- `harness_output/`
- `verify.log`
- `workspace/`

For request-level model accounting and parsing/debugging, also inspect
`harness_output/usage.jsonl` and `harness_output/model_proxy.log`.

## 6. Try a Larger Task

The two smoke-test tasks used above are intentionally simple. They are there to
exercise the benchmark flow with minimal setup. `hello-world-python` is the
smallest useful example. `hello-world-prank-python` is similar in size but is
designed to check whether the agent is actually following the benchmark rather
than guessing the usual answer.

The third smoke-test task, `eos-metropolis-1953`, is the first task in the
repository that looks like the kind of scientific workload the project is meant
to support. It asks the agent to implement pieces of a small Metropolis Monte
Carlo simulation and is therefore a much better demonstration of the full run
machinery than the hello-world tasks.

```bash
abc-bench run \
  --task smoke_test/eos-metropolis-1953 \
  --agent opencode-default \
  --timeout 3600
```

If you want to exercise the planner/builder flow explicitly:

```bash
abc-bench build-container opencode-hybrid/smoke_test
abc-bench run \
  --task smoke_test/eos-metropolis-1953 \
  --agent opencode-hybrid-gpt54-gemma-4-31b \
  --timeout 3600
```

## Common Problems

### Missing runtime container

```bash
abc-bench build-container opencode/smoke_test
```

### Charliecloud missing

```bash
ch-run --version
```

### API key missing

Check both:

- the `api_key_env` field in `~/.abc-bench/config.yaml`
- the corresponding shell environment variable

### Unsure what the CLI sees

```bash
abc-bench list-task-groups
abc-bench list-tasks
abc-bench list-containers
abc-bench list-models
abc-bench list-agents
```

## Next Steps

- [Configuration](configuration.md)
- [Running Benchmarks](running-benchmarks.md)
- [Managing Containers](managing-containers.md)
- [Understanding Results](results.md)
