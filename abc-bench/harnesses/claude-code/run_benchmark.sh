#!/usr/bin/env bash
# Claude Code CLI harness runner for ABC Bench
#
# Runs `claude -p` (print/headless mode) non-interactively against the staged
# workspace. The prompt is fed on stdin; Claude Code owns the agent loop and
# executes tools inside the same runtime container. Model traffic is routed
# through the ABC Bench model proxy declared in models_config.yaml.

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
CLAUDE_OUTPUT_DIR="${OUTPUT_DIR}/claude-code"
mkdir -p "${CLAUDE_OUTPUT_DIR}"

# Claude Code writes user state (config, cache, session/debug logs, and a
# projects/ tree) under HOME and CLAUDE_CONFIG_DIR. The container HOME
# (/var/tmp/automaton-home) is shared and chmod 0777, so state must not land
# there: parallel attempts on one node would collide. mktemp -d gives each
# attempt its own root. /var/tmp rather than /tmp mirrors the codex harness and
# keeps this off a small tmpfs, since the CLI's cache is not tiny.
CLAUDE_RUNTIME_DIR="$(mktemp -d /var/tmp/abc-bench-claude-code.XXXXXX)"
trap 'rm -rf "${CLAUDE_RUNTIME_DIR}"' EXIT
CLAUDE_STATE_HOME="${CLAUDE_RUNTIME_DIR}/home"
mkdir -p "${CLAUDE_STATE_HOME}" "${CLAUDE_RUNTIME_DIR}/tmp"

# ---------------------------------------------------------------------------
# Resolve model + proxy wiring from models_config.yaml and harness_config.yaml
# ---------------------------------------------------------------------------
# Claude Code talks to an Anthropic-compatible endpoint selected by
# ANTHROPIC_BASE_URL, which here is the ABC Bench proxy. A single `claude -p`
# invocation drives one model, so only the first exposed model is used;
# harness_config knobs are read here too.
readarray -t CLAUDE_VALUES < <(python - "${MODELS_CONFIG}" "${HARNESS_CONFIG}" <<'PY'
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
# `opencode_model`. Claude Code has no provider-prefix concept, so we send the
# bare upstream model id, which the proxy also accepts.
upstream_model = model.get("model")
if not upstream_model:
    raise SystemExit("Error: first exposed model has no `model` id")

# --- Harness boundary: interpret the opaque `api` hint ---------------------
# Core carries `api` verbatim from provider/model config and assigns it no
# meaning or default. This harness owns the vocabulary. Claude Code speaks only
# the Anthropic Messages API (/v1/messages); it cannot be pointed at an
# OpenAI-compatible endpoint, so every other value is rejected here rather than
# failing later with an opaque HTTP error.
DEFAULT_API = "anthropic_messages"
SUPPORTED_API = {"anthropic_messages"}
api_style = model.get("api") or DEFAULT_API
if api_style not in SUPPORTED_API:
    raise SystemExit(
        f"Error: claude-code harness does not support api '{api_style}'. "
        f"Supported: {sorted(SUPPORTED_API)}. Claude Code speaks the Anthropic "
        f"Messages API only."
    )

# harness_config knobs, all optional.
effort = harness_config.get("effort") or ""
VALID_EFFORT = {"", "low", "medium", "high", "xhigh", "max"}
if effort not in VALID_EFFORT:
    raise SystemExit(
        f"Error: invalid effort '{effort}'. Supported: {sorted(VALID_EFFORT - {''})}"
    )

# An agent-loop cap, NOT a count of benchmark attempts. Empty means uncapped.
max_turns = harness_config.get("max_turns") or ""
if max_turns and not str(max_turns).isdigit():
    raise SystemExit(f"Error: max_turns must be an integer, got '{max_turns}'")

max_budget_usd = harness_config.get("max_budget_usd") or ""
if max_budget_usd:
    try:
        if float(max_budget_usd) <= 0:
            raise ValueError
    except ValueError:
        raise SystemExit(
            f"Error: max_budget_usd must be a positive number, got '{max_budget_usd}'"
        )

# The tools the agent is permitted to use. `tools` in harness_config overrides
# this; it is the allow list, not the offered list.
DEFAULT_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"]
tools = harness_config.get("tools") or DEFAULT_TOOLS
if isinstance(tools, str):
    tools = [name.strip() for name in tools.split(",") if name.strip()]
if not isinstance(tools, list) or not tools:
    raise SystemExit("Error: tools must be a non-empty list or comma-separated string")
tools = [str(name).strip() for name in tools]
for name in tools:
    if not name or any(char.isspace() for char in name) or "," in name:
        raise SystemExit(f"Error: invalid tool name '{name}'")

# Tools that are *offered* to the model but never allowed, so that calling one
# yields a transparent refusal rather than the capability simply being absent.
# The agent can then say plainly that it was denied web access instead of
# silently reasoning without it. See the invocation below; this is a
# legibility/honesty choice, not the containment boundary.
REFUSED_TOOLS = ["WebSearch", "WebFetch"]
offered = list(tools) + [name for name in REFUSED_TOOLS if name not in tools]

for value in (
    upstream_model,
    base_url,
    api_key_env,
    effort,
    max_turns,
    max_budget_usd,
    ",".join(tools),
    ",".join(offered),
    model.get("name") or "",
):
    print(value)
PY
)

# `readarray < <(...)` runs the producer in a subshell, so a SystemExit in the
# Python block above does NOT trip `set -e`. Check the parsed output explicitly,
# or a config error would silently degrade into an empty/garbled invocation.
if [[ "${#CLAUDE_VALUES[@]}" -ne 9 ]]; then
    echo "Error: failed to parse model configuration (expected 9 values, got ${#CLAUDE_VALUES[@]})" >&2
    echo "       check ${MODELS_CONFIG} and ${HARNESS_CONFIG}" >&2
    exit 1
fi

CLAUDE_MODEL="${CLAUDE_VALUES[0]}"
PROXY_BASE_URL="${CLAUDE_VALUES[1]}"
API_KEY_ENV="${CLAUDE_VALUES[2]}"
EFFORT="${CLAUDE_VALUES[3]}"
MAX_TURNS="${CLAUDE_VALUES[4]}"
MAX_BUDGET_USD="${CLAUDE_VALUES[5]}"
ALLOWED_TOOLS="${CLAUDE_VALUES[6]}"
OFFERED_TOOLS="${CLAUDE_VALUES[7]}"
MODEL_NAME="${CLAUDE_VALUES[8]}"

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

# ---------------------------------------------------------------------------
# Environment for the CLI
# ---------------------------------------------------------------------------
export HOME="${CLAUDE_STATE_HOME}"
export TMPDIR="${CLAUDE_RUNTIME_DIR}/tmp"
# Keep every writable state root inside the per-run dir, so nothing is shared
# between attempts and everything is discarded by the EXIT trap.
export CLAUDE_CONFIG_DIR="${CLAUDE_STATE_HOME}/.claude"
export XDG_CONFIG_HOME="${CLAUDE_RUNTIME_DIR}/config"
export XDG_CACHE_HOME="${CLAUDE_RUNTIME_DIR}/cache"
export XDG_DATA_HOME="${CLAUDE_RUNTIME_DIR}/data"
export XDG_STATE_HOME="${CLAUDE_RUNTIME_DIR}/state"
mkdir -p "${CLAUDE_CONFIG_DIR}" "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" \
    "${XDG_DATA_HOME}" "${XDG_STATE_HOME}"

# Auth: the CLI reads the proxy token from ANTHROPIC_API_KEY. In print mode this
# is used directly with no interactive key-approval step. The token is read from
# the variable named by api_key_env so it never appears in argv or run metadata.
export ANTHROPIC_API_KEY="${!API_KEY_ENV}"
export ANTHROPIC_BASE_URL="${PROXY_BASE_URL}"
# ANTHROPIC_AUTH_TOKEN (a bearer-token gateway credential) must not also be set:
# an inherited one would change which credential is sent.
unset ANTHROPIC_AUTH_TOKEN || true

# The sandbox has no route off localhost, so every non-essential outbound call
# (update checks, telemetry, error reporting) would stall and then fail. Turn
# them off rather than paying their timeouts on every run.
export DISABLE_AUTOUPDATER=1
export DISABLE_UPDATES=1
export DISABLE_TELEMETRY=1
export DISABLE_ERROR_REPORTING=1
export DISABLE_BUG_COMMAND=1
export DISABLE_COST_WARNINGS=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# Claude Code's own request retries are deliberately left at their default (it
# retries a failed request with exponential backoff, honoring Retry-After).
#
# automaton's proxy retries only *transport* failures (a dropped or refused
# connection); an upstream HTTP error such as a 503 "Overloaded" is forwarded to
# the client verbatim by design. The CLI is therefore the only thing that can
# ride out a transient 5xx, and suppressing its retries made a single 503 end a
# run that had already done real work. The two layers cover disjoint failure
# classes, so they do not compound.
#
# An endpoint that genuinely will not answer still ends the run as a harness
# failure. Nothing here turns a dead endpoint into a graded result.
# Claude Code's own Bash sandbox is bubblewrap-based and cannot nest inside
# Charliecloud's user namespace; ABC Bench already confines the run via the
# container plus a network namespace, which is the boundary here.
export CLAUDE_CODE_BUBBLEWRAP=0

# The container image installs claude at a known absolute path; fall back to
# PATH lookup so the script also works against a locally provided binary.
if [[ -x /usr/local/bin/claude ]]; then
    CLAUDE_BIN=/usr/local/bin/claude
elif CLAUDE_BIN="$(command -v claude 2>/dev/null)"; then
    :
else
    echo "Error: claude binary not found at /usr/local/bin/claude or on PATH" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Build the Claude Code invocation
# ---------------------------------------------------------------------------
SETTINGS_FILE="${CLAUDE_OUTPUT_DIR}/settings.json"
cp "${SCRIPT_DIR}/settings.json" "${SETTINGS_FILE}"

# `--tools` selects which built-in tools exist at all; `--allowedTools`
# pre-approves permission for them. BOTH are required: `--permission-mode
# dontAsk` denies anything not explicitly allowed, so passing `--tools` alone
# leaves the agent able to see Write/Edit/Bash but denied when it calls them,
# and it politely reports that it cannot edit the file. That looks exactly like
# a model failure while actually being a harness misconfiguration.
#
# Web access is handled by offering WebSearch/WebFetch but NOT allowing them, so
# the agent sees the tool, calls it, and is transparently refused with a message
# it can read and adapt to. That is deliberate: a flat refusal the agent can
# observe is better than silently withholding the capability, because the agent
# then reports honestly that it could not consult the web rather than being
# unable to explain a gap in its own reasoning.
#
# The refusal is a policy decision, not an error, so it is not treated as a
# failure below. Note also that the refusal is NOT the containment boundary: the
# agent can and will try to route around it with `curl` (observed). What actually
# prevents egress is `sandbox_network`'s loopback-only namespace, in which DNS
# does not resolve and no external host is reachable. This setting exists so the
# model is told "no" in terms it understands, not to secure the sandbox.
#
# Anthropic's genuinely provider-executed tools (web_search_20250305,
# web_fetch_20250910) are a different mechanism entirely -- they run on the
# provider's own infrastructure inside the model's HTTP response, where neither
# the network namespace nor the transcript can see them -- and are independently
# rejected by automaton's proxy. Claude Code's WebSearch/WebFetch are ordinary
# client-executed tools (name + input_schema, no `type`), so that guard does not
# and need not apply to them.
CLAUDE_ARGS=(
    -p
    --model "${CLAUDE_MODEL}"
    --output-format stream-json
    --verbose
    # Non-interactive: nothing can answer a permission prompt, so anything that
    # would prompt must be denied rather than left hanging.
    --permission-mode dontAsk
    --permission-prompts none
    --tools ${OFFERED_TOOLS//,/ }
    --allowedTools ${ALLOWED_TOOLS//,/ }
    --settings "${SETTINGS_FILE}"
    # Load only this run's own settings; ignore project/local settings that a
    # task workspace could otherwise use to change harness behavior.
    --setting-sources user
    # Ignore any MCP server configured anywhere else, and configure none.
    --strict-mcp-config
    --mcp-config '{"mcpServers":{}}'
    # Attempts must not inherit conversation state from each other.
    --no-session-persistence
)
if [[ -n "${EFFORT}" ]]; then
    CLAUDE_ARGS+=(--effort "${EFFORT}")
fi
if [[ -n "${MAX_TURNS}" ]]; then
    CLAUDE_ARGS+=(--max-turns "${MAX_TURNS}")
fi
if [[ -n "${MAX_BUDGET_USD}" ]]; then
    CLAUDE_ARGS+=(--max-budget-usd "${MAX_BUDGET_USD}")
fi

{
    echo "=== Claude Code Harness Starting ==="
    echo "Workspace: ${WORKSPACE}"
    echo "Model (upstream id): ${CLAUDE_MODEL}"
    echo "Model (config name): ${MODEL_NAME}"
    echo "Proxy base URL: ${PROXY_BASE_URL}"
    echo "API key env: ${API_KEY_ENV}"
    echo "Tools (allowed): ${ALLOWED_TOOLS}"
    echo "Tools (offered, refused on call): ${OFFERED_TOOLS}"
    echo "Effort: ${EFFORT:-<model default>}"
    echo "Max turns: ${MAX_TURNS:-<uncapped>}"
    echo "Max budget USD: ${MAX_BUDGET_USD:-<uncapped>}"
    echo "Claude binary: ${CLAUDE_BIN}"
    echo "Claude version: $(${CLAUDE_BIN} --version 2>&1)"
    echo "CLAUDE_CONFIG_DIR: ${CLAUDE_CONFIG_DIR}"
    echo "HOME: ${HOME}"
    echo "TMPDIR: ${TMPDIR}"
    echo "Settings file: ${SETTINGS_FILE}"
    echo "Task env: ${TASK_ENV}"
    echo "Prompt file: ${COMBINED_PROMPT_FILE}"
    if [[ -n "${TIMEOUT_SECONDS}" ]]; then
        echo "Timeout: ${TIMEOUT_SECONDS}s"
    else
        echo "Timeout: disabled"
    fi
    echo "===================================="
    echo ""
} | tee "${HARNESS_LOG}"

EVENTS_FILE="${CLAUDE_OUTPUT_DIR}/events.jsonl"
EVENTS_SUMMARY="${CLAUDE_OUTPUT_DIR}/events_summary.json"
CLAUDE_STDERR="${CLAUDE_OUTPUT_DIR}/stderr.log"

# The CLI's JSONL stream goes to the renderer, which preserves it verbatim and
# emits a readable transcript. stderr is kept separate so CLI diagnostics are not
# interleaved into the event stream. The prompt arrives on stdin so it is never
# interpolated into a command line.
set +e
if [[ -n "${TIMEOUT_SECONDS}" ]]; then
    timeout "${TIMEOUT_SECONDS}" "${CLAUDE_BIN}" "${CLAUDE_ARGS[@]}" \
        < "${COMBINED_PROMPT_FILE}" \
        2> >(tee "${CLAUDE_STDERR}" >&2) \
        | python "${SCRIPT_DIR}/lib/render_events.py" "${EVENTS_FILE}" "${EVENTS_SUMMARY}" \
        | tee -a "${HARNESS_LOG}"
else
    "${CLAUDE_BIN}" "${CLAUDE_ARGS[@]}" \
        < "${COMBINED_PROMPT_FILE}" \
        2> >(tee "${CLAUDE_STDERR}" >&2) \
        | python "${SCRIPT_DIR}/lib/render_events.py" "${EVENTS_FILE}" "${EVENTS_SUMMARY}" \
        | tee -a "${HARNESS_LOG}"
fi
# PIPESTATUS[0] is claude/timeout; $? would be tee's status, which is ~always 0.
EXIT_CODE="${PIPESTATUS[0]}"
set -e

# Collect the CLI's own debug/session logs for post-mortem debugging before the
# EXIT trap discards the runtime dir.
CLAUDE_LOG_DIR="${CLAUDE_OUTPUT_DIR}/logs"
mkdir -p "${CLAUDE_LOG_DIR}"
find "${CLAUDE_STATE_HOME}" -type f \( -name '*.log' -o -name '*.txt' \) \
    -size -8M -exec cp --parents -t "${CLAUDE_LOG_DIR}" {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# Report outcome
# ---------------------------------------------------------------------------
# The result event is authoritative for how the agent loop ended. Note that
# `subtype` alone is NOT a success check: an API error still reports
# subtype "success" while setting is_error true and terminal_reason "api_error".
RESULT_FIELDS=""
if [[ -f "${EVENTS_SUMMARY}" ]]; then
    RESULT_FIELDS="$(python - "${EVENTS_SUMMARY}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1]) as f:
        summary = json.load(f) or {}
except Exception:
    summary = {}
print(summary.get("result_subtype") or "")
print("true" if summary.get("is_error") else "")
print(summary.get("terminal_reason") or "")
print(summary.get("api_error_status") or "")
print(len(summary.get("permission_denials") or []))
PY
)"
fi
readarray -t RESULT_VALUES <<< "${RESULT_FIELDS}"
RESULT_SUBTYPE="${RESULT_VALUES[0]:-}"
RESULT_IS_ERROR="${RESULT_VALUES[1]:-}"
TERMINAL_REASON="${RESULT_VALUES[2]:-}"
API_ERROR_STATUS="${RESULT_VALUES[3]:-}"
PERMISSION_DENIALS="${RESULT_VALUES[4]:-0}"

{
    echo ""
    echo "Claude Code exit code: ${EXIT_CODE}"
    echo "Result subtype: ${RESULT_SUBTYPE:-<none>}"
    echo "Result is_error: ${RESULT_IS_ERROR:-false}"
    echo "Terminal reason: ${TERMINAL_REASON:-<none>}"
    echo "API error status: ${API_ERROR_STATUS:-<none>}"
    echo "Permission denials: ${PERMISSION_DENIALS:-0}"
    echo "Events: ${EVENTS_FILE}"
    echo "Event summary: ${EVENTS_SUMMARY}"
    echo "Claude Code stderr: ${CLAUDE_STDERR}"
} | tee -a "${HARNESS_LOG}"

if [[ ${EXIT_CODE} -eq 124 ]]; then
    {
        echo ""
        echo "=== TIMEOUT ==="
        echo "Claude Code execution timed out after ${TIMEOUT_SECONDS} seconds"
    } | tee -a "${HARNESS_LOG}"
    exit 124
elif [[ ${EXIT_CODE} -ne 0 ]]; then
    {
        echo ""
        echo "=== ERROR ==="
        echo "Claude Code execution failed with exit code ${EXIT_CODE}"
        if [[ -s "${CLAUDE_STDERR}" ]]; then
            echo "--- claude stderr (tail) ---"
            tail -n 40 "${CLAUDE_STDERR}"
        fi
    } | tee -a "${HARNESS_LOG}"
    exit "${EXIT_CODE}"
fi

# A zero exit with an errored result means the agent loop ended badly even though
# the process itself succeeded. Surface it as a harness failure rather than
# letting a run that never really started be graded as a wrong answer.
if [[ -n "${RESULT_IS_ERROR}" ]]; then
    {
        echo ""
        echo "=== ERROR ==="
        echo "Claude Code reported an errored result (terminal_reason: ${TERMINAL_REASON:-<none>})"
    } | tee -a "${HARNESS_LOG}"
    exit 1
fi

# NOTE: a `permission_denied` event is deliberately NOT treated as a failure.
# Denials are an expected, legitimate outcome: the harness rejects some tools on
# purpose (web access, below), and a rejected tool call is exactly what that
# looks like. A denial caused by a harness misconfiguration is indistinguishable
# from an intended policy rejection in this event, so there is no sound way to
# branch on it -- the count is recorded in the event summary for observability
# and left at that.

{
    echo ""
    echo "=== Claude Code Harness Completed ==="
} | tee -a "${HARNESS_LOG}"

exit 0
