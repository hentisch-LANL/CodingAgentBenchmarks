# Configuration

ABC Bench uses a YAML configuration file to manage model providers, models, and default settings.

## Configuration File Location

The configuration file is loaded from:
1. `~/.abc-bench/config.yaml` (default, created on first run)
2. Custom path via `--config` flag

```bash
# Use custom config
abc-bench run --config /path/to/config.yaml ...
```

## Configuration Structure

### Providers

Providers define API endpoints and authentication. Define once, reference from multiple models.

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
    base_url: http://localhost:8000/v1
    api_key_env: LOCAL_API_KEY
    cert: null
```

| Field | Required | Description |
|-------|----------|-------------|
| `base_url` | Yes | API endpoint base URL (should end with `/v1`) |
| `api_key_env` | Yes | Environment variable name containing the API key |
| `cert` | No | Path to CA certificate bundle, or `null` for system defaults |

### Models

Models reference providers and specify the model identifier.

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

  local-llama:
    provider: local
    model: llama3.1:70b
```

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | Yes | Name of provider defined in `providers` section |
| `model` | Yes | Model identifier as expected by the API |

### Practical Cost Configuration

Most model entries can stay simple. If you want benchmark cost accounting,
attach a `cost` block directly to the model entry.

OpenAI-style example:

```yaml
models:
  openai-gpt5:
    provider: openai
    model: gpt-5
    cost:
      currency: USD
      input_per_1m: 1.25
      cached_input_per_1m: 0.125
      output_per_1m: 10.0
```

This covers the common case where cache reads have a separate price but cache
writes are not billed separately.

If your platform bills cache writes separately, add `cache_write_input_per_1m`:

```yaml
models:
  bedrock-claude-sonnet:
    provider: bedrock
    model: anthropic.claude-sonnet-4-5-20250929-v1:0
    cost:
      currency: USD
      input_per_1m: 3.0
      cached_input_per_1m: 0.3
      cache_write_input_per_1m: 3.75
      output_per_1m: 15.0
```

If write cost depends on cache TTL, use TTL-specific keys:

```yaml
models:
  bedrock-claude-sonnet-ttl:
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

If no cache-write price is configured, ABC Bench treats cache writes as zero
additional cost.

### Direct Model Format

Models can also specify `base_url` and `api_key_env` directly without using a
separate provider section:

```yaml
models:
  my-custom-model:
    base_url: https://custom-api.example.com/v1
    api_key_env: CUSTOM_API_KEY
    model: my-model-name
```

The two layouts exist because they solve slightly different organizational
problems:

- provider-based configuration is useful when many model entries share one API endpoint
- direct model configuration is useful when each configured model is effectively its own endpoint description

### Defaults

Global defaults for benchmark runs:

```yaml
defaults:
  results_dir: results          # Relative to repo root or absolute path
  timeout_seconds: 1800         # Optional explicit benchmark timeout
```

## Environment Variables

API keys are read from environment variables specified in `api_key_env`:

```bash
# For the example provider
export MODEL_API_KEY="sk-..."

# For Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# For OpenAI
export OPENAI_API_KEY="sk-..."

# For local models
export LOCAL_API_KEY="not-needed"
```

## Complete Example

```yaml
# ~/.abc-bench/config.yaml
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

  # Local Ollama
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

If `timeout_seconds` is omitted, ABC Bench does not impose a benchmark-level
timeout by default. Harness-specific or proxy-level request deadlines may still
apply where documented.

## Configuration Validation

Validate your configuration:

```bash
# List models to verify config loads correctly
abc-bench list-models

# Output:
# Available models:
#   example-model (provider: example)
#   example-small (provider: example)
#   anthropic-claude-sonnet (provider: anthropic)
#   ...
```

## Model And Agent Resolution

When you run a benchmark, the runner merges provider settings into each model
config and then resolves the selected agent into a harness plus the list of
models exposed to that harness:

```python
# Internal resolution (happens automatically)
model_config = {
    "provider": "example",
    "model": "your-model-id",
    "base_url": "https://api.example.com/v1",      # From provider
    "api_key_env": "MODEL_API_KEY",                          # From provider
    "cert": null                                              # From provider
}
```

The resolved models are passed to the harness via `models_config.yaml`, and the
agent's harness-specific settings are passed through via `harness_config.yaml`.

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

When ABC Bench proxies model traffic, it honors the provider `cert` field first. If `cert` is `null`, the proxy falls back to standard CA-bundle environment variables such as `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `SSL_CERT_FILE` before using Python's default trust store.

If a provider requires a non-default CA bundle and you do not configure it, benchmark runs can appear to stall while the harness retries failed model requests. In that case, inspect `harness_output/model_proxy.log` or the live `[model-proxy] ...` messages printed during the run.

## Next Steps

- [Quickstart](quickstart.md) — Run your first benchmark
- [Running Benchmarks](running-benchmarks.md) — Execute benchmarks with various options
- [Reference: Configuration Schema](../reference/config-schema.md) — Complete schema reference
