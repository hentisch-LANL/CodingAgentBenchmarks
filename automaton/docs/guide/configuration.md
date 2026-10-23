# Configuration

Automaton uses a YAML configuration file to manage model providers, models, and
default settings.

## Configuration File Location

The configuration file is loaded from:

1. `~/.automaton/config.yaml` (default, created on first run)
2. A custom path passed to `AutomatonConfig(config_path=Path("/path/to/config.yaml"))`

## Configuration Structure

The config is content-free: it does not own tasks, harnesses, or container
assets. A benchmark orchestrator supplies those as inputs and may subclass
`AutomatonConfig` to re-add `data_dir`/`results_dir` if it needs them.

### Providers

Providers define API endpoints and authentication. Define once, reference from
multiple models.

```yaml
providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null
    # api: the upstream API style for this provider. Each harness defines how
    #      it wires a style ("chat_completions" (default) | "responses" |
    #      "anthropic_messages"), and the model proxy uses it to pick the
    #      upstream authentication scheme: "anthropic_messages" authenticates
    #      with x-api-key, the others with Authorization: Bearer.
    #      Can be overridden per-model. Leave unset for the harness default.

  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    cert: null
    api: anthropic_messages
```

An `api` value the proxy does not recognize is rejected when the proxy starts,
rather than after an agent has been provisioned. A mismatch between the declared
style and the endpoint the harness actually calls is also refused: sending the
wrong credential scheme upstream returns a bare 401 that reads like an invalid
key, which is far harder to diagnose than a named configuration error. For
`anthropic_messages` the accepted endpoints are exactly `/messages` and
`/messages/count_tokens`; any other path is refused.

| Field | Required | Description |
|-------|----------|-------------|
| `base_url` | Yes | API endpoint base URL (automatically normalized to end with `/v1`) |
| `api_key_env` | Yes | Environment variable name containing the API key |
| `cert` | No | Path to CA certificate bundle, or `null` for system defaults |
| `api` | No | Opaque API-style hint carried through to the harness. The core engine assigns it no meaning or default; each harness interprets it and applies its own default when it is absent. Can be overridden per-model. |

### Models

Models reference providers and specify the model identifier.

```yaml
models:
  example-model:
    provider: example
    model: your-model-id
    cost:
      currency: USD
      input_per_1m: 0
      output_per_1m: 0

  anthropic-claude:
    provider: anthropic
    model: anthropic/claude-sonnet-4
    cost:
      currency: USD
      input_per_1m: 0
      output_per_1m: 0
```

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | Yes | Name of provider defined in `providers` section |
| `model` | Yes | Model identifier as expected by the API |
| `cost` | No | Token pricing used for cost accounting. Missing cost is treated as zero cost. |

#### Cost configuration

Most model entries can stay simple. If you want cost accounting, attach a
`cost` block:

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

If your platform bills cache writes separately:

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

At runtime Automaton normalizes these to per-token values. If
`cached_input_per_1m` is omitted, it defaults to `input_per_1m`. If
`cache_write_input_per_1m` is omitted, it defaults to `0`.

#### Direct model format

Models can also specify `base_url` and `api_key_env` directly without using a
separate provider section:

```yaml
models:
  my-custom-model:
    base_url: https://custom-api.example.com/v1
    api_key_env: CUSTOM_API_KEY
    model: my-model-name
```

### Agents

```yaml
agents:
  opencode-default:
    harness: opencode
    models:
      - example-model
    harness_config: {}
```

| Field | Required | Description |
|-------|----------|-------------|
| `harness` | Yes | Harness identifier used for runtime/container resolution |
| `models` | Yes | Models exposed to the harness through the proxy |
| `harness_config` | No | Harness-specific config blob passed through unchanged |

### Defaults

Global defaults:

```yaml
defaults:
  timeout_seconds: 1800
  sandbox_network: true
  allowed_provider_executed_tools: []
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `timeout_seconds` | No | `1800` | Default timeout in seconds |
| `sandbox_network` | No | `true` | Run harness in a loopback-only network namespace |
| `allowed_provider_executed_tools` | No | `[]` (none) | Provider-executed tool types the model proxy may forward |

#### `allowed_provider_executed_tools`

Tools that the model provider executes on its own infrastructure — for example
the OpenAI Responses native `web_search`, or Anthropic's server-side `web_fetch`
— rather than the agent executing them in the sandbox.

`sandbox_network` cannot confine these: the work happens inside the model's HTTP
response, so no connection leaves the container and the harness transcript shows
no tool call. They are blocked by default; naming one here is an explicit,
auditable decision to permit it.

```yaml
defaults:
  allowed_provider_executed_tools:
    - web_search
```

Names are the wire `type` values and are matched case-insensitively. See
[Architecture](architecture.md#provider-executed-tool-policy).

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

When Automaton's model proxy connects to an upstream, it uses the provider
`cert` path as the CA bundle for upstream HTTPS verification. If `cert` is
`null`, the proxy checks `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and
`SSL_CERT_FILE`, then falls back to Python's default trust store.

## Complete Example

```yaml
# ~/.automaton/config.yaml
providers:
  example:
    base_url: https://api.example.com/v1
    api_key_env: MODEL_API_KEY
    cert: null

  anthropic:
    base_url: https://api.anthropic.com/v1
    api_key_env: ANTHROPIC_API_KEY
    cert: null

models:
  example-model:
    provider: example
    model: your-model-id
    cost:
      currency: USD
      input_per_1m: 0
      output_per_1m: 0

  anthropic-claude:
    provider: anthropic
    model: anthropic/claude-sonnet-4
    cost:
      currency: USD
      input_per_1m: 0
      output_per_1m: 0

agents:
  opencode-default:
    harness: opencode
    models:
      - example-model
    harness_config: {}

defaults:
  timeout_seconds: 1800
  sandbox_network: true
```

## Subclassing AutomatonConfig

Downstream projects may subclass `AutomatonConfig` to add their own settings.
For example, ABC Bench subclasses it to add `data_dir` and `results_dir`:

```python
class LanlbenchConfig(AutomatonConfig):
    @property
    def data_dir(self) -> Path:
        return Path(self.data.get("data_dir", "."))

    @property
    def results_dir(self) -> Path:
        return Path(self.data.get("results_dir", "results"))
```

## See also

- [Architecture](architecture.md)
- [Harness Interface](harness-interface.md)
- [API Reference](../api-reference/automaton.config.md)
