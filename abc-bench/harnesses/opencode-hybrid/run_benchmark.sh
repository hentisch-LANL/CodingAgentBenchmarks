#!/usr/bin/env bash
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
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

for var in WORKSPACE TASK_PROMPT SYSTEM_PROMPT MODELS_CONFIG HARNESS_CONFIG OUTPUT_DIR; do
    [[ -n "${!var:-}" ]] || { echo "Missing required argument: ${var}" >&2; exit 1; }
done

# Emit the optional shared task-group instructions section (a no-op when the
# group defines no group_prompt.md). Both the planner and builder prompts get
# it, between the system/planner instructions and the per-task prompt.
emit_group_prompt() {
    if [[ -n "${GROUP_PROMPT}" && -f "${GROUP_PROMPT}" ]]; then
        echo "# Task Group Instructions"
        echo
        cat "${GROUP_PROMPT}"
        echo
        echo "---"
        echo
    fi
}

mkdir -p "${OUTPUT_DIR}"
PLAN_FILE="${WORKSPACE}/PLAN.md"
PLAN_PROMPT_FILE="${OUTPUT_DIR}/planner_prompt.md"
BUILD_PROMPT_FILE="${OUTPUT_DIR}/builder_prompt.md"
OPENCODE_CONFIG_FILE="${OUTPUT_DIR}/opencode.json"

readarray -t MODEL_VALUES < <(/opt/conda/bin/python - "${MODELS_CONFIG}" "${HARNESS_CONFIG}" <<'PY'
import json
import sys
import yaml

with open(sys.argv[1]) as f:
    models_config = yaml.safe_load(f) or {}
with open(sys.argv[2]) as f:
    harness_config = yaml.safe_load(f) or {}

models = {model["name"]: model for model in models_config.get("models", [])}
plan_name = harness_config.get("plan_model")
build_name = harness_config.get("build_model")
if plan_name not in models or build_name not in models:
    raise SystemExit("plan_model/build_model must reference exposed models")

first = models_config.get("models", [])[0]
print(models_config.get("proxy_base_url") or first.get("base_url", ""))
print(models_config.get("api_key_env", "ABC_BENCH_MODEL_PROXY_API_KEY"))
print(models[plan_name]["model"])
print(models[plan_name]["opencode_model"])
print(models[build_name]["model"])
print(models[build_name]["opencode_model"])
print(json.dumps(models_config.get("models", [])))
PY
)

BASE_URL="${MODEL_VALUES[0]}"
API_KEY_ENV="${MODEL_VALUES[1]}"
PLAN_MODEL_ID="${MODEL_VALUES[2]}"
PLAN_OPENCODE_MODEL="${MODEL_VALUES[3]}"
BUILD_MODEL_ID="${MODEL_VALUES[4]}"
BUILD_OPENCODE_MODEL="${MODEL_VALUES[5]}"
ALL_MODELS_JSON="${MODEL_VALUES[6]}"

if [[ -n "${API_KEY_ENV}" && "${API_KEY_ENV}" != "None" ]]; then
    API_KEY_VALUE="${!API_KEY_ENV:-}"
    [[ -n "${API_KEY_VALUE}" ]] || { echo "Missing API key env ${API_KEY_ENV}" >&2; exit 1; }
    export OPENAI_API_KEY="${API_KEY_VALUE}"
    export OPENAI_BASE_URL="${BASE_URL}"
fi

python - "${SCRIPT_DIR}/opencode.json" "${ALL_MODELS_JSON}" "${BASE_URL}" "${API_KEY_ENV}" "${PLAN_OPENCODE_MODEL}" "${BUILD_OPENCODE_MODEL}" "${OPENCODE_CONFIG_FILE}" "${OUTPUT_DIR}/resolved_models.env" <<'PY'
import json
import sys

(
    template_path,
    models_json,
    base_url,
    api_key_env,
    plan_model_in,
    build_model_in,
    output_path,
    resolved_env_path,
) = sys.argv[1:]
with open(template_path) as f:
    config = json.load(f)
models = json.loads(models_json)

# --- Harness boundary: interpret the opaque `api` hint ---------------------
# Core carries `api` verbatim from provider/model config and assigns it no
# meaning. This harness owns the vocabulary of supported API styles and maps
# each to a concrete OpenCode provider wiring. Unknown values are rejected here.
#
# Unlike the single-model opencode harness, the hybrid harness exposes multiple
# models (planner + builder) that may legitimately use different API styles.
# One OpenCode provider block wires exactly one npm package, so we emit one
# provider block per distinct API style and route each model to the provider
# matching its own style. OpenCode model IDs are namespaced by provider id
# (e.g. "abc-bench-proxy/<model>"), so per-style providers require per-style
# provider ids.
#
# Because model routing can move onto a style-specific provider id, this block
# is the single source of truth for the resolved planner/builder --model ids
# and writes them back to the shell via resolved_env_path.
DEFAULT_API = "chat_completions"
API_NPM = {
    "chat_completions": "@ai-sdk/openai-compatible",
    "responses": "@ai-sdk/openai",
}
BASE_PROVIDER_ID = "abc-bench-proxy"


def provider_id_for(api_style):
    # Keep the historical id for the default style so existing model routing
    # remains valid and unchanged for the common single-style case.
    if api_style == DEFAULT_API:
        return BASE_PROVIDER_ID
    return f"{BASE_PROVIDER_ID}-{api_style.replace('_', '-')}"


for model in models:
    api_style = model.get("api") or DEFAULT_API
    if api_style not in API_NPM:
        sys.stderr.write(
            f"Error: opencode-hybrid harness does not support api '{api_style}' "
            f"for model '{model.get('name')}'. Supported: {sorted(API_NPM)}\n"
        )
        sys.exit(1)
    model["_api_style"] = api_style
    model["_provider_id"] = provider_id_for(api_style)
    bare = model["opencode_model"].split("/", 1)[1]
    model["_bare_model"] = bare
    # Re-route the opencode model id onto its style-specific provider so the
    # planner/builder --model references resolve to the correct provider block.
    model["opencode_model"] = f"{model['_provider_id']}/{bare}"


def resolve(model_in):
    bare = model_in.split("/", 1)[1]
    for model in models:
        if model["_bare_model"] == bare:
            return model["opencode_model"]
    return model_in


plan_model = resolve(plan_model_in)
build_model = resolve(build_model_in)

providers = {}
for model in models:
    provider_id = model["_provider_id"]
    if provider_id not in providers:
        providers[provider_id] = {
            "npm": API_NPM[model["_api_style"]],
            "name": "ABC Bench Model Proxy",
            "options": {
                "baseURL": base_url,
                "apiKey": f"{{env:{api_key_env}}}",
            },
            "models": {},
        }
    providers[provider_id]["models"][model["_bare_model"]] = {
        "name": model["model"],
    }
# ---------------------------------------------------------------------------

config["model"] = plan_model
config["small_model"] = plan_model
config["provider"] = providers
config["enabled_providers"] = sorted(providers.keys())
config["disabled_providers"] = ["anthropic", "openai", "opencode", "openrouter"]
config["share"] = "disabled"
config["autoupdate"] = False

with open(output_path, "w") as f:
    json.dump(config, f, indent=2)

with open(resolved_env_path, "w") as f:
    f.write(f"PLAN_OPENCODE_MODEL={plan_model}\n")
    f.write(f"BUILD_OPENCODE_MODEL={build_model}\n")
PY

# The builder is the source of truth for the resolved planner/builder model
# ids, since API-style routing can move a model onto a style-specific provider.
# shellcheck disable=SC1090
source "${OUTPUT_DIR}/resolved_models.env"

export OPENCODE_CONFIG="${OPENCODE_CONFIG_FILE}"
RUNTIME_DIR="$(mktemp -d /tmp/abc-bench-opencode-hybrid.XXXXXX)"
trap 'rm -rf "${RUNTIME_DIR}"' EXIT
export HOME="${RUNTIME_DIR}/home"
export XDG_CONFIG_HOME="${RUNTIME_DIR}/config"
export XDG_CACHE_HOME="${RUNTIME_DIR}/cache"
export XDG_DATA_HOME="${RUNTIME_DIR}/data"
export XDG_STATE_HOME="${RUNTIME_DIR}/state"
export TMPDIR="${RUNTIME_DIR}/tmp"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$TMPDIR"

cd "${WORKSPACE}"
if [[ -z "${TASK_ENV}" ]]; then
    TASK_ENV="${WORKSPACE}/abc-bench-task-env"
fi
if [[ -d "${TASK_ENV}" ]]; then
    export PATH="${TASK_ENV}/bin:${PATH}"
fi

{
    echo "# Planner Instructions"
    echo
    cat "${SCRIPT_DIR}/planner_prompt.md"
    echo
    echo "---"
    echo
    emit_group_prompt
    echo "# Task"
    echo
    cat "${TASK_PROMPT}"
} > "${PLAN_PROMPT_FILE}"

{
    echo "# System Instructions"
    echo
    cat "${SYSTEM_PROMPT}"
    echo
    echo "---"
    echo
    emit_group_prompt
    echo "# Task"
    echo
    cat "${TASK_PROMPT}"
    echo
    echo "---"
    echo
    echo "# Plan"
    echo
    if [[ -f "${PLAN_FILE}" ]]; then
        cat "${PLAN_FILE}"
    else
        echo "No plan file found."
    fi
} > "${BUILD_PROMPT_FILE}"

echo "=== OpenCode Hybrid Harness Starting ===" | tee "${OUTPUT_DIR}/harness.log"
echo "Plan model: ${PLAN_MODEL_ID}" | tee -a "${OUTPUT_DIR}/harness.log"
echo "Build model: ${BUILD_MODEL_ID}" | tee -a "${OUTPUT_DIR}/harness.log"

set +e
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    timeout "${TIMEOUT_SECONDS}" /usr/local/bin/opencode run \
        --dir "${WORKSPACE}" \
        --model "${PLAN_OPENCODE_MODEL}" \
        --file "${PLAN_PROMPT_FILE}" \
        --format default \
        --dangerously-skip-permissions \
        "Write a detailed implementation plan to PLAN.md and do not modify other files." \
        2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
    EXIT_CODE=${PIPESTATUS[0]}
else
    /usr/local/bin/opencode run \
        --dir "${WORKSPACE}" \
        --model "${PLAN_OPENCODE_MODEL}" \
        --file "${PLAN_PROMPT_FILE}" \
        --format default \
        --dangerously-skip-permissions \
        "Write a detailed implementation plan to PLAN.md and do not modify other files." \
        2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
    EXIT_CODE=${PIPESTATUS[0]}
fi

if [[ ${EXIT_CODE} -eq 0 ]]; then
    {
        echo "# System Instructions"
        echo
        cat "${SYSTEM_PROMPT}"
        echo
        echo "---"
        echo
        emit_group_prompt
        echo "# Task"
        echo
        cat "${TASK_PROMPT}"
        echo
        echo "---"
        echo
        echo "# Plan"
        echo
        if [[ -f "${PLAN_FILE}" ]]; then
            cat "${PLAN_FILE}"
        else
            echo "No plan file found."
        fi
    } > "${BUILD_PROMPT_FILE}"

    if [[ -n "${TIMEOUT_SECONDS}" ]]; then
        timeout "${TIMEOUT_SECONDS}" /usr/local/bin/opencode run \
            --dir "${WORKSPACE}" \
            --model "${BUILD_OPENCODE_MODEL}" \
            --file "${BUILD_PROMPT_FILE}" \
            --format default \
            --dangerously-skip-permissions \
            "Please solve the task using the attached plan file and leave the completed solution in the workspace." \
            2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
        EXIT_CODE=${PIPESTATUS[0]}
    else
        /usr/local/bin/opencode run \
            --dir "${WORKSPACE}" \
            --model "${BUILD_OPENCODE_MODEL}" \
            --file "${BUILD_PROMPT_FILE}" \
            --format default \
            --dangerously-skip-permissions \
            "Please solve the task using the attached plan file and leave the completed solution in the workspace." \
            2>&1 | tee -a "${OUTPUT_DIR}/harness.log"
        EXIT_CODE=${PIPESTATUS[0]}
    fi
fi
set -e

if [[ ${EXIT_CODE} -eq 124 ]]; then
    echo "=== TIMEOUT ===" | tee -a "${OUTPUT_DIR}/harness.log"
    exit 124
elif [[ ${EXIT_CODE} -ne 0 ]]; then
    echo "=== ERROR ===" | tee -a "${OUTPUT_DIR}/harness.log"
    exit ${EXIT_CODE}
fi

echo "=== OpenCode Hybrid Harness Completed ===" | tee -a "${OUTPUT_DIR}/harness.log"
exit 0
