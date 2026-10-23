# Harness Interface

Complete specification for the harness run script standard interface.

## Overview

Every harness must provide a `run_benchmark.sh` script that implements this
interface. The caller's `Sandbox.run_agent` calls this script inside the final
runtime container selected for the `(harness, runtime layer)` pair.

The point of this interface is to let the engine treat different agent tools in
a uniform way. A harness may wrap a CLI agent, a Python library, or another
execution model, but from the engine's perspective it still needs to accept the
same staged inputs and produce the same kind of output.

## Command Signature

```bash
./run_benchmark.sh \
  --workspace PATH \
  --task-prompt PATH \
  --system-prompt PATH \
  --models-config PATH \
  --harness-config PATH \
  --output-dir PATH \
  --timeout-seconds N \
  --task-env PATH
```

## Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--workspace` | Yes | Agent's working directory (the deliverable workspace) |
| `--task-prompt` | Yes | Path to the task prompt file |
| `--system-prompt` | Yes | Path to the system prompt file |
| `--models-config` | Yes | Path to `models_config.yaml` (exposed model settings) |
| `--harness-config` | Yes | Path to `harness_config.yaml` (harness-owned config blob) |
| `--output-dir` | Yes | Directory for logs (`harness.log`, etc.) |
| `--timeout-seconds` | Yes | Maximum execution time in seconds |
| `--task-env` | No | Path to a task environment directory (if any) |

## Models Configuration

The `models_config.yaml` is produced by `Sandbox.harness_models_config`. When
sandboxing is enabled, models are routed through the loopback proxy:

```yaml
proxy_base_url: http://127.0.0.1:18080
api_key_env: AUTOMATON_MODEL_PROXY_API_KEY
models:
  - name: example-model
    model: your-model-id
    base_url: http://127.0.0.1:18080
    opencode_model: automaton-proxy/example-model
    proxied_base_url: https://api.example.com/v1
    api: null
```

| Field | Description |
|-------|-------------|
| `proxy_base_url` | Shared proxy URL for the harness (sandbox-local) |
| `api_key_env` | Environment variable for proxy auth token |
| `models[].name` | Config-level model name exposed to the harness |
| `models[].model` | Upstream model identifier |
| `models[].base_url` | Effective URL the harness should call (sandboxed: proxy) |
| `models[].opencode_model` | Routed model name using the engine provider id |
| `models[].proxied_base_url` | Original upstream URL |
| `models[].api` | Opaque API-style hint from provider/model config. The engine assigns it no meaning or default; the harness interprets it (e.g. selecting chat/completions vs. responses wiring) and applies its own default when it is `null`. |

The separate `harness_config.yaml` is not interpreted by the engine. It is
passed through verbatim so each harness can define its own role mapping or
phase-specific settings.

The engine rewrites `base_url` to point at a sandbox-local localhost proxy. That
proxy runs in the same loopback-only network namespace as the harness and
bridges requests to a host-side broker, which then forwards them to
`proxied_base_url`, injects authentication, and uses `cert` when provided. If
the upstream model API returns an error such as `401`, `403`, or TLS
verification failure, the proxy logs it to `harness_output/model_proxy.log` and
mirrors it to stderr during the run.

This exists so the harness can keep model access without giving the agent a
general path out of the isolated container.

When sandboxing is enabled, the engine stages an injected Python helper into the
per-run directory and launches it outside `ch-run` but inside the sandbox
network namespace. The helper serves the localhost proxy, executes the harness
`ch-run` command, and mirrors its stdout to both the normal log files and the
parent engine's live stream.

The engine-local identifiers (`api_key_env`, `api_key_value`,
`proxy_provider_id`, `sandbox_proxy_port`) are configurable via `Sandbox`
constructor parameters, with the defaults above.

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
        *) export "${API_KEY_ENV}=${API_KEY_VALUE}" ;;
    esac
fi
```

### 4. Combine Prompts

```bash
mkdir -p "${OUTPUT_DIR}"
cat "${SYSTEM_PROMPT}" "${TASK_PROMPT}" > "${OUTPUT_DIR}/combined_prompt.md"
```

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
timeout "${TIMEOUT_SECONDS}" my-agent run ... \
    2>&1 | tee "${OUTPUT_DIR}/harness.log"
```

## Runtime Directory Structure

```
/workspace/                    # <-- WORKSPACE inside container
├── <deliverable files>       # Agent edits these

/automaton/run/               # <-- per-run bind mount root inside container
├── workspace/                # Same directory as /workspace
├── harness/
├── prompt.md
├── models_config.yaml
├── harness_config.yaml
├── harness_output/           # <-- OUTPUT_DIR
└── task-env/                 # <-- TASK_ENV (if exists)

/automaton/run/harness_output/
├── harness.log               # Required output
├── combined_prompt.md        # System + task prompt
├── ch-run.log                # Container output (from engine)
├── model_proxy.log           # Host-side upstream proxy activity
├── usage.jsonl               # Per-request usage + cost records
└── ch-run.sandbox.log        # Sandbox launcher stdout/stderr
```

The engine bind-mounts only the single per-run result directory into the
container, at anonymous internal paths under `/automaton/run` and `/workspace`.
The host repo path is not mounted at the same absolute location inside the
container.

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

The caller can validate harnesses via `Harness.validate()`:

```python
from automaton.harness import Harness

harness = Harness("my-harness", harness_dir="/path/to/harness")
errors = harness.validate()
# Checks: run_benchmark.sh, system_prompt.md exist
```

## See also

- [Architecture](architecture.md)
- [Container System](container-system.md)
- [Configuration](configuration.md)
- [API Reference](../api-reference/automaton.harness.md)
