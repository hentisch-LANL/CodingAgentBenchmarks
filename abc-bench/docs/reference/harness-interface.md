# Harness Interface

Complete specification for the `run_benchmark.sh` standard interface.

## Overview

Every harness must provide a `run_benchmark.sh` script that implements this exact interface. The runner calls this script inside the final runtime container selected for the `(harness, task_group)` pair.

The point of this interface is to let the runner treat different agent tools in
a uniform way. A harness may wrap a CLI agent, a Python library, or another
execution model, but from the runner's perspective it still needs to accept the
same staged inputs and produce the same kind of output.

## Command Signature

```bash
./run_benchmark.sh \
  --workspace PATH \
  --task-prompt PATH \
  --system-prompt PATH \
  --group-prompt PATH \
  --models-config PATH \
  --harness-config PATH \
  --output-dir PATH \
  --timeout-seconds N \
  --task-env PATH
```

## Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--workspace` | Yes | Agent's working directory (contains the deliverable file) |
| `--task-prompt` | Yes | Path to `prompt.md` (task description) |
| `--system-prompt` | Yes | Path to `system_prompt.md` (agent instructions) |
| `--group-prompt` | No | Path to the task group's `group_prompt.md` (shared group-wide guidance, e.g. how to build/test the group's software stack). Only passed when the task group defines one. |
| `--models-config` | Yes | Path to `models_config.yaml` (exposed model settings) |
| `--harness-config` | Yes | Path to `harness_config.yaml` (harness-owned config blob) |
| `--output-dir` | Yes | Directory for logs (`harness.log`, etc.) |
| `--timeout-seconds` | Yes | Maximum execution time in seconds |
| `--task-env` | No | Path to task conda environment (`workspace/abc-bench-task-env`) |

## Models Configuration

The `models_config.yaml` contains:

```yaml
proxy_base_url: http://127.0.0.1:18080
api_key_env: ABC_BENCH_MODEL_PROXY_API_KEY
models:
  - name: example-model
    model: your-model-id
    base_url: http://127.0.0.1:18080
    opencode_model: abc-bench-proxy/example-model
    proxied_base_url: https://api.example.com/v1
    api: null
```

| Field | Description |
|-------|-------------|
| `proxy_base_url` | Shared proxy URL for the harness |
| `api_key_env` | Environment variable for proxy auth token |
| `models[].name` | Config-level model name exposed to the harness |
| `models[].model` | Upstream model identifier |
| `models[].base_url` | Effective URL the harness should call |
| `models[].opencode_model` | OpenCode-specific routed model name |
| `models[].proxied_base_url` | Original upstream URL |
| `models[].api` | Opaque API-style hint from provider/model config. The runner assigns it no meaning or default; the harness interprets it (e.g. selecting chat/completions vs. responses wiring) and applies its own default when it is `null`. |

The separate `harness_config.yaml` is not interpreted by ABC Bench. It is
passed through verbatim so each harness can define its own role mapping or
phase-specific settings.

The runner rewrites `base_url` to point at a sandbox-local localhost proxy. That
proxy is produced by `automaton.Sandbox` as part of the sandbox execution flow.
It runs in the same loopback-only network namespace as the harness and bridges
requests to a host-side broker (implemented by `automaton.proxy.ModelProxy`),
which then forwards them to `proxied_base_url`, injects authentication, and uses
`cert` when provided. If the upstream model API returns an error such as `401`,
`403`, or TLS verification failure, the proxy logs it to
`harness_output/model_proxy.log` and mirrors it to stderr during the benchmark
run.

This exists so the harness can keep model access without giving the agent a
general path out of the isolated benchmark environment.

When sandboxing is enabled, the runner stages an injected Python helper into the
per-run directory and launches it outside `ch-run` but inside the sandbox
network namespace. The helper serves the localhost proxy, executes the harness
and verification `ch-run` commands, and mirrors their stdout to both the normal
log files and the parent runner's live stream.

## Required Behaviors

### 1. Parse All Arguments

```bash
while [[ $# -gt 0 ]]; do
    case $1 in
        --workspace) WORKSPACE="$2"; shift 2 ;;
        --task-prompt) TASK_PROMPT="$2"; shift 2 ;;
        --system-prompt) SYSTEM_PROMPT="$2"; shift 2 ;;
        --models-config) MODELS_CONFIG="$2"; shift 2 ;;
        --harness-config) HARNESS_CONFIG="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --timeout-seconds) TIMEOUT_SECONDS="$2"; shift 2 ;;
        --task-env) TASK_ENV="$2"; shift 2 ;;
        *) echo "Unknown: $1" >&2; exit 1 ;;
    esac
done

# Validate required
for var in WORKSPACE TASK_PROMPT SYSTEM_PROMPT MODELS_CONFIG HARNESS_CONFIG OUTPUT_DIR TIMEOUT_SECONDS; do
    [[ -n "${!var:-}" ]] || { echo "Missing: --${var,,}" >&2; exit 1; }
done
```

### 2. Read Models Configuration

```bash
MODEL_ID=$(python - "${MODELS_CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
    print(data["models"][0]["model"])
PY
)

BASE_URL=$(python - "${MODELS_CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
    print(data.get("proxy_base_url") or data["models"][0]["base_url"])
PY
)

API_KEY_ENV=$(python - "${MODELS_CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    print(yaml.safe_load(f)["api_key_env"])
PY
)
```

### 3. Export API Credentials

```bash
if [[ -n "${API_KEY_ENV}" ]]; then
    API_KEY_VALUE="${!API_KEY_ENV}"
    [[ -z "${API_KEY_VALUE}" ]] && { echo "Error: ${API_KEY_ENV} not set" >&2; exit 1; }

    case "${MODEL_ID%%/*}" in
        openai) export OPENAI_API_KEY="${API_KEY_VALUE}" ;;
        anthropic) export ANTHROPIC_API_KEY="${API_KEY_VALUE}" ;;
        *) export "${API_KEY_ENV}"="${API_KEY_VALUE}" ;;
    esac
fi
```

### 4. Combine Prompts

The agent's prompt is assembled from up to three layers, in order: the
per-harness **system prompt**, the optional per-task-group **group prompt**, and
the per-task **task prompt**. The group prompt is inserted only when
`--group-prompt` is supplied and the file exists:

```bash
{
    echo "# System Instructions"; echo
    cat "${SYSTEM_PROMPT}"; echo; echo "---"; echo
    if [[ -n "${GROUP_PROMPT:-}" && -f "${GROUP_PROMPT}" ]]; then
        echo "# Task Group Instructions"; echo
        cat "${GROUP_PROMPT}"; echo; echo "---"; echo
    fi
    echo "# Task"; echo
    cat "${TASK_PROMPT}"
} > "${OUTPUT_DIR}/combined_prompt.md"
```

The group prompt is where a task group documents its shared software stack —
for example, a scientific-computing group can use it to tell the agent which
compilers exist and how to build and test the group's code with its provided
helper scripts. Keeping this out of the per-task `prompt.md` avoids repeating
build boilerplate in every task and keeps the harness `system_prompt.md` generic
across task types.

### 5. Change to Workspace

```bash
cd "${WORKSPACE}"
```

### 6. Add Task Environment to PATH

```bash
if [[ -n "${TASK_ENV}" && -d "${TASK_ENV}/bin" ]]; then
    export PATH="${TASK_ENV}/bin:${PATH}"
fi
```

### 7. Run Agent with Timeout

```bash
mkdir -p "${OUTPUT_DIR}"

timeout "${TIMEOUT_SECONDS}" my-agent run \
    --model "${MODEL_ID}" \
    --base-url "${BASE_URL}" \
    --prompt "${OUTPUT_DIR}/combined_prompt.md" \
    2>&1 | tee "${OUTPUT_DIR}/harness.log"

EXIT_CODE=$?
```

### 8. Handle Exit Codes

```bash
if [[ ${EXIT_CODE} -eq 124 ]]; then
    echo "TIMEOUT" | tee -a "${OUTPUT_DIR}/harness.log"
    exit 124
elif [[ ${EXIT_CODE} -ne 0 ]]; then
    echo "ERROR" | tee -a "${OUTPUT_DIR}/harness.log"
    exit ${EXIT_CODE}
fi

exit 0
```

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — agent completed |
| `1` | Error — agent or script failed |
| `124` | Timeout — exceeded `--timeout-seconds` |

## Logging

All output must be written to `${OUTPUT_DIR}/harness.log`:

```bash
# Using tee to write to file and stdout
timeout "${TIMEOUT_SECONDS}" my-agent run ... \
    2>&1 | tee "${OUTPUT_DIR}/harness.log"
```

The runner also captures container output separately.

In practice there are two useful log views:

- `${OUTPUT_DIR}/harness.log` for the harness's own logging and agent output
- `${OUTPUT_DIR}/ch-run.log` for the full container-level stdout/stderr captured
  by the runner

With network sandboxing enabled, the injected helper still preserves live log
streaming to the terminal while writing those files.

## Complete Template

```bash
#!/usr/bin/env bash
set -euo pipefail

# === Parse Arguments ===
while [[ $# -gt 0 ]]; do
    case $1 in
        --workspace) WORKSPACE="$2"; shift 2 ;;
        --task-prompt) TASK_PROMPT="$2"; shift 2 ;;
        --system-prompt) SYSTEM_PROMPT="$2"; shift 2 ;;
        --models-config) MODELS_CONFIG="$2"; shift 2 ;;
        --harness-config) HARNESS_CONFIG="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --timeout-seconds) TIMEOUT_SECONDS="$2"; shift 2 ;;
        --task-env) TASK_ENV="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# Validate required
for var in WORKSPACE TASK_PROMPT SYSTEM_PROMPT MODELS_CONFIG HARNESS_CONFIG OUTPUT_DIR TIMEOUT_SECONDS; do
    [[ -n "${!var:-}" ]] || { echo "Missing required argument: --${var,,}" >&2; exit 1; }
done

# === Extract Models Configuration ===
MODEL_ID=$(python - "${MODELS_CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
    print(data["models"][0]["model"])
PY
)

BASE_URL=$(python - "${MODELS_CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
    print(data.get("proxy_base_url") or data["models"][0]["base_url"])
PY
)

API_KEY_ENV=$(python - "${MODELS_CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    print(yaml.safe_load(f)["api_key_env"])
PY
)

# === Export API Credentials ===
if [[ -n "${API_KEY_ENV}" ]]; then
    API_KEY_VALUE="${!API_KEY_ENV}"
    [[ -z "${API_KEY_VALUE}" ]] && { echo "Error: ${API_KEY_ENV} not set in environment" >&2; exit 1; }

    case "${MODEL_ID%%/*}" in
        openai) export OPENAI_API_KEY="${API_KEY_VALUE}" ;;
        anthropic) export ANTHROPIC_API_KEY="${API_KEY_VALUE}" ;;
        example) export MODEL_API_KEY="${API_KEY_VALUE}" ;;
        *) export "${API_KEY_ENV}=${API_KEY_VALUE}" ;;
    esac
fi

# === Combine Prompts ===
cat "${SYSTEM_PROMPT}" "${TASK_PROMPT}" > "${OUTPUT_DIR}/combined_prompt.md"

# === Add Task Environment to PATH ===
if [[ -n "${TASK_ENV}" && -d "${TASK_ENV}/bin" ]]; then
    export PATH="${TASK_ENV}/bin:${PATH}"
fi

# === Run Agent ===
cd "${WORKSPACE}"
mkdir -p "${OUTPUT_DIR}"

timeout "${TIMEOUT_SECONDS}" my-agent run \
    --model "${MODEL_ID}" \
    --base-url "${BASE_URL}" \
    --prompt "${OUTPUT_DIR}/combined_prompt.md" \
    2>&1 | tee "${OUTPUT_DIR}/harness.log"

EXIT_CODE=$?
if [[ ${EXIT_CODE} -eq 124 ]]; then
    echo "TIMEOUT" | tee -a "${OUTPUT_DIR}/harness.log"
    exit 124
elif [[ ${EXIT_CODE} -ne 0 ]]; then
    echo "ERROR" | tee -a "${OUTPUT_DIR}/harness.log"
    exit ${EXIT_CODE}
fi

exit 0
```

## Harness-Specific Patterns

### OpenCode (CLI Agent)

```bash
# The harness chooses which exposed model to use.
timeout "${TIMEOUT_SECONDS}" opencode run \
    --dir "${WORKSPACE}" \
    --model "${MODEL_ID}" \
    --file "${OUTPUT_DIR}/combined_prompt.md" \
    --dangerously-skip-permissions \
    2>&1 | tee "${OUTPUT_DIR}/harness.log"
```

### Python Wrapper (Library Agent)

```bash
# run_benchmark.sh calls a Python wrapper
python run_agent.py \
    --workspace "${WORKSPACE}" \
    --prompt "${OUTPUT_DIR}/combined_prompt.md" \
    --models-config "${MODELS_CONFIG}" \
    --harness-config "${HARNESS_CONFIG}" \
    --output-dir "${OUTPUT_DIR}" \
    --timeout "${TIMEOUT_SECONDS}" \
    2>&1 | tee "${OUTPUT_DIR}/harness.log"
```

The wrapper (`run_agent.py`) merges ABC Bench config with agent's native config.

## Environment Variables Available

| Variable | Set By | Description |
|----------|--------|-------------|
| `ABC_BENCH_MODEL_PROXY_API_KEY` | Runner | Proxy auth token (`abc-bench-proxy`) |
| `OPENAI_API_KEY` | Harness | Exported from models config |
| `ANTHROPIC_API_KEY` | Harness | Exported from models config |
| `MODEL_API_KEY` | Harness | Exported from models config |
| `PATH` | Harness | Includes task env if provided |

## Directory Structure at Runtime

```
/workspace/                    # <-- WORKSPACE inside container
├── solution.py               # Agent edits this
├── README.md
└── abc-bench-task-env/       # <-- TASK_ENV (if exists)
    └── bin/
        └── python

/abc-bench/run/               # <-- per-run bind mount root inside container
├── workspace/                # Same directory as /workspace
├── harness/
├── prompt.md
├── group_prompt.md           # Only present when the task group defines one
├── models_config.yaml
├── harness_config.yaml
├── harness_output/           # <-- OUTPUT_DIR
└── verification/             # Injected after agent run

/abc-bench/run/harness_output/
├── harness.log               # Required output
├── combined_prompt.md        # System + task prompt
├── ch-run.log                # Container output (from runner)
├── model_proxy.log           # Host-side upstream proxy activity
├── usage.jsonl               # Per-request usage + cost records
└── ch-run.sandbox.log        # Sandbox launcher stdout/stderr
```

The runner bind-mounts only the single per-run result directory into the
container, at anonymous internal paths under `/abc-bench/run` and `/workspace`.
The host repo path is not mounted at the same absolute location inside the
container.

## Testing the Interface

### Manual Test

```bash
# Create test workspace
mkdir -p /tmp/test-workspace
cp tasks/smoke_test/hello-world-python/workspace/solution.py /tmp/test-workspace/

# Create minimal models config
cat > /tmp/test-models-config.yaml <<EOF
proxy_base_url: http://localhost:8000/v1
api_key_env: TEST_API_KEY
models:
  - name: test-model
    model: test-model
    base_url: http://localhost:8000/v1
    opencode_model: abc-bench-proxy/test-model
EOF
cat > /tmp/test-harness-config.yaml <<EOF
{}
EOF
export TEST_API_KEY="test-key"

# Run harness
cd harnesses/my-harness
./run_benchmark.sh \
  --workspace /tmp/test-workspace \
  --task-prompt tasks/smoke_test/hello-world-python/prompt.md \
  --system-prompt system_prompt.md \
  --models-config /tmp/test-models-config.yaml \
  --harness-config /tmp/test-harness-config.yaml \
  --output-dir /tmp/test-output \
  --timeout-seconds 60
```

### Verify Output

```bash
# Check exit code
echo $?

# Check logs
cat /tmp/test-output/harness.log

# Check solution was modified
cat /tmp/test-workspace/solution.py
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Missing argument parsing | Implement full argument parser with validation |
| Not exporting API keys | Map `api_key_env` to provider-specific vars |
| Not using timeout | Wrap agent in `timeout ${TIMEOUT_SECONDS}` |
| Wrong exit code | Return `0` for success, `124` for timeout, or a non-zero failure code |
| Not logging to harness.log | Use `tee "${OUTPUT_DIR}/harness.log"` |
| Not combining prompts | `cat system_prompt task_prompt > combined_prompt.md` |
| Ignoring task env | Add `${TASK_ENV}/bin` to PATH if exists |
| Not cd'ing to workspace | `cd "${WORKSPACE}"` before running agent |

## Validation

The runner validates harnesses via `Harness.validate()`:

```python
harness = Harness("my-harness")
errors = harness.validate()
# Checks: run_benchmark.sh, system_prompt.md, and the matching harness container template exist
```

## Harness Discovery

Harnesses are auto-discovered from `harnesses/` directory. `Harness` is now
provided by the `automaton` engine (constructed with an explicit `harness_dir`),
and the `list_harnesses()` staticmethod has been removed from abc_bench. The CLI
discovers available harness directories directly from the `harnesses/` layout.

## Next Steps

- [Adding Harnesses](../developer-guide/adding-harnesses.md) — Full guide to creating harnesses
- [Container System](../developer-guide/container-system.md) — Container build details
- [Architecture](../developer-guide/architecture.md) — System overview
