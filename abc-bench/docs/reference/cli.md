# CLI Reference

Reference for the `abc-bench` command-line interface.

The CLI is small. Most commands either discover repository state, build or
delete container artifacts, or run benchmarks.

## Global Options

| Option | Description |
|--------|-------------|
| `--config PATH` | Path to config file |
| `--verbose`, `-v` | Show verbose error output |
| `--help` | Show help |

Default config path:

```text
~/.abc-bench/config.yaml
```

## Task Group Resolution

Commands that touch task groups resolve them from a prioritized set of search
roots, analogous to directories on the shell `PATH`. Each root is a directory
that *contains* task groups (`<root>/<group>/` or `<root>/<group>.sqfs`).
Priority, highest first:

1. the `--task-dir <dir>` CLI flag
2. the `TASK_PATH` environment variable (colon-separated directories, left-to-right)
3. the installed root `~/.abc-bench/tasks` (populated by `abc-bench install`)

The first root that provides a given group id wins; lower-priority roots that
also define it are shadowed, with a warning printed to stderr.

The `--task-dir PATH` flag is accepted by `list-tasks`, `list-task-groups`,
`list-containers`, `build-container`, `delete-container`, `clear-cache`, `run`,
and `run-tasks`. The bundled `smoke_test` group is auto-installed into the
installed root on first use of any command.

## Discovery Commands

### `abc-bench list-task-groups`

List available task groups across all resolved search roots.

```bash
abc-bench list-task-groups [--task-dir PATH] [--config PATH]
```

### `abc-bench list-tasks`

List valid tasks, optionally filtered to one task group.

```bash
abc-bench list-tasks [TASK_GROUP] [--task-dir PATH] [--config PATH]
```

Example:

```text
Available tasks:
  smoke_test
    - eos-metropolis-1953
      canonical: smoke_test/eos-metropolis-1953
    - hello-world-python
      canonical: smoke_test/hello-world-python
```

### `abc-bench list-containers`

List known container refs, show details for one ref, or glob-match multiple refs.

```bash
abc-bench list-containers [CONTAINER_OR_GLOB] [--built-only] [--task-dir PATH] [--config PATH]
```

Examples:

- `abc-bench list-containers`
- `abc-bench list-containers opencode`
- `abc-bench list-containers 'opencode*'`
- `abc-bench list-containers 'base/opencode*'`

### `abc-bench list-models`

List configured models.

```bash
abc-bench list-models [--config PATH]
```

### `abc-bench list-agents`

List configured agents.

```bash
abc-bench list-agents [--config PATH]
```

## Task Group Commands

### `abc-bench install`

Install task group(s) into the installed root `~/.abc-bench/tasks` so they can be
referenced by name without a filesystem path.

```bash
abc-bench install [SOURCE ...] [--force] [--config PATH]
```

Each `SOURCE` may be:

- a task-group directory (contains `group.yaml`),
- a directory that *contains* task groups,
- a squashfs (`.sqfs`) image of a task group, or
- a `.tar.gz` / `.tgz` archive of a task group.

Behavior:

- with no `SOURCE`, seeds the bundled `smoke_test` group if nothing is installed yet
- `--force`, `-f` overwrites an already-installed group of the same id
- `.sqfs` sources require `squashfuse` (and `mksquashfs` to build images)

Examples:

```bash
abc-bench install                         # seed bundled smoke_test if empty
abc-bench install /path/to/my_group       # install a group directory
abc-bench install my_group.sqfs           # install from a squashfs image
abc-bench install my_group.tar.gz         # install from an archive
abc-bench install /path/to/groups         # install every group in a directory
abc-bench install my_group.sqfs --force   # overwrite an existing install
```

## Container Commands

### `abc-bench build-container`

Build a container artifact. Missing parent layers are built automatically.

```bash
abc-bench build-container [CONTAINER_REF] [--all] [--rebuild] [--task-dir PATH] [--config PATH] [-v]
```

Behavior:

- `abc-bench build-container base` builds the shared base layer
- `abc-bench build-container opencode` builds the harness layer
- `abc-bench build-container opencode-hybrid` builds the hybrid harness layer
- `abc-bench build-container opencode/smoke_test` builds the final runtime
- `abc-bench build-container opencode-hybrid/smoke_test` builds the hybrid final runtime
- `abc-bench build-container --all` builds all known artifacts

### `abc-bench delete-container`

Delete built container artifacts.

```bash
abc-bench delete-container [CONTAINER_REF] [--all] [--force] [--task-dir PATH] [--config PATH]
```

Examples:

- `abc-bench delete-container base`
- `abc-bench delete-container opencode`
- `abc-bench delete-container opencode/smoke_test`
- `abc-bench delete-container opencode-hybrid`
- `abc-bench delete-container opencode-hybrid/smoke_test`
- `abc-bench delete-container --all`

### `abc-bench clear-cache`

Remove runtime artifacts and Charliecloud build cache.

```bash
abc-bench clear-cache [--force] [--task-dir PATH] [--config PATH]
```

## Run Commands

### `abc-bench run`

Run a benchmark.

```bash
abc-bench run --task TASK --agent AGENT_ID [OPTIONS]
```

Task forms:

- canonical ref: `smoke_test/eos-metropolis-1953`
- short task id: `eos-metropolis-1953`
- short task id plus explicit `--task-group`

Task groups matter here because the runner uses the task's group to resolve the
final runtime container.

Required arguments:

| Argument | Description |
|----------|-------------|
| `--task TASK` | Canonical `group/task` or short `task` |
| `--agent AGENT_ID` | Agent identifier from config |

Optional arguments:

| Argument | Description |
|----------|-------------|
| `--task-group GROUP` | Explicit task group when `--task` is not canonical |
| `--task-dir PATH` | Directory containing task groups (highest resolution priority) |
| `--timeout SECONDS` | Optional benchmark timeout |
| `--results-dir PATH` | Organized archive root |
| `--output-dir PATH` | Standalone output directory |
| `--dry-run` | Print actions without running |
| `--no-stream-output` | Disable live log streaming |
| `--no-clean-env` | Preserve task environment |
| `--no-sandbox-network` | Disable loopback-only sandboxing for harness and verification |
| `--no-repeat-tasks` | Skip the run if organized results already exist for the same agent configuration |
| `--harness-memory-limit SIZE` | Fixed memory cap |
| `--harness-memory-limit-fraction FRACTION` | Fraction of available memory as cap |
| `--no-harness-memory-limit` | Disable harness memory cap |

Examples:

```bash
abc-bench run --task smoke_test/hello-world-python --agent opencode-default
abc-bench run --task eos-metropolis-1953 --task-group smoke_test --agent opencode-default
abc-bench run --task smoke_test/eos-metropolis-1953 --agent opencode-default --dry-run
```

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | Benchmark finished; inspect `result.json` for PASS, FAIL, ENDPOINT_ERROR, or HARNESS_ERROR |
| `1` | Runtime error |
| `2` | Invalid arguments or configuration |
| `124` | Harness timeout |

Sandbox behavior notes:

- `build_env.sh` still runs with host networking
- by default, harness and verification run in a loopback-only namespace
- sandboxed runs still stream live harness output to the terminal unless `--no-stream-output` is set
- `--no-sandbox-network` disables that namespace isolation for both harness and verification
- `--no-repeat-tasks` only checks the organized results archive, so it is intended for normal archived runs rather than `--output-dir`
- if `--timeout` is omitted, ABC Bench does not impose a benchmark-level timeout by default
- the model proxy still applies a 15-minute per-attempt response timeout and a 1-hour total request deadline

### `abc-bench run-tasks`

Run multiple benchmarks selected by quoted glob patterns over canonical
`group/task` refs.

```bash
abc-bench run-tasks 'GROUP/TASK_GLOB' ['GROUP/TASK_GLOB' ...] --agent AGENT_ID [OPTIONS]
```

Patterns are matched only against canonical task refs such as
`smoke_test/hello-world-python`. They are not matched against filesystem paths
under `tasks/`.

When `--no-repeat-tasks` is set, `run-tasks` skips matched tasks that already
have at least one existing run in the organized results archive for the same
resolved agent configuration.

Required arguments:

| Argument | Description |
|----------|-------------|
| `PATTERN` | One or more quoted canonical task-ref glob patterns |
| `--agent AGENT_ID` | Agent identifier from config |

Optional arguments:

| Argument | Description |
|----------|-------------|
| `--task-dir PATH` | Directory containing task groups (highest resolution priority) |
| `--timeout SECONDS` | Optional benchmark timeout per task |
| `--results-dir PATH` | Organized archive root |
| `--dry-run` | Print actions without running |
| `--no-stream-output` | Disable live log streaming |
| `--no-repeat-tasks` | Skip tasks that already have organized results for the same agent configuration |
| `--no-clean-env` | Preserve task environment |
| `--no-sandbox-network` | Disable loopback-only sandboxing for harness and verification |
| `--harness-memory-limit SIZE` | Fixed memory cap |
| `--harness-memory-limit-fraction FRACTION` | Fraction of available memory as cap |
| `--no-harness-memory-limit` | Disable harness memory cap |

Examples:

```bash
abc-bench run-tasks 'smoke_test/*' --agent opencode-default
abc-bench run-tasks 'smoke_test/*' 'smoke_test/eos-*' --agent opencode-default
abc-bench run-tasks '*/hello-world-*' --agent opencode-default --dry-run
```

Notes:

- quote patterns so your shell does not expand them before `abc-bench` sees them
- unmatched patterns produce warnings if at least one other pattern matched
- if no supplied pattern matches any canonical task ref, the command exits with an error
- `--output-dir` is intentionally not supported for batch runs; use `--results-dir`
- by default, harness and verification run in a loopback-only network namespace; use `--no-sandbox-network` to opt out
- `build_env.sh` is not sandboxed and retains host networking

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TASK_PATH` | Colon-separated directories that contain task groups (like `PATH`) |
| `ABC_BENCH_PACKAGE_ROOT` | Override the package root holding `harnesses/` and `containers/` |
| `ABC_BENCH_CONTAINER_BASE` | Override container storage root |
| `ABC_BENCH_RESULTS_DIR` | Default results archive root |
| `ABC_BENCH_OUTPUT_DIR` | Standalone output directory |
| `HTTP_PROXY` | HTTP proxy for container network access |
| `HTTPS_PROXY` | HTTPS proxy for container network access |

Provider-specific API keys are also read from the environment according to the
configured `api_key_env` fields.

## Notes

- container refs mirror the build tree: `base`, `<harness>`, `<harness>/<task-group>`
- built container artifacts are independent after build
- results are organized by agent archive, task group, and task id
- docs tooling lives under `docs/scripts/`
- task groups are discovered via `--task-dir` > `TASK_PATH` > the installed root
  `~/.abc-bench/tasks`; use `abc-bench install` to populate the installed root
