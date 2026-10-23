# Configuration Schema

Reference for the ABC Bench configuration file.

The configuration schema exists to separate three concerns that would otherwise
be mixed together in every run command:

- how to reach a model API
- which model names should be available to harnesses through the proxy
- which named agents should be available from the CLI
- which defaults should apply to benchmark runs

## Package Root

`package_root` points at the ABC Bench runtime assets: `harnesses/` and the
base/harness container templates under `containers/`. Leave it unset (or `null`)
to auto-detect, which is recommended for most users.

Task groups (benchmarks) are **not** configured in `config.yaml`. They are
discovered at runtime with the following priority, highest first:

1. the `--task-dir` CLI flag (a directory that contains task groups)
2. the `TASK_PATH` environment variable (colon-separated directories, like `PATH`)
3. the installed root `~/.abc-bench/tasks` (populate it with `abc-bench install`)

See [Task Structure](task-structure.md) for the discovery model in detail.

## Top-Level Structure

```yaml
# Optional: path to abc-bench runtime assets (harnesses/ and containers/).
# Leave unset (null) to auto-detect. This is NOT where benchmarks live.
package_root: /path/to/abc-bench

# Provider definitions (required for provider-based models)
providers:
  <provider_name>:
    base_url: <string>      # Required
    api_key_env: <string>   # Required
    cert: <string|null>     # Optional

# Model definitions (required)
models:
  <model_name>:
    provider: <string>      # Required for provider-based format
    model: <string>         # Required
    cost:                   # Optional token pricing, defaults to zero cost
      currency: <string>
      input_per_1m: <number>
      cached_input_per_1m: <number>
      cache_write_input_per_1m: <number>
      output_per_1m: <number>
    # OR legacy format:
    # base_url: <string>
    # api_key_env: <string>
    # model: <string>

# Agent definitions (required for benchmark runs)
agents:
  <agent_name>:
    harness: <string>
    models:
      - <model_name>
    harness_config: <mapping>

# Global defaults (optional)
defaults:
  results_dir: <string>     # Default: "results"
  timeout_seconds: <int>    # Optional explicit benchmark timeout
  sandbox_network: <bool>   # Optional, defaults to true
```

## Providers

Providers define reusable endpoint and credential information.

### Schema

```yaml
providers:
  <provider_name>:
    base_url: <string>      # API base URL (should end with /v1)
    api_key_env: <string>   # Environment variable name for API key
    cert: <string|null>     # Path to CA cert bundle, or null for system defaults
    api: <string>           # Optional API-style hint, interpreted by the harness
```

### Fields

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `base_url` | Yes | string | API endpoint base URL. Automatically normalized to end with `/v1`. |
| `api_key_env` | Yes | string | Name of environment variable containing the API key. |
| `cert` | No | string/null | Path to custom CA certificate bundle. Use `null` for system defaults. |
| `api` | No | string | Opaque API-style hint carried through to the harness. The core runner assigns it no meaning or default. Each harness defines which values it supports and how to wire them. Can be overridden per-model. |

#### The `api` field and the harness boundary

`api` selects the upstream API style (for example, chat/completions vs. the
OpenAI responses API). It is deliberately a **passthrough** value: the runner
and model proxy never interpret it. Instead, it is written verbatim into the
per-run `models_config.yaml` and consumed by the harness, which owns the
vocabulary of valid values and the mapping to a concrete agent wiring.

For the `opencode` and `opencode-hybrid` harnesses:

| `api` value | OpenCode provider npm | API style |
|-------------|-----------------------|-----------|
| `chat_completions` (default) | `@ai-sdk/openai-compatible` | Chat completions |
| `responses` | `@ai-sdk/openai` | OpenAI responses API |

For the `codex` harness, `api` maps to the Codex provider's `wire_api`:

| `api` value | Codex `wire_api` |
|-------------|------------------|
| `responses` (default) | `responses` |
| `chat_completions` | `chat` |

For the `claude-code` harness, only one value is accepted, because the Claude
Code CLI speaks the Anthropic Messages API and cannot be pointed at an
OpenAI-compatible endpoint:

| `api` value | Meaning |
|-------------|---------|
| `anthropic_messages` (default) | Anthropic Messages API (`/v1/messages`) |

Note that the default differs by harness: the OpenCode harnesses default to
`chat_completions`, while `codex` defaults to `responses` because Codex is built
around the Responses API and loses reasoning-item continuity between turns on the
Chat Completions path.

A harness rejects `api` values it does not support. `api` resolves
provider-level with per-model override, matching `base_url`/`cert`.

The single-model `opencode` harness wires one provider block and therefore
requires all exposed models to share one API style. The `opencode-hybrid`
harness exposes a planner and a builder model that may use different API
styles; it emits one provider block per distinct style and routes each model
to the block matching its own `api` value.

### Example

```yaml
providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null

  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    cert: null

  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    cert: null

  local:
    base_url: http://localhost:11434/v1
    api_key_env: OLLAMA_API_KEY
    cert: null

  internal:
    base_url: https://internal-api.example.com/v1
    api_key_env: INTERNAL_API_KEY
    cert: /etc/ssl/certs/internal-ca.pem
```

---

## Models

Models define the names you use from the CLI and map them onto concrete model
identifiers and API settings.

### Provider-Based Format

```yaml
models:
  <model_name>:
    provider: <string>      # Must match a key in providers
    model: <string>         # Model identifier as expected by the API
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `provider` | Yes | string | Name of provider defined in `providers` section |
| `model` | Yes | string | Model identifier (e.g., `your-model-id`) |
| `cost` | No | mapping | Token pricing used for benchmark cost accounting. Missing cost is treated as zero cost. |

**Example**:
```yaml
models:
  example-model:
    provider: example
    model: your-model-id

  example-small:
    provider: example
    model: your-small-model-id

  anthropic-claude-sonnet:
    provider: anthropic
    model: anthropic/claude-sonnet-4-20250514

  anthropic-claude-opus:
    provider: anthropic
    model: anthropic/claude-opus-4-20250514

  openai-gpt4:
    provider: openai
    model: gpt-4

  openai-gpt35:
    provider: openai
    model: gpt-3.5-turbo

  ollama-llama31-70b:
    provider: local
    model: llama3.1:70b

  ollama-codellama:
    provider: local
    model: codellama:34b
```

### Direct Model Format

Models can specify `base_url` and `api_key_env` directly without a provider:

```yaml
models:
  my-custom-model:
    base_url: https://custom-api.example.com/v1
    api_key_env: CUSTOM_API_KEY
    model: my-model-name
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `base_url` | Yes | string | API endpoint base URL |
| `api_key_env` | Yes | string | Environment variable for API key |
| `model` | Yes | string | Model identifier |

The schema supports both layouts because both are useful in practice:

- provider-based configuration reduces repetition when several models share one service description
- direct model configuration keeps everything for one model in one place

### Cost Fields

```yaml
models:
  anthropic-claude-sonnet:
    provider: anthropic
    model: anthropic/claude-sonnet-4-20250514
    cost:
      currency: USD
      input_per_1m: 3.0
      cached_input_per_1m: 0.3
      cache_write_input_per_1m: 0.0
      output_per_1m: 15.0
```

At runtime ABC Bench normalizes these to per-token values. If `cached_input_per_1m`
is omitted, it defaults to `input_per_1m`. If `cache_write_input_per_1m` is
omitted, it defaults to `0`, which lets models/providers that do not bill cache
writes keep the simpler behavior. ABC Bench will account for cache-write tokens
when the upstream usage payload reports them. If `cost` is omitted entirely, the
model is treated as free for benchmark accounting.

Practical rule of thumb:

- OpenAI-style models usually only need `input_per_1m`, `cached_input_per_1m`, and `output_per_1m`
- models/providers that bill prompt-cache writes separately can additionally set `cache_write_input_per_1m`
- if cache-write price depends on retention, add TTL-specific keys such as `cache_write_input_5m_per_1m` or `cache_write_input_1h_per_1m`

Example with separate cache-write pricing:

```yaml
models:
  bedrock-claude-sonnet-cache-priced:
    provider: bedrock
    model: anthropic.claude-sonnet-4-5-20250929-v1:0
    cost:
      currency: USD
      input_per_1m: 3.0
      cached_input_per_1m: 0.3
      cache_write_input_per_1m: 3.75
      output_per_1m: 15.0
```

Example with TTL-specific cache-write pricing:

```yaml
models:
  bedrock-claude-sonnet-cache-ttl:
    provider: bedrock
    model: anthropic.claude-sonnet-4-5-20250929-v1:0
    cost:
      currency: USD
      input_per_1m: 3.0
      cached_input_per_1m: 0.3
      cache_write_input_5m_per_1m: 3.75
      cache_write_input_1h_per_1m: 4.5
      output_per_1m: 15.0
```

---

## Agents

Agents are the unit the CLI runs. An agent binds a harness to one or more
configured models and passes a harness-owned config blob through unchanged.

```yaml
agents:
  opencode-default:
    harness: opencode
    models:
      - example-model
    harness_config: {}

  hybrid-opus-sonnet:
    harness: opencode-hybrid
    models:
      - anthropic-claude-opus
      - anthropic-claude-sonnet
    harness_config:
      plan_model: anthropic-claude-opus
      build_model: anthropic-claude-sonnet
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `harness` | Yes | string | Harness identifier used for runtime/container resolution |
| `models` | Yes | list[string] | Models exposed to the harness through the proxy |
| `harness_config` | No | mapping | Harness-specific config blob passed through unchanged |

ABC Bench only interprets `models`. How those models are mapped to roles or
phases is entirely up to the harness.

---

## Defaults

Global default settings for benchmark runs.

```yaml
defaults:
  results_dir: <string>     # Relative to repo root or absolute path
  timeout_seconds: <int>    # Optional explicit benchmark timeout
  sandbox_network: <bool>   # Loopback-only sandbox for harness + verification
  allowed_provider_executed_tools: <list[str]>   # Default: none allowed
```

| Field | Required | Type | Default | Description |
|-------|----------|------|---------|-------------|
| `results_dir` | No | string | `"results"` | Directory for benchmark results |
| `timeout_seconds` | No | integer | none | Optional benchmark timeout in seconds |
| `sandbox_network` | No | boolean | `true` | Run harness and verification in a loopback-only network namespace |
| `allowed_provider_executed_tools` | No | list of strings | `[]` (none) | Provider-executed tool types the model proxy may forward |

### `allowed_provider_executed_tools`

Some agents can declare tools that are executed by the **model provider's
infrastructure** rather than inside the sandbox — for example the OpenAI
Responses native `web_search`, or Anthropic's server-side `web_fetch`.

These are a containment concern, not a capability preference. A locally executed
`function` tool runs in the container, so the loopback-only network namespace
confines it and its invocation appears in the harness transcript. A
provider-executed tool is performed remotely *inside the model's HTTP response*:
nothing egresses from the container, so `sandbox_network` cannot restrict it and
the transcript shows no tool call. Remote content can be pulled in, and prompt
content can leave inside a provider-side query.

The model proxy therefore **blocks them by default**, before opening the upstream
connection, and the run is recorded as `HARNESS_ERROR` with
`failure_phase: "provider_executed_tool_blocked"` rather than being graded.

Allowing one is an explicit, auditable decision:

```yaml
defaults:
  allowed_provider_executed_tools:
    - web_search
```

The check fails closed: an unrecognized tool type with no local call signature is
treated as provider-executed, so a new native tool in a future agent release is
blocked rather than silently forwarded. See
[Codex Harness](../developer-guide/codex-harness.md#provider-executed-tool-policy-proxy-side).

**Example**:
```yaml
defaults:
  results_dir: results
  timeout_seconds: 1800
  sandbox_network: true

agents:
  opencode-default:
    harness: opencode
    models:
      - example-model
    harness_config: {}
```

If `timeout_seconds` is omitted, ABC Bench does not impose a benchmark-level
timeout by default.

---

## Complete Example

```yaml
# ~/.abc-bench/config.yaml

# Optional: explicit path to abc-bench runtime assets (auto-detected if omitted)
# package_root: /home/user/abc-bench

providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null

  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    cert: null

  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    cert: null

  local:
    base_url: http://localhost:11434/v1
    api_key_env: OLLAMA_API_KEY
    cert: null

models:
  # Example models
  example-model:
    provider: example
    model: your-model-id

  example-small:
    provider: example
    model: your-small-model-id

  # Anthropic models
  anthropic-claude-sonnet:
    provider: anthropic
    model: anthropic/claude-sonnet-4-20250514

  anthropic-claude-opus:
    provider: anthropic
    model: anthropic/claude-opus-4-20250514

  # OpenAI models
  openai-gpt4:
    provider: openai
    model: gpt-4

  openai-gpt35:
    provider: openai
    model: gpt-3.5-turbo

  # Local Ollama models
  ollama-llama31-70b:
    provider: local
    model: llama3.1:70b

  ollama-codellama:
    provider: local
    model: codellama:34b

defaults:
  results_dir: results
  timeout_seconds: 1800
```

---

## Model And Agent Resolution

When you run a benchmark, the runner merges provider settings into each resolved
model config, then resolves the selected agent into `(harness, models, harness_config)`:

```python
# Internal resolution (automatic)
model_config = {
    "provider": "example",
    "model": "your-model-id",
    "base_url": "https://api.example.com/v1",      # From provider
    "api_key_env": "MODEL_API_KEY",                          # From provider
    "cert": null,                                             # From provider
    "api": null                                               # From provider (passthrough, harness-interpreted)
}
```

**Merge behavior**:
- Provider settings are merged into model config
- Model-specific fields override provider settings
- `base_url` is normalized to end with `/v1`
- `api` is carried through verbatim (no normalization, no default at the core layer) and interpreted by the harness
- The harness receives `models_config.yaml` and `harness_config.yaml` in the result directory

---

## Environment Variables

API keys are read from environment variables specified in `api_key_env`:

```bash
# For the example provider
export MODEL_API_KEY="sk-..."

# For Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# For OpenAI
export OPENAI_API_KEY="sk-..."

# For local models (Ollama typically doesn't need a key)
export OLLAMA_API_KEY="not-needed"
```

---

## TLS Certificates

For internal endpoints with custom CAs:

```yaml
providers:
  internal:
    base_url: https://internal-api.example.com/v1
    api_key_env: INTERNAL_API_KEY
    cert: /etc/ssl/certs/internal-ca.pem
```

Set `cert: null` to use system default certificate store.

At runtime, the model proxy uses this `cert` path as the CA bundle for upstream HTTPS verification. If `cert` is `null`, the proxy checks `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `SSL_CERT_FILE`, then falls back to Python's default certificate store.

---

## Validation

Validate your configuration:

```bash
# List models to verify config loads correctly
abc-bench list-models

# List agents to verify agent configs load correctly
abc-bench list-agents

# Output:
# Agents from: ~/.abc-bench/config.yaml
#   opencode-default
#     harness: opencode
#     models: example-model
```

---

## File Locations

| Location | Purpose |
|----------|---------|
| `~/.abc-bench/config.yaml` | User config (default) |
| `configs/example.yaml` | Template with examples |
| `--config PATH` | Custom config via CLI flag |

First `abc-bench` command creates `~/.abc-bench/config.yaml` from template.
