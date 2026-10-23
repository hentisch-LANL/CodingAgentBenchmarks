# API Reference

Auto-generated API documentation for the Automaton Python modules.

## Modules

| Module | Description |
|--------|-------------|
| [`automaton.config`](automaton.config.md) | Configuration loading and management |
| [`automaton.agent`](automaton.agent.md) | Agent definitions and resolution |
| [`automaton.harness`](automaton.harness.md) | Harness definitions and loading |
| [`automaton.container`](automaton.container.md) | Charliecloud container management |
| [`automaton.proxy`](automaton.proxy.md) | Loopback model proxy |
| [`automaton.sandbox`](automaton.sandbox.md) | Sandboxed execution engine |
| [`automaton.usage`](automaton.usage.md) | Usage normalization and cost accounting |

## Quick Reference

### Configuration (`automaton.config`)

```python
from pathlib import Path

from automaton.config import AutomatonConfig

config = AutomatonConfig()                       # Loads ~/.automaton/config.yaml
config = AutomatonConfig(Path("custom.yaml"))    # Custom config path

config.get_model("example-model")               # Get merged model config
config.get_agent("opencode-default")             # Get agent definition
config.get_provider("example")                   # Get provider config
config.providers                                 # All provider configs
config.models                                    # All model configs
config.agents                                    # All agent configs
config.defaults                                  # Default settings
config.timeout_seconds                           # Default timeout
config.sandbox_network                           # Loopback-only sandbox
```

### Agents (`automaton.agent`)

```python
from automaton.agent import Agent

agent = Agent("opencode-default", config)

agent.agent_id        # Configured agent name
agent.harness_id      # Harness selected by this agent
agent.model_names     # Configured model names exposed to the harness
agent.harness_config  # Harness-specific config blob
agent.models          # Resolved model configs with provider defaults merged in

agent.validate()      # Returns list of validation errors
```

### Harnesses (`automaton.harness`)

```python
from automaton.harness import Harness

harness = Harness("opencode", harness_dir="/path/to/harness")

harness.harness_id             # Harness identifier
harness.harness_dir            # Path to harness directory
harness.run_script             # Path to run_benchmark.sh
harness.system_prompt_file     # Path to system_prompt.md
harness.container_template     # Harness container template name
harness.config                 # Parsed harness.yaml

harness.validate()             # Returns list of validation errors
```

### Containers (`automaton.container`)

```python
from automaton.container import (
    ContainerLayout,
    ContainerManager,
    ContainerInfo,
    ContainerRef,
)

manager = ContainerManager(layout)                     # With a ContainerLayout
manager = ContainerManager(layout, cache_root=Path("/path"))
manager = ContainerManager(layout, image_prefix="automaton")

manager.build_container("base")                          # Build base container
manager.build_container("opencode")                      # Build harness layer
manager.build_container("opencode/smoke_test")           # Build one runtime
manager.build_container("base/opencode", force=True)

manager.container_exists("opencode/smoke_test")          # Check if built
manager.delete_container("opencode/smoke_test")          # Delete runtime
manager.clear_cache()                                    # Remove all containers
manager.list_containers()                                # List all
manager.find_container_infos("opencode*")                # Glob-match refs
manager.get_container_info("opencode/smoke_test")        # Get metadata

ContainerRef(kind="runtime", harness_id="opencode", group_id="smoke_test")
```

### Proxy (`automaton.proxy`)

```python
from automaton.proxy import ModelProxy, EndpointRetryExhausted

registry = {
    "demo-model": {
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "model": "demo/model",
    }
}
with ModelProxy(registry, Path("proxy.log")) as proxy:
    print(proxy.usage_records)

# The proxy tracks the last upstream failure for surfacing in output.
print(proxy.last_error_detail)
print(proxy.endpoint_exhausted)
```

`ModelProxy` keeps streaming responses intact for the harness while still
parsing usage from JSON, gzip-encoded JSON, or streamed SSE payloads when the
provider includes usage in the response.

### Sandbox (`automaton.sandbox`)

```python
from automaton.sandbox import Sandbox, ExecutionResult, BindMount, CommandRunFailed

sandbox = Sandbox()
sandbox = Sandbox(host_root=Path("/path"), dry_run=True)
sandbox = Sandbox(
    harness_memory_limit_bytes=8 * 1024 * 1024 * 1024,  # 8 GiB
    stream_output=True,
)

# Run a command (non-agentic)
sandbox.run_command(
    container_path=Path("/var/tmp/automaton-1000/containers/runtime__opencode__smoke_test"),
    argv=["bash", "-c", "python --version"],
    network=False,  # loopback-only
    check=True,
)

# Run an agent (via Sandbox.run_agent)
result: ExecutionResult = sandbox.run_agent(
    container_path=...,
    harness=harness,
    agent=agent,
    workspace=Path("/automaton/run/workspace"),
    run_dir=Path("/path/to/run"),
    container_paths=...,
    bind_mounts=[BindMount(host, container)],
    proxy_log=Path("/path/to/model_proxy.log"),
    usage_records_file=Path("/path/to/usage.jsonl"),
    harness_output_dir=Path("/path/to/harness_output"),
    models_config_file=Path("/path/to/models_config.yaml"),
    harness_config_file=Path("/path/to/harness_config.yaml"),
    extra_env=None,
    timeout=1800,
    sandboxed=True,
)

result.success               # Whether harness exited cleanly
result.returncode              # Harness exit code
result.workspace             # Host path to deliverable workspace
result.endpoint_exhausted    # Whether proxy could not obtain upstream response
result.failure_detail        # Failure detail when success is False
result.proxy_error_detail    # Last upstream/model proxy error
result.usage_records         # Per-request usage/cost records
result.usage_summary         # Aggregated usage summary
result.log_file              # Path to harness process log
result.dry_run               # Whether this was a dry run
```

### Usage (`automaton.usage`)

```python
from automaton.usage import aggregate_usage, compute_cost, normalize_usage

normalized = normalize_usage("/chat/completions", response_json)
cost = compute_cost(normalized, pricing)
summary = aggregate_usage(records)
```

## Usage Examples

### Running a Command Programmatically

```python
from pathlib import Path
from automaton.sandbox import Sandbox

sandbox = Sandbox()

result = sandbox.run_command(
    container_path=Path("/var/tmp/automaton-1000/containers/base"),
    argv=["python", "--version"],
)
```

### Building Containers Programmatically

```python
from automaton.container import ContainerManager, ContainerLayout

layout = ContainerLayout(
    base_template_path=Path("containers/base.Dockerfile.jinja2"),
    harness_template_path=lambda name: Path(f"containers/harnesses/{name}.Dockerfile.jinja2"),
    list_harnesses=lambda: ["opencode"],
    load_harness=lambda name: Harness(name, harness_dir=Path(f"harnesses/{name}")),
    list_runtime_layers=lambda: ["smoke_test"],
    runtime_layer_metadata=lambda name: {"id": name},
    runtime_template_path=lambda name: Path(f"containers/runtime/{name}.Dockerfile.jinja2"),
)

manager = ContainerManager(layout)

# Build base
manager.build_container("base", verbose=True)

# Build final runtime
manager.build_container("opencode/smoke_test", verbose=True)

# Check status
info = manager.get_container_info("opencode/smoke_test")
print(f"Built: {info.exists}, Size: {info.size_human()}")
```

### Loading Harness Definitions

```python
from automaton.harness import Harness

harness = Harness("opencode", harness_dir=Path("harnesses/opencode"))
errors = harness.validate()
```

## Docstring Style

All docstrings use **NumPy format** for consistency with the scientific Python
ecosystem.

## Source Code

All source code is in the `automaton/` package.

## Version

Current version: `0.1.0`

Check version programmatically:

```python
import automaton
print(automaton.__version__)
```
