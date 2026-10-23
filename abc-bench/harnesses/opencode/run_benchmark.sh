#!/usr/bin/env bash
# OpenCode harness runner for ABC Bench
# This script runs OpenCode in non-interactive mode to solve a benchmark task

set -euo pipefail

# Parse arguments
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
        --group-prompt)
            GROUP_PROMPT="$2"
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
    echo "Usage: $0 --workspace PATH --task-prompt PATH --system-prompt PATH --models-config PATH --harness-config PATH --output-dir PATH [--timeout-seconds SECONDS]" >&2
    exit 1
fi

# Create output directory
mkdir -p "${OUTPUT_DIR}"
OPENCODE_RUNTIME_DIR="$(mktemp -d /tmp/abc-bench-opencode.XXXXXX)"
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

# Parse the first exposed model written by the ABC Bench runner.
MODEL_ID=$(/opt/conda/bin/python - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
models = data.get("models", [])
print(models[0].get("model", "") if models else "")
PY
)
BASE_URL=$(/opt/conda/bin/python - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
models = data.get("models", [])
print(models[0].get("base_url", data.get("proxy_base_url", "")) if models else data.get("proxy_base_url", ""))
PY
)
API_KEY_ENV=$(/opt/conda/bin/python - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
print(data.get("api_key_env", ""))
PY
)
OPENCODE_MODEL_ID=$(python - "${MODELS_CONFIG}" <<'PY'
import sys
import yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
models = data.get("models", [])
print(models[0].get("opencode_model", "abc-bench-proxy/benchmark-model") if models else "abc-bench-proxy/benchmark-model")
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
WORKSPACE_PROMPT_FILE="${WORKSPACE}/.abc_bench_combined_prompt.md"
{
    echo "# System Instructions"
    echo ""
    cat "${SYSTEM_PROMPT}"
    echo ""
    echo "---"
    echo ""
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
cp "${COMBINED_PROMPT_FILE}" "${WORKSPACE_PROMPT_FILE}"

# Generic kickoff message. Task-type-specific expectations (which files to edit,
# how to build/verify, what the deliverable is) live in the system prompt, the
# task-group instructions, and the per-task prompt — not hard-coded here.
OPENCODE_RUN_MESSAGE="Please solve the task described in the attached prompt file. Read all files in the workspace and follow the instructions in the prompt to implement and, where applicable, build and test the required solution."

OPENCODE_CONFIG_FILE="${OUTPUT_DIR}/opencode.json"
python - "${SCRIPT_DIR}/opencode.json" "${MODELS_CONFIG}" "${OPENCODE_CONFIG_FILE}" <<'PY'
import json
import sys
import yaml

template_path, models_config_path, output_path = sys.argv[1:]
with open(template_path) as f:
    config = json.load(f)
with open(models_config_path) as f:
    models_config = yaml.safe_load(f) or {}

base_url = models_config.get("proxy_base_url") or models_config["models"][0]["base_url"]
api_key_env = models_config.get("api_key_env", "ABC_BENCH_MODEL_PROXY_API_KEY")
first_model = models_config["models"][0]
opencode_model = first_model.get("opencode_model", f"abc-bench-proxy/{first_model['name']}")

# --- Harness boundary: interpret the opaque `api` hint ---------------------
# Core carries `api` verbatim from provider/model config and assigns it no
# meaning. This harness owns the vocabulary of supported API styles and maps
# each to a concrete OpenCode provider wiring. Unknown values are rejected
# here rather than silently accepted.
#
# The `api` value is a per-model property (provider-level with model override
# resolved upstream). All models exposed through a single OpenCode provider
# must agree on the API style, since one provider block wires one npm package.
DEFAULT_API = "chat_completions"
API_NPM = {
    "chat_completions": "@ai-sdk/openai-compatible",
    "responses": "@ai-sdk/openai",
}

models = models_config.get("models", [])
api_styles = {(model.get("api") or DEFAULT_API) for model in models}
if len(api_styles) > 1:
    sys.stderr.write(
        "Error: opencode harness cannot mix API styles in one provider: "
        f"{sorted(api_styles)}\n"
    )
    sys.exit(1)
api_style = next(iter(api_styles)) if api_styles else DEFAULT_API
if api_style not in API_NPM:
    sys.stderr.write(
        f"Error: opencode harness does not support api '{api_style}'. "
        f"Supported: {sorted(API_NPM)}\n"
    )
    sys.exit(1)
provider_npm = API_NPM[api_style]
# ---------------------------------------------------------------------------

config["model"] = opencode_model
config["small_model"] = opencode_model
config["provider"] = {
    "abc-bench-proxy": {
        "npm": provider_npm,
        "name": "ABC Bench Model Proxy",
        "options": {
            "baseURL": base_url,
            "apiKey": f"{{env:{api_key_env}}}",
        },
        "models": {
            model["opencode_model"].split("/", 1)[1]: {
                "name": model["model"],
            }
            for model in models_config.get("models", [])
        },
    }
}
config["enabled_providers"] = ["abc-bench-proxy"]
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
    TASK_ENV="${WORKSPACE}/abc-bench-task-env"
fi
if [[ -d "${TASK_ENV}" ]]; then
    export PATH="${TASK_ENV}/bin:${PATH}"
fi

# Log the invocation
echo "=== OpenCode Harness Starting ===" | tee "${OUTPUT_DIR}/harness.log"
echo "Workspace: ${WORKSPACE}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Model: ${MODEL_ID}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode model: ${OPENCODE_MODEL_ID}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode config: ${OPENCODE_CONFIG}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Task env: ${TASK_ENV}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Prompt file: ${WORKSPACE_PROMPT_FILE}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode binary: /usr/local/bin/opencode" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode runtime dir: ${OPENCODE_RUNTIME_DIR}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode retained logs: ${OPENCODE_LOG_DIR}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "OpenCode HOME: ${HOME}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "XDG_CONFIG_HOME: ${XDG_CONFIG_HOME}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "XDG_CACHE_HOME: ${XDG_CACHE_HOME}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "XDG_DATA_HOME: ${XDG_DATA_HOME}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "XDG_STATE_HOME: ${XDG_STATE_HOME}" | tee -a "${OUTPUT_DIR}/harness.log"
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    echo "Timeout: ${TIMEOUT_SECONDS}s" | tee -a "${OUTPUT_DIR}/harness.log"
else
    echo "Timeout: disabled" | tee -a "${OUTPUT_DIR}/harness.log"
fi
echo "===================================" | tee -a "${OUTPUT_DIR}/harness.log"
echo "" | tee -a "${OUTPUT_DIR}/harness.log"

# Run OpenCode in non-interactive mode
# Using 'opencode run' to execute with a prompt file
set +e
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    timeout "${TIMEOUT_SECONDS}" /usr/local/bin/opencode run \
        --dir "${WORKSPACE}" \
        --model "${OPENCODE_MODEL_ID}" \
        --file "${WORKSPACE_PROMPT_FILE}" \
        --format default \
        --dangerously-skip-permissions \
        "${OPENCODE_RUN_MESSAGE}" \
        2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
else
    /usr/local/bin/opencode run \
        --dir "${WORKSPACE}" \
        --model "${OPENCODE_MODEL_ID}" \
        --file "${WORKSPACE_PROMPT_FILE}" \
        --format default \
        --dangerously-skip-permissions \
        "${OPENCODE_RUN_MESSAGE}" \
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
echo "=== OpenCode Harness Completed ===" | tee -a "${OUTPUT_DIR}/harness.log"

exit 0
