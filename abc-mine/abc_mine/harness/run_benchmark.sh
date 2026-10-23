#!/usr/bin/env bash
# OpenCode harness runner for the abc-mine benchmark-mining workflow.
#
# Adapted from abc-bench/harnesses/opencode/run_benchmark.sh. Same CLI:
#   --workspace --task-prompt --system-prompt --models-config --harness-config
#   --output-dir --timeout-seconds --task-env
#
# Differences from the abc-bench harness:
#   - The generated opencode.json permission block allows `bash` and `task`
#     (subagents), so the agent can build/test the codebase and spawn research
#     subagents. This is baked into the committed opencode.json template; this
#     script preserves the template's permission block verbatim.
#   - Proxy provider defaults are automaton's (`automaton-proxy` /
#     `AUTOMATON_MODEL_PROXY_API_KEY`), since abc-mine uses the default
#     Sandbox proxy settings rather than abc-bench's overrides.

set -euo pipefail

# Parse arguments
WORKSPACE=""
TASK_PROMPT=""
SYSTEM_PROMPT=""
MODELS_CONFIG=""
HARNESS_CONFIG=""
OUTPUT_DIR=""
TIMEOUT_SECONDS=""
TASK_ENV=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
    case $1 in
        --workspace)
            WORKSPACE="$2"
            shift 2
            ;;
        --task-prompt)
            TASK_PROMPT="$2"
            shift 2
            ;;
        --system-prompt)
            SYSTEM_PROMPT="$2"
            shift 2
            ;;
        --models-config)
            MODELS_CONFIG="$2"
            shift 2
            ;;
        --harness-config)
            HARNESS_CONFIG="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --timeout-seconds)
            TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        --task-env)
            TASK_ENV="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "${WORKSPACE}" || -z "${TASK_PROMPT}" || -z "${SYSTEM_PROMPT}" || -z "${MODELS_CONFIG}" || -z "${HARNESS_CONFIG}" || -z "${OUTPUT_DIR}" ]]; then
    echo "Usage: $0 --workspace PATH --task-prompt PATH --system-prompt PATH --models-config PATH --harness-config PATH --output-dir PATH [--timeout-seconds SECONDS] [--task-env PATH]" >&2
    exit 1
fi

# Create output directory
mkdir -p "${OUTPUT_DIR}"

# Resolve a Python interpreter for the small config-parsing helpers below.
# The container is expected to ship system python3 (no conda); fall back to conda or
# `python` for portability across images.
if command -v python3 >/dev/null 2>&1; then
    PY_BIN="$(command -v python3)"
elif [[ -x /opt/conda/bin/python ]]; then
    PY_BIN="/opt/conda/bin/python"
elif command -v python >/dev/null 2>&1; then
    PY_BIN="$(command -v python)"
else
    echo "No python interpreter found (need python3, /opt/conda/bin/python, or python)" >&2
    exit 1
fi
OPENCODE_RUNTIME_DIR="$(mktemp -d /tmp/lbm-opencode.XXXXXX)"
OPENCODE_OUTPUT_DIR="${OUTPUT_DIR}/opencode"
OPENCODE_LOG_DIR="${OPENCODE_OUTPUT_DIR}/logs"
mkdir -p "${OPENCODE_LOG_DIR}"
trap 'rm -rf "${OPENCODE_RUNTIME_DIR}"' EXIT
OPENCODE_RUNTIME_OUTPUT_DIR="${OPENCODE_RUNTIME_DIR}/opencode"
OPENCODE_HOME="${OPENCODE_RUNTIME_OUTPUT_DIR}/home"
mkdir -p \
    "${OPENCODE_HOME}" \
    "${OPENCODE_RUNTIME_OUTPUT_DIR}/config" \
    "${OPENCODE_RUNTIME_OUTPUT_DIR}/cache" \
    "${OPENCODE_RUNTIME_OUTPUT_DIR}/data" \
    "${OPENCODE_RUNTIME_OUTPUT_DIR}/state" \
    "${OPENCODE_RUNTIME_OUTPUT_DIR}/tmp"

# Load model configuration
if [[ ! -f "${MODELS_CONFIG}" ]]; then
    echo "Error: Models config file not found: ${MODELS_CONFIG}" >&2
    exit 1
fi

# Parse the first exposed model written by the mining runner.
MODEL_ID=$("${PY_BIN}" - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
models = data.get("models", [])
print(models[0].get("model", "") if models else "")
PY
)
BASE_URL=$("${PY_BIN}" - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
models = data.get("models", [])
print(models[0].get("base_url", data.get("proxy_base_url", "")) if models else data.get("proxy_base_url", ""))
PY
)
API_KEY_ENV=$("${PY_BIN}" - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
print(data.get("api_key_env", ""))
PY
)
OPENCODE_MODEL_ID=$("${PY_BIN}" - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
models = data.get("models", [])
print(models[0].get("opencode_model", "automaton-proxy/benchmark-model") if models else "automaton-proxy/benchmark-model")
PY
)

if [[ -z "${MODEL_ID}" ]]; then
    echo "Error: Could not parse model from config" >&2
    exit 1
fi

# Set up API key from environment if specified
if [[ -n "${API_KEY_ENV}" && "${API_KEY_ENV}" != "None" ]]; then
    API_KEY_VALUE="${!API_KEY_ENV:-}"
    if [[ -z "${API_KEY_VALUE}" ]]; then
        echo "Error: API key environment variable ${API_KEY_ENV} is not set" >&2
        exit 1
    fi
fi

# OpenCode reads provider credentials from provider-specific environment
# variables. Export common OpenAI-compatible names for harness configs that
# use an api_key_env indirection.
PROVIDER="${MODEL_ID%%/*}"
if [[ -n "${API_KEY_ENV}" && "${API_KEY_ENV}" != "None" ]]; then
    case "${PROVIDER}" in
        openai)
            export OPENAI_API_KEY="${API_KEY_VALUE}"
            if [[ -n "${BASE_URL}" && "${BASE_URL}" != "None" ]]; then
                export OPENAI_BASE_URL="${BASE_URL}"
            fi
            ;;
        anthropic)
            export ANTHROPIC_API_KEY="${API_KEY_VALUE}"
            ;;
        *)
            export "${API_KEY_ENV}"="${API_KEY_VALUE}"
            ;;
    esac
fi

# Combine prompts
COMBINED_PROMPT_FILE="${OUTPUT_DIR}/combined_prompt.md"
WORKSPACE_PROMPT_FILE="${WORKSPACE}/.lbm_combined_prompt.md"
{
    echo "# System Instructions"
    echo ""
    cat "${SYSTEM_PROMPT}"
    echo ""
    echo "---"
    echo ""
    echo "# Task"
    echo ""
    cat "${TASK_PROMPT}"
} > "${COMBINED_PROMPT_FILE}"
cp "${COMBINED_PROMPT_FILE}" "${WORKSPACE_PROMPT_FILE}"

OPENCODE_CONFIG_FILE="${OUTPUT_DIR}/opencode.json"
"${PY_BIN}" - "${SCRIPT_DIR}/opencode.json" "${MODELS_CONFIG}" "${OPENCODE_CONFIG_FILE}" <<'PY'
import json
import sys
import yaml

template_path, models_config_path, output_path = sys.argv[1:]
with open(template_path) as f:
    config = json.load(f)
with open(models_config_path) as f:
    models_config = yaml.safe_load(f) or {}

base_url = models_config.get("proxy_base_url") or models_config["models"][0]["base_url"]
api_key_env = models_config.get("api_key_env", "AUTOMATON_MODEL_PROXY_API_KEY")
first_model = models_config["models"][0]
opencode_model = first_model.get("opencode_model", f"automaton-proxy/{first_model['name']}")
proxy_provider = opencode_model.split("/", 1)[0]

# --- Harness boundary: interpret the opaque `api` hint ---------------------
# Core carries `api` verbatim from provider/model config and assigns it no
# meaning. This harness owns the vocabulary of supported API styles and maps
# each to a concrete OpenCode provider wiring. Unknown values are rejected
# here rather than silently accepted.
DEFAULT_API = "chat_completions"
API_NPM = {
    "chat_completions": "@ai-sdk/openai-compatible",
    "responses": "@ai-sdk/openai",
    # Selects the Anthropic Messages protocol. OpenCode's prompt-caching path
    # keys off this exact package name, so an Anthropic-backed endpoint reached
    # over chat_completions silently forgoes cache reads.
    "anthropic_messages": "@ai-sdk/anthropic",
}

models = models_config.get("models", [])
api_styles = {(model.get("api") or DEFAULT_API) for model in models}
if len(api_styles) > 1:
    sys.stderr.write(
        "Error: abc-mine harness cannot mix API styles in one provider: "
        f"{sorted(api_styles)}\n"
    )
    sys.exit(1)
api_style = next(iter(api_styles)) if api_styles else DEFAULT_API
if api_style not in API_NPM:
    sys.stderr.write(
        f"Error: abc-mine harness does not support api '{api_style}'. "
        f"Supported: {sorted(API_NPM)}\n"
    )
    sys.exit(1)
provider_npm = API_NPM[api_style]
# ---------------------------------------------------------------------------

config["model"] = opencode_model
config["small_model"] = opencode_model
config["provider"] = {
    proxy_provider: {
        "npm": provider_npm,
        "name": "Automaton Model Proxy",
        "options": {
            "baseURL": base_url,
            "apiKey": f"{{env:{api_key_env}}}",
        },
        "models": {
            model["opencode_model"].split("/", 1)[1]: {
                "name": model["model"],
            }
            for model in models
        },
    }
}
config["enabled_providers"] = [proxy_provider]
config["disabled_providers"] = [
    "anthropic",
    "openai",
    "opencode",
    "openrouter",
]
config["share"] = "disabled"
config["autoupdate"] = False

with open(output_path, "w") as f:
    json.dump(config, f, indent=2)
PY
export OPENCODE_CONFIG="${OPENCODE_CONFIG_FILE}"
export HOME="${OPENCODE_HOME}"
export XDG_CONFIG_HOME="${OPENCODE_RUNTIME_OUTPUT_DIR}/config"
export XDG_CACHE_HOME="${OPENCODE_RUNTIME_OUTPUT_DIR}/cache"
export XDG_DATA_HOME="${OPENCODE_RUNTIME_OUTPUT_DIR}/data"
export XDG_STATE_HOME="${OPENCODE_RUNTIME_OUTPUT_DIR}/state"
export TMPDIR="${OPENCODE_RUNTIME_OUTPUT_DIR}/tmp"

# Change to workspace directory
cd "${WORKSPACE}"

if [[ -z "${TASK_ENV}" ]]; then
    TASK_ENV="${WORKSPACE}/lbm-task-env"
fi
if [[ -d "${TASK_ENV}" ]]; then
    export PATH="${TASK_ENV}/bin:${PATH}"
fi

# Log the invocation
echo "=== abc-mine OpenCode Harness Starting ===" | tee "${OUTPUT_DIR}/harness.log"
echo "Workspace: ${WORKSPACE}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Model: ${MODEL_ID}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode model: ${OPENCODE_MODEL_ID}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode config: ${OPENCODE_CONFIG}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Task env: ${TASK_ENV}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Prompt file (stdin): ${WORKSPACE_PROMPT_FILE}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode binary: /usr/local/bin/opencode" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode runtime dir: ${OPENCODE_RUNTIME_DIR}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode retained logs: ${OPENCODE_LOG_DIR}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode HOME: ${HOME}" | tee -a "${OUTPUT_DIR}/harness.log"
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    echo "Timeout: ${TIMEOUT_SECONDS}s" | tee -a "${OUTPUT_DIR}/harness.log"
else
    echo "Timeout: disabled" | tee -a "${OUTPUT_DIR}/harness.log"
fi
echo "===================================" | tee -a "${OUTPUT_DIR}/harness.log"
echo "" | tee -a "${OUTPUT_DIR}/harness.log"

# Run OpenCode in non-interactive mode.
#
# The prompt goes in on STDIN, not via --file. OpenCode turns an attached file
# into a synthetic Read-tool call, and the Read tool returns at most 2000 lines
# or 50 KB: a longer prompt reaches the model silently truncated, with a note
# telling the agent to page through the rest itself. Piped stdin is appended to
# the message verbatim, whatever its size.
#
# --title is set explicitly because, without one, OpenCode generates a session
# title by sending the entire first message to the model a second time. For a
# large prompt (ranking can approach the context window) that doubles the cost
# of the opening turn for nothing.
RUN_MESSAGE="Follow the instructions below exactly. Read the workspace, do the requested work, and emit outputs where the instructions say."
SESSION_TITLE="abc-mine"
set +e
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    timeout "${TIMEOUT_SECONDS}" /usr/local/bin/opencode run \
        --dir "${WORKSPACE}" \
        --model "${OPENCODE_MODEL_ID}" \
        --title "${SESSION_TITLE}" \
        --format default \
        --dangerously-skip-permissions \
        "${RUN_MESSAGE}" \
        < "${WORKSPACE_PROMPT_FILE}" \
        2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
else
    /usr/local/bin/opencode run \
        --dir "${WORKSPACE}" \
        --model "${OPENCODE_MODEL_ID}" \
        --title "${SESSION_TITLE}" \
        --format default \
        --dangerously-skip-permissions \
        "${RUN_MESSAGE}" \
        < "${WORKSPACE_PROMPT_FILE}" \
        2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
fi

EXIT_CODE=$?
set -e

{
    echo "# OpenCode output files"
    echo ""
    echo "Runtime dir: ${OPENCODE_RUNTIME_DIR}"
    echo "Retained log dir: ${OPENCODE_LOG_DIR}"
    echo ""
    if [[ -d "${OPENCODE_RUNTIME_OUTPUT_DIR}" ]]; then
        while IFS= read -r -d '' log_file; do
            relative_path="${log_file#${OPENCODE_RUNTIME_OUTPUT_DIR}/}"
            mkdir -p "${OPENCODE_LOG_DIR}/$(dirname "${relative_path}")"
            cp "${log_file}" "${OPENCODE_LOG_DIR}/${relative_path}"
            echo "${OPENCODE_LOG_DIR}/${relative_path}"
        done < <(find "${OPENCODE_RUNTIME_OUTPUT_DIR}" -type f \( -iname '*.log' -o -iname '*log*' \) -print0 | sort -z)
    fi
} > "${OPENCODE_OUTPUT_DIR}/files.txt"

# Check exit status
if [[ ${EXIT_CODE} -eq 124 ]]; then
    echo "" | tee -a "${OUTPUT_DIR}/harness.log"
    echo "=== TIMEOUT ===" | tee -a "${OUTPUT_DIR}/harness.log"
    echo "OpenCode execution timed out after ${TIMEOUT_SECONDS} seconds" | tee -a "${OUTPUT_DIR}/harness.log"
    exit 124
elif [[ ${EXIT_CODE} -ne 0 ]]; then
    echo "" | tee -a "${OUTPUT_DIR}/harness.log"
    echo "=== ERROR ===" | tee -a "${OUTPUT_DIR}/harness.log"
    echo "OpenCode execution failed with exit code ${EXIT_CODE}" | tee -a "${OUTPUT_DIR}/harness.log"
    exit ${EXIT_CODE}
fi

echo "" | tee -a "${OUTPUT_DIR}/harness.log"
echo "=== abc-mine OpenCode Harness Completed ===" | tee -a "${OUTPUT_DIR}/harness.log"

exit 0
