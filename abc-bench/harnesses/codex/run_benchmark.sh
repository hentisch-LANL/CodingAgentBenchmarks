#!/usr/bin/env bash
# Codex CLI harness runner for ABC Bench
#
# Runs `codex exec` non-interactively against the staged workspace. The prompt is
# fed on stdin; Codex owns the agent loop and executes tools inside the same
# runtime container. Model traffic is routed through the ABC Bench model proxy
# declared in models_config.yaml.

set -euo pipefail

WORKSPACE=""
TASK_PROMPT=""
SYSTEM_PROMPT=""
GROUP_PROMPT=""
MODELS_CONFIG=""
HARNESS_CONFIG=""
OUTPUT_DIR=""
TIMEOUT_SECONDS=""
TASK_ENV=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
    case $1 in
        --workspace) WORKSPACE="$2"; shift 2 ;;
        --task-prompt) TASK_PROMPT="$2"; shift 2 ;;
        --system-prompt) SYSTEM_PROMPT="$2"; shift 2 ;;
        --group-prompt) GROUP_PROMPT="$2"; shift 2 ;;
        --models-config) MODELS_CONFIG="$2"; shift 2 ;;
        --harness-config) HARNESS_CONFIG="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --timeout-seconds) TIMEOUT_SECONDS="$2"; shift 2 ;;
        --task-env) TASK_ENV="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

for var in WORKSPACE TASK_PROMPT SYSTEM_PROMPT MODELS_CONFIG HARNESS_CONFIG OUTPUT_DIR; do
    if [[ -z "${!var:-}" ]]; then
        echo "Usage: $0 --workspace PATH --task-prompt PATH --system-prompt PATH --models-config PATH --harness-config PATH --output-dir PATH [--group-prompt PATH] [--timeout-seconds SECONDS] [--task-env PATH]" >&2
        echo "Error: missing required argument --${var,,}" >&2
        exit 1
    fi
done

if [[ ! -f "${MODELS_CONFIG}" ]]; then
    echo "Error: Models config file not found: ${MODELS_CONFIG}" >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"
HARNESS_LOG="${OUTPUT_DIR}/harness.log"
CODEX_OUTPUT_DIR="${OUTPUT_DIR}/codex"
mkdir -p "${CODEX_OUTPUT_DIR}"

# Codex refuses to create its PATH helper aliases when CODEX_HOME lives under
# /tmp, so the per-run state root goes under /var/tmp instead. mktemp -d keeps
# concurrent attempts on one node from colliding; the container HOME
# (/var/tmp/automaton-home) is shared and chmod 0777, so agent state must not
# land there.
CODEX_RUNTIME_DIR="$(mktemp -d /var/tmp/abc-bench-codex.XXXXXX)"
trap 'rm -rf "${CODEX_RUNTIME_DIR}"' EXIT
CODEX_STATE_HOME="${CODEX_RUNTIME_DIR}/home"
export CODEX_HOME="${CODEX_RUNTIME_DIR}/codex"
mkdir -p "${CODEX_STATE_HOME}" "${CODEX_HOME}" "${CODEX_RUNTIME_DIR}/tmp"

# ---------------------------------------------------------------------------
# Resolve model + proxy wiring from models_config.yaml and harness_config.yaml
# ---------------------------------------------------------------------------
# Codex needs a custom model provider pointed at the ABC Bench proxy. A single
# `codex exec` invocation drives one model, so only the first exposed model is
# used; harness_config knobs (reasoning effort, sandbox mode) are read here too.
readarray -t CODEX_VALUES < <(python - "${MODELS_CONFIG}" "${HARNESS_CONFIG}" <<'PY'
import sys
import yaml

models_config_path, harness_config_path = sys.argv[1:3]

with open(models_config_path) as f:
    models_config = yaml.safe_load(f) or {}
try:
    with open(harness_config_path) as f:
        harness_config = yaml.safe_load(f) or {}
except FileNotFoundError:
    harness_config = {}

models = models_config.get("models") or []
if not models:
    raise SystemExit("Error: models_config.yaml exposes no models")
model = models[0]

base_url = models_config.get("proxy_base_url") or model.get("base_url")
if not base_url:
    raise SystemExit("Error: no proxy_base_url or model base_url in models_config.yaml")
api_key_env = models_config.get("api_key_env") or "ABC_BENCH_MODEL_PROXY_API_KEY"

# The proxy resolves a request to an upstream model by matching the request's
# `model` field against the config name, the upstream model id, or
# `opencode_model`. `opencode_model` is the generic proxy-qualified id despite
# its name, but Codex has no provider-prefix concept, so we send the bare
# upstream model id, which the proxy also accepts.
upstream_model = model.get("model")
if not upstream_model:
    raise SystemExit("Error: first exposed model has no `model` id")

# --- Harness boundary: interpret the opaque `api` hint ---------------------
# Core carries `api` verbatim from provider/model config and assigns it no
# meaning or default. This harness owns the vocabulary and maps each value to a
# Codex provider `wire_api`. Unknown values are rejected here rather than
# silently accepted.
#
# Codex is built around the Responses API: `chat` is supported by the CLI but
# loses reasoning-item continuity between turns, so `responses` is the default.
DEFAULT_API = "responses"
API_WIRE = {
    "responses": "responses",
    "chat_completions": "chat",
}
api_style = model.get("api") or DEFAULT_API
if api_style not in API_WIRE:
    raise SystemExit(
        f"Error: codex harness does not support api '{api_style}'. "
        f"Supported: {sorted(API_WIRE)}"
    )
wire_api = API_WIRE[api_style]

# harness_config knobs, all optional.
effort = harness_config.get("reasoning_effort") or ""
VALID_EFFORT = {"", "minimal", "low", "medium", "high", "xhigh"}
if effort not in VALID_EFFORT:
    raise SystemExit(
        f"Error: invalid reasoning_effort '{effort}'. "
        f"Supported: {sorted(VALID_EFFORT - {''})}"
    )

# Codex's own sandbox (bubblewrap + seccomp) generally cannot start nested
# inside Charliecloud's user namespace, and ABC Bench already confines the run
# via the container plus a network namespace. Default to letting Codex execute
# commands directly and treat the container as the boundary.
sandbox_mode = harness_config.get("sandbox_mode") or "danger-full-access"
VALID_SANDBOX = {"read-only", "workspace-write", "danger-full-access"}
if sandbox_mode not in VALID_SANDBOX:
    raise SystemExit(
        f"Error: invalid sandbox_mode '{sandbox_mode}'. Supported: {sorted(VALID_SANDBOX)}"
    )

context_window = harness_config.get("context_window") or ""
if context_window and not str(context_window).isdigit():
    raise SystemExit(f"Error: context_window must be an integer, got '{context_window}'")

for value in (
    upstream_model,
    base_url,
    api_key_env,
    wire_api,
    effort,
    sandbox_mode,
    context_window,
    model.get("name") or "",
):
    print(value)
PY
)

# `readarray < <(...)` runs the producer in a subshell, so a SystemExit in the
# Python block above does NOT trip `set -e`. Check the parsed output explicitly,
# or a config error would silently degrade into an empty/garbled invocation.
if [[ "${#CODEX_VALUES[@]}" -ne 8 ]]; then
    echo "Error: failed to parse model configuration (expected 8 values, got ${#CODEX_VALUES[@]})" >&2
    echo "       check ${MODELS_CONFIG} and ${HARNESS_CONFIG}" >&2
    exit 1
fi

CODEX_MODEL="${CODEX_VALUES[0]}"
PROXY_BASE_URL="${CODEX_VALUES[1]}"
API_KEY_ENV="${CODEX_VALUES[2]}"
WIRE_API="${CODEX_VALUES[3]}"
REASONING_EFFORT="${CODEX_VALUES[4]}"
SANDBOX_MODE="${CODEX_VALUES[5]}"
CONTEXT_WINDOW="${CODEX_VALUES[6]}"
MODEL_NAME="${CODEX_VALUES[7]}"

if [[ -z "${API_KEY_ENV}" || "${API_KEY_ENV}" == "None" ]]; then
    echo "Error: models_config.yaml did not name an api_key_env" >&2
    exit 1
fi
if [[ -z "${!API_KEY_ENV:-}" ]]; then
    echo "Error: API key environment variable ${API_KEY_ENV} is not set" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Assemble the prompt: system -> task-group -> task
# ---------------------------------------------------------------------------
COMBINED_PROMPT_FILE="${OUTPUT_DIR}/combined_prompt.md"
{
    echo "# System Instructions"
    echo ""
    cat "${SYSTEM_PROMPT}"
    echo ""
    echo "---"
    echo ""
    # --group-prompt is only passed when the task group ships group_prompt.md.
    if [[ -n "${GROUP_PROMPT}" && -f "${GROUP_PROMPT}" ]]; then
        echo "# Task Group Instructions"
        echo ""
        cat "${GROUP_PROMPT}"
        echo ""
        echo "---"
        echo ""
    fi
    echo "# Task"
    echo ""
    cat "${TASK_PROMPT}"
} > "${COMBINED_PROMPT_FILE}"

cd "${WORKSPACE}"

if [[ -z "${TASK_ENV}" ]]; then
    TASK_ENV="${WORKSPACE}/abc-bench-task-env"
fi
if [[ -d "${TASK_ENV}" ]]; then
    export PATH="${TASK_ENV}/bin:${PATH}"
fi

export HOME="${CODEX_STATE_HOME}"
export TMPDIR="${CODEX_RUNTIME_DIR}/tmp"

# The container image installs codex at a known absolute path; fall back to PATH
# lookup so the script also works against a locally provided binary.
if [[ -x /usr/local/bin/codex ]]; then
    CODEX_BIN=/usr/local/bin/codex
elif CODEX_BIN="$(command -v codex 2>/dev/null)"; then
    :
else
    echo "Error: codex binary not found at /usr/local/bin/codex or on PATH" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Build the Codex invocation
# ---------------------------------------------------------------------------
# Global flags must precede the `exec` subcommand.
CODEX_GLOBAL_ARGS=(
    --ask-for-approval never
    # Point Codex at the ABC Bench proxy as a custom provider. env_key names
    # the variable Codex reads the bearer token from; the token itself never
    # appears in argv or in run metadata.
    -c 'model_provider="abc-bench-proxy"'
    -c 'model_providers.abc-bench-proxy.name="ABC Bench Model Proxy"'
    -c "model_providers.abc-bench-proxy.base_url=\"${PROXY_BASE_URL}\""
    -c "model_providers.abc-bench-proxy.env_key=\"${API_KEY_ENV}\""
    -c "model_providers.abc-bench-proxy.wire_api=\"${WIRE_API}\""
    # Codex's own request retries are deliberately left at their default.
    #
    # The model proxy retries only *transport* failures (a dropped or refused
    # connection); an upstream HTTP error such as a 503 "Overloaded" is
    # forwarded to the client verbatim by design. The CLI is therefore the only
    # thing that can ride out a transient 5xx. These were previously pinned to
    # 0 on the incorrect assumption that the proxy covered that case, which
    # turned one transient 503 into a dead run. The two layers cover disjoint
    # failure classes, so they do not compound.
    #
    # An endpoint that genuinely will not answer still ends the run as a harness
    # failure; nothing here turns a dead endpoint into a graded result.
    # Benchmarks must be solved from the workspace, not from the web.
    #
    # `web_search` is an enum (disabled|cached|indexed|live), not a boolean, and
    # "disabled" is the current, first-class way to turn the tool off. Note that
    # the similarly-named `tools.web_search=false` is a DIFFERENT key that
    # parses, validates under --strict-config, emits no warning, and has no
    # effect on codex-cli 0.155.1 -- do not "helpfully" add it back.
    #
    # This matters more than an ordinary tool toggle: Codex declares web_search
    # as a native provider tool (`{"type":"web_search"}`, not `"function"`), so
    # the model's own infrastructure performs the fetch inside the single HTTP
    # response. Nothing egresses from the container, so neither the sandbox
    # network namespace nor the model proxy can observe or block it. Disabling
    # it here is the control. automaton's proxy independently rejects
    # provider-executed tools unless explicitly allowed, as defense in depth.
    -c 'web_search="disabled"'
)
if [[ -n "${REASONING_EFFORT}" ]]; then
    CODEX_GLOBAL_ARGS+=(-c "model_reasoning_effort=\"${REASONING_EFFORT}\"")
fi
if [[ -n "${CONTEXT_WINDOW}" ]]; then
    CODEX_GLOBAL_ARGS+=(-c "model_context_window=${CONTEXT_WINDOW}")
fi

CODEX_EXEC_ARGS=(
    exec
    --sandbox "${SANDBOX_MODE}"
    --cd "${WORKSPACE}"
    # Task workspaces are not necessarily Git repositories.
    --skip-git-repo-check
    --model "${CODEX_MODEL}"
    # Do not persist session rollout files between attempts.
    --ephemeral
    # Ignore any config.toml a previous run or a task left in CODEX_HOME.
    --ignore-user-config
    --json
    --color never
    --output-last-message "${CODEX_OUTPUT_DIR}/final_message.txt"
    -
)

{
    echo "=== Codex Harness Starting ==="
    echo "Workspace: ${WORKSPACE}"
    echo "Model (upstream id): ${CODEX_MODEL}"
    echo "Model (config name): ${MODEL_NAME}"
    echo "Proxy base URL: ${PROXY_BASE_URL}"
    echo "Wire API: ${WIRE_API}"
    echo "API key env: ${API_KEY_ENV}"
    echo "Reasoning effort: ${REASONING_EFFORT:-<model default>}"
    echo "Context window: ${CONTEXT_WINDOW:-<model default>}"
    echo "Codex sandbox mode: ${SANDBOX_MODE}"
    echo "Codex binary: ${CODEX_BIN}"
    echo "Codex version: $(${CODEX_BIN} --version 2>&1)"
    echo "CODEX_HOME: ${CODEX_HOME}"
    echo "HOME: ${HOME}"
    echo "TMPDIR: ${TMPDIR}"
    echo "Task env: ${TASK_ENV}"
    echo "Prompt file: ${COMBINED_PROMPT_FILE}"
    if [[ -n "${TIMEOUT_SECONDS}" ]]; then
        echo "Timeout: ${TIMEOUT_SECONDS}s"
    else
        echo "Timeout: disabled"
    fi
    echo "==============================="
    echo ""
} | tee "${HARNESS_LOG}"

EVENTS_FILE="${CODEX_OUTPUT_DIR}/events.jsonl"
EVENTS_SUMMARY="${CODEX_OUTPUT_DIR}/events_summary.json"
CODEX_STDERR="${CODEX_OUTPUT_DIR}/stderr.log"

# Codex's JSONL stream goes to the renderer, which preserves it verbatim and
# emits a readable transcript. stderr is kept separate so CLI diagnostics are
# not interleaved into the event stream.
set +e
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    timeout "${TIMEOUT_SECONDS}" "${CODEX_BIN}" \
        "${CODEX_GLOBAL_ARGS[@]}" "${CODEX_EXEC_ARGS[@]}" \
        < "${COMBINED_PROMPT_FILE}" \
        2> >(tee "${CODEX_STDERR}" >&2) \
        | python "${SCRIPT_DIR}/lib/render_events.py" "${EVENTS_FILE}" "${EVENTS_SUMMARY}" \
        | tee -a "${HARNESS_LOG}"
else
    "${CODEX_BIN}" \
        "${CODEX_GLOBAL_ARGS[@]}" "${CODEX_EXEC_ARGS[@]}" \
        < "${COMBINED_PROMPT_FILE}" \
        2> >(tee "${CODEX_STDERR}" >&2) \
        | python "${SCRIPT_DIR}/lib/render_events.py" "${EVENTS_FILE}" "${EVENTS_SUMMARY}" \
        | tee -a "${HARNESS_LOG}"
fi
# PIPESTATUS[0] is codex/timeout; $? would be tee's status, which is ~always 0.
EXIT_CODE="${PIPESTATUS[0]}"
set -e

# ---------------------------------------------------------------------------
# Report outcome
# ---------------------------------------------------------------------------
TERMINAL_EVENT=""
if [[ -f "${EVENTS_SUMMARY}" ]]; then
    TERMINAL_EVENT="$(python - "${EVENTS_SUMMARY}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1]) as f:
        print((json.load(f) or {}).get("last_terminal_event") or "")
except Exception:
    print("")
PY
)"
fi

{
    echo ""
    echo "Codex exit code: ${EXIT_CODE}"
    echo "Terminal event: ${TERMINAL_EVENT:-<none>}"
    echo "Events: ${EVENTS_FILE}"
    echo "Event summary: ${EVENTS_SUMMARY}"
    echo "Codex stderr: ${CODEX_STDERR}"
} | tee -a "${HARNESS_LOG}"

if [[ ${EXIT_CODE} -eq 124 ]]; then
    {
        echo ""
        echo "=== TIMEOUT ==="
        echo "Codex execution timed out after ${TIMEOUT_SECONDS} seconds"
    } | tee -a "${HARNESS_LOG}"
    exit 124
elif [[ ${EXIT_CODE} -ne 0 ]]; then
    {
        echo ""
        echo "=== ERROR ==="
        echo "Codex execution failed with exit code ${EXIT_CODE}"
        if [[ -s "${CODEX_STDERR}" ]]; then
            echo "--- codex stderr (tail) ---"
            tail -n 40 "${CODEX_STDERR}"
        fi
    } | tee -a "${HARNESS_LOG}"
    exit "${EXIT_CODE}"
fi

# A zero exit with a failed turn means the agent loop ended badly even though
# the process itself succeeded. Surface it as a harness failure rather than
# letting it be silently graded as a wrong answer.
if [[ "${TERMINAL_EVENT}" == "turn.failed" ]]; then
    {
        echo ""
        echo "=== ERROR ==="
        echo "Codex reported a failed turn (turn.failed)"
    } | tee -a "${HARNESS_LOG}"
    exit 1
fi

{
    echo ""
    echo "=== Codex Harness Completed ==="
} | tee -a "${HARNESS_LOG}"

exit 0
