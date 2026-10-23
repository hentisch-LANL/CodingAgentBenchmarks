# API Reference

Auto-generated API documentation for ABC Bench Python modules.

> **Note:** The sandbox/agent execution engine — `automaton.Sandbox`,
> `automaton.proxy.ModelProxy`, `automaton.ContainerManager`, `automaton.Agent`,
> `automaton.Harness`, `automaton.AutomatonConfig`, and the usage accounting
> helpers — is provided by the separate `automaton` library and is documented
> in the Automaton project. abc_bench's `ContainerManager` subclasses
> `automaton.ContainerManager`, and its `Config` subclasses
> `automaton.AutomatonConfig`.

## Modules

| Module | Description |
|--------|-------------|
| [`abc_bench.config`](abc_bench.config.md) | Configuration loading and package-root resolution |
| [`abc_bench.task`](abc_bench.task.md) | Task definitions and loading |
| [`abc_bench.groups`](abc_bench.groups.md) | Task-group discovery and resolution (`GroupResolver`) |
| [`abc_bench.install`](abc_bench.install.md) | Installing task groups into `~/.abc-bench/tasks` |
| [`abc_bench.squashfs`](abc_bench.squashfs.md) | Mounting/materializing `.sqfs` task-group images |
| [`abc_bench.container`](abc_bench.container.md) | Charliecloud container management |
| [`abc_bench.runner`](abc_bench.runner.md) | Benchmark orchestration and execution |
| [`abc_bench.cli`](abc_bench.cli.md) | Command-line interface commands |

## Quick Reference

### Configuration (`abc_bench.config`)

```python
from pathlib import Path

from abc_bench.config import Config, get_package_root, initialize_user_config

config = Config()                    # Loads ~/.abc-bench/config.yaml
config = Config(Path("custom.yaml")) # Custom config path

config.get_model("my-model")         # Get merged model config
config.get_agent("opencode-default") # Get agent definition
config.get_provider("example")        # Get provider config
config.models                        # All model configs
config.agents                        # All agent configs
config.providers                     # All provider configs

config.package_root                  # Root holding harnesses/ and containers/
config.installed_root                # Installed task-group root (~/.abc-bench/tasks)
config.group_resolver()              # Build a GroupResolver (CLI > TASK_PATH > installed)
```

### Tasks (`abc_bench.task`)

`Task` is resolver-based: it takes a `GroupResolver` that locates task groups
across the search roots (`--task-dir` > `TASK_PATH` > installed root).

```python
from abc_bench.config import Config
from abc_bench.task import Task

resolver = Config().group_resolver()                       # CLI > TASK_PATH > installed

task = Task("smoke_test/eos-metropolis-1953", resolver)    # Load canonical task ref
task = Task("eos-metropolis-1953", resolver, task_group="smoke_test")

task.build_env_script      # Path to build_env.sh
task.prompt_file           # Path to prompt.md
task.group_prompt_file     # Path to group_prompt.md or None
task.workspace_dir         # Path to workspace/
task.verification_dir      # Path to verification/
task.verify_script         # Path to verify_solution.sh
task.task_ref              # Canonical group/task ref
task.group_id              # Task-group identifier
task.group_dir             # Resolved task-group directory

task.validate()                     # Returns list of errors
Task.list_tasks(resolver)           # List all available tasks
Task.list_task_groups(resolver)     # List all available task groups
```

### Task-Group Discovery (`abc_bench.groups`)

```python
from abc_bench.groups import GroupResolver, installed_tasks_root

# Priority: task_dir (CLI) > task_path (TASK_PATH) > installed_root
resolver = GroupResolver(task_dir=None, task_path=None, installed_root=None, warn=True)

resolver.list_group_ids()                  # Discoverable group ids (deduped)
resolver.has_group("smoke_test")           # True if any search root provides it
resolver.resolve_group_dir("smoke_test")   # Concrete dir (.sqfs materialized to temp)

installed_tasks_root()                     # ~/.abc-bench/tasks
```

### Installing Task Groups (`abc_bench.install`)

```python
from abc_bench.install import install_source, ensure_smoke_test_installed

# Install from a directory, a .sqfs image, or a .tar.gz/.tgz archive.
group_ids = install_source("/path/to/my_group", force=False)

# Auto-seed the bundled smoke_test group if nothing is installed yet.
ensure_smoke_test_installed()
```

### SquashFS Task-Group Images (`abc_bench.squashfs`)

```python
from abc_bench.squashfs import is_squashfs_artifact, materialize_group

is_squashfs_artifact("my_group.sqfs")      # True for a .sqfs file
group_dir = materialize_group("my_group.sqfs")  # Mount + copy to a temp dir
```

Requires `squashfuse` on `PATH` (and `mksquashfs` to build images); raises a
clear `SquashfsError` if `squashfuse` is missing.

### Harnesses (`automaton.Harness`)

`Harness` is now provided by the `automaton` engine. abc_bench no longer
defines its own harness module; the runner imports harnesses from automaton
and supplies them the staged run directory and task-group runtime layer.

```python
from automaton import Harness

# Harness takes a harness_id and an explicit harness_dir (no list_harnesses)
harness = Harness("opencode", "harnesses/opencode")

harness.run_script         # Path to run_benchmark.sh
harness.system_prompt_file # Path to system_prompt.md
harness.config             # Parsed harness.yaml
```

### Containers (`abc_bench.container`)

```python
from abc_bench.config import Config
from abc_bench.container import ContainerManager, ContainerInfo

config = Config()
resolver = config.group_resolver()
manager = ContainerManager(config.package_root, resolver)  # package_root + resolver

manager.build_container("base")                     # Build base container
manager.build_container("opencode")                 # Build harness-layer container
manager.build_container("opencode-hybrid")          # Build hybrid harness layer
manager.build_container("opencode/smoke_test")      # Build one runtime
manager.build_container("opencode-hybrid/smoke_test")
manager.build_container("base/opencode", force=True)

manager.container_exists("opencode/smoke_test")   # Check if built
manager.delete_container("opencode/smoke_test")   # Delete runtime
manager.clear_cache()                              # Remove all containers + builder storage
manager.list_containers()                          # List all (built and available)
manager.find_container_infos("opencode*")         # Glob-match refs
```

The base and harness templates come from `package_root/containers/`; the runtime
(task-group) layer template comes from the resolved group directory's
`Dockerfile.jinja2`.

### Agents (`automaton.Agent`)

`Agent` is now provided by the `automaton` engine. abc_bench no longer defines
its own agent module; the runner imports agents from automaton.

```python
from automaton import Agent

agent = Agent("opencode-default", config)

agent.agent_id        # Configured agent name
agent.harness_id      # Harness selected by this agent
agent.model_names     # Configured model names exposed to the harness
agent.harness_config  # Harness-specific config blob
agent.models          # Resolved model configs with provider defaults merged in
```

### Runner (`abc_bench.runner`)

```python
from abc_bench.config import Config
from abc_bench.runner import BenchmarkRunner, BenchmarkResult
from automaton import ModelProxy

config = Config()
resolver = config.group_resolver()

runner = BenchmarkRunner(
    config,
    resolver,
    dry_run=False,
    stream_output=True,
    clean_env=True,
)

result = runner.run(
    task_id="smoke_test/eos-metropolis-1953",
    agent_id="opencode-default",
)

result.status      # "PASS" or "FAIL"
result.result_dir  # Path to result directory
result.error       # Error message if failed

# ModelProxy (internal use)
registry = {
    "demo-model": {
        "base_url": "https://api.example.com/v1",
        "api_key": "secret",
        "model": "demo/model",
    }
}
with ModelProxy(registry, log_file) as proxy:
    print(proxy.usage_records)

# The proxy tracks the last upstream failure for surfacing in benchmark output.
print(proxy.last_error_detail)
```

`ModelProxy` keeps streaming responses intact for the harness while still
parsing usage from JSON, gzip-encoded JSON, or streamed SSE payloads when the
provider includes usage in the response.

### CLI (`abc_bench.cli`)

```python
from abc_bench.cli import main, parse_memory_limit

main()  # Entry point (called by `abc-bench` command)

parse_memory_limit("8G")     # 8589934592 (bytes)
parse_memory_limit("50%")    # Not supported (only absolute sizes)
```

## Usage Examples

### Running a Benchmark Programmatically

```python
from abc_bench.config import Config
from abc_bench.runner import BenchmarkRunner

config = Config()
resolver = config.group_resolver()
runner = BenchmarkRunner(config, resolver, stream_output=True)

result = runner.run(
    task_id="smoke_test/hello-world-python",
    agent_id="opencode-default",
)

print(f"Status: {result.status}")
print(f"Result dir: {result.result_dir}")
```

### Building Containers Programmatically

```python
from abc_bench.container import ContainerManager
from abc_bench.config import Config

config = Config()
resolver = config.group_resolver()
manager = ContainerManager(config.package_root, resolver)

# Build base
manager.build_container("base", verbose=True)

# Build final runtime
manager.build_container("opencode/smoke_test", verbose=True)
manager.build_container("opencode-hybrid/smoke_test", verbose=True)

# Check status
info = manager.get_container_info("opencode/smoke_test")
print(f"Built: {info.exists}, Size: {info.size_human()}")
```

### Loading Task/Harness Definitions

```python
from abc_bench.config import Config
from abc_bench.task import Task
from automaton import Harness

resolver = Config().group_resolver()

# List available tasks
print(Task.list_tasks(resolver))

# Load specific
task = Task("smoke_test/eos-metropolis-1953", resolver)

# Harnesses are provided by automaton and take an explicit harness_dir.
# (Harness.list_harnesses() no longer exists; harnesses are auto-discovered
# from harnesses/ at the CLI/discovery level.)
harness = Harness("opencode", "harnesses/opencode")
```

### Usage Accounting

```python
from automaton.usage import aggregate_usage, compute_cost, normalize_usage

normalized = normalize_usage("/chat/completions", response_json)
cost = compute_cost(normalized, pricing)
summary = aggregate_usage(records)
```

## Docstring Style

All docstrings use **NumPy format** for consistency with scientific Python ecosystem:

```python
def function_name(param1: type1, param2: type2 = default) -> return_type:
    """
    Short one-line summary.

    Extended description if needed.

    Parameters
    ----------
    param1 : type1
        Description of param1.
    param2 : type2, optional
        Description of param2. Default is `default`.

    Returns
    -------
    return_type
        Description of return value.

    Raises
    ------
    ExceptionType
        Condition when this exception is raised.

    Examples
    --------
    >>> function_name("value", 42)
    'expected output'
    """
```

## Source Code

All source code is in the `abc_bench/` package:
- [GitHub Repository](https://github.com/example/CodingAgentBenchmarks/tree/main/abc-bench/abc_bench)

## Version

Current version: `0.1.19`

Check version programmatically:
```python
import abc_bench
print(abc_bench.__version__)
```
