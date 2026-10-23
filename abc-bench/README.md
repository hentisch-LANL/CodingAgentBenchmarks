# ABC Bench
NOTE: Currently, this README and project have been heavily "vibe coded", and much of this README (and project) may fairly be considered "slop".

**ABC Bench** is an autonomous benchmark suite for scientific computing tasks, inspired by SWE-Bench. It evaluates AI models on their ability to solve computational science problems by autonomously writing code, running simulations, and producing verifiable results.

Documentation: https://example.com/abc-bench/

## Overview

ABC Bench tests AI models by:
1. Providing a scientific problem description and supporting materials
2. Allowing the model to work autonomously in a workspace
3. Verifying the solution against hidden test cases
4. Reporting PASS or FAIL

The benchmark is designed to be:
- **Autonomous**: Models solve tasks without human intervention
- **Verifiable**: Solutions are tested against deterministic, hidden test suites
- **Reproducible**: All execution happens in Charliecloud containers
- **Extensible**: Easy to add new task groups, tasks, and harnesses

## Architecture

ABC Bench has four main concepts:

### 1. Task Groups and Tasks

Tasks are organized canonically by group:

```text
tasks/<task-group>/
  group.yaml
  <task-id>/
    build_env.sh
    prompt.md
    workspace/
    verification/
```

Current tasks all live in the `smoke_test` group:

```text
tasks/
  smoke_test/
    group.yaml
    hello-world-python/
    hello-world-prank-python/
    eos-metropolis-1953/
```

You can refer to a task either as:
- canonical: `smoke_test/eos-metropolis-1953`
- short form: `eos-metropolis-1953` when unambiguous

### 2. Harnesses

Harnesses define how an agent is invoked, but do not own container recipes:

```text
harnesses/<harness-id>/
  harness.yaml
  run_benchmark.sh
  system_prompt.md
  ...
```

### 3. Container Templates

Runtime containers are built in three layers:

```text
base -> harness -> task group
```

Template sources live under:

```text
containers/
  base.Dockerfile.jinja2
  harnesses/
    opencode.Dockerfile.jinja2
    opencode-hybrid.Dockerfile.jinja2
  task_groups/
    smoke_test.Dockerfile.jinja2
```

Built container refs are exposed as:
- `base`
- `<harness>` as shorthand for `base/<harness>`
- `<harness>/<task-group>` as shorthand for `base/<harness>/<task-group>`

### 4. Runner

The runner orchestrates benchmark execution:
1. Resolves the task and task group
2. Selects the matching runtime container for `(harness, task_group)`
3. Starts a localhost model proxy that injects auth and honors provider TLS settings
4. Sets up the task workspace
5. Launches the harness inside Charliecloud
6. Runs verification
7. Writes result metadata including task-group information

## Getting Started

### Prerequisites

- Python 3.10+
- Charliecloud (`ch-image`, `ch-run`, `ch-convert`)
- Git

### Installation

```bash
git clone <monorepo-url> CodingAgentBenchmarks
cd CodingAgentBenchmarks/abc-bench
pip install -e ../automaton
pip install -e .
```

ABC Bench uses the sibling `../automaton` component in this monorepo. Install
it before ABC Bench; no submodule initialization or registry dependency is needed.
Develop engine changes directly in that sibling component.

`pip install -e .` installs the Python package only. Generated benchmark
datasets are maintained through explicit build targets rather than as an
installation side effect.

Installed package note:

- the Python package bundles only the tiny `smoke_test` group plus harness and
  container base/harness templates; heavy benchmark groups are NOT packaged
- task groups are discovered at runtime via the `--task-dir` flag, the
  `TASK_PATH` environment variable, or the installed root `~/.abc-bench/tasks`
  (populated with `abc-bench install`; `smoke_test` is auto-installed on first
  use)
- harness/container base assets come from a repo checkout or a directory given
  through `ABC_BENCH_PACKAGE_ROOT`

### Task Groups Beyond `smoke_test`

The Python package bundles only the tiny `smoke_test` group. Heavy benchmark
groups (for example `fortran_transpilation` and `truchas`) are no longer part of
this repository. They are built in a separate `benchmarks` monorepo and shipped
as installable task groups, then added with `abc-bench install`:

```bash
abc-bench install ./fortran_transpilation.sqfs   # .sqfs, .tar.gz, or a directory
abc-bench run --task fortran_transpilation/linpack-sgefa --agent opencode-default
```

Installed groups land under `~/.abc-bench/tasks/<group>/` and are then
discoverable like any other group.

### Running the Tests

ABC Bench is not a published package: it is used from a checkout (or the dev
container described in `dev_container.Dockerfile`), never installed from a
package registry. Install it editable and run the suite:

```bash
pip install ./../automaton
pip install -e ".[dev]"
python -m pytest tests
```

Use the same installation sequence in your CI environment.

### Build Containers

Build the shared base container:

```bash
abc-bench build-container base
```

Build a harness-layer container:

```bash
abc-bench build-container opencode
abc-bench build-container opencode-hybrid
```

Build a runtime for a specific harness and task group:

```bash
abc-bench build-container opencode/smoke_test
abc-bench build-container opencode-hybrid/smoke_test
```

Build everything:

```bash
abc-bench build-container --all
```

### List Available Items

```bash
abc-bench list-task-groups
abc-bench list-tasks
abc-bench list-tasks smoke_test
abc-bench list-containers
abc-bench list-containers opencode
abc-bench list-models
abc-bench list-agents
```

### Run a Benchmark

Canonical task path:

```bash
abc-bench run \
  --task smoke_test/eos-metropolis-1953 \
  --agent opencode-default
```

Short task id with explicit group:

```bash
abc-bench run \
  --task eos-metropolis-1953 \
  --task-group smoke_test \
  --agent opencode-default
```

Dry run:

```bash
abc-bench run \
  --task smoke_test/hello-world-python \
  --agent opencode-default \
  --dry-run
```

## Result Layout

Results are grouped by agent, task group, and task:

```text
results/
  <agent_archive_dir>/
    <task-group>/
      <task-id>/
        <timestamp>_<agent>_<task-id>/
```

Example:

```text
results/
  opencode-default_96b33089/
    smoke_test/
      eos-metropolis-1953/
        20260615T120000_opencode-default_eos-metropolis-1953/
          workspace/
          verification/
          harness_output/
          result.json
          metadata.json
          usage.json
```

`result.json` and `metadata.json` both include:
- `task`
- `task_group`
- `task_ref`

`metadata.json` also includes wall-clock timing and host-machine metadata such
as `hostname`, host OS details, CPU model, total memory, and NVIDIA GPU
information when `nvidia-smi` is available.

During harness execution, `harness_output/model_proxy.log` captures proxied model traffic and upstream failures such as TLS verification errors, authentication failures, or model-access denials. With normal streamed output, those failures are also printed live as `[model-proxy] ...` lines.

Prompt/token accounting is aggregated into `usage.json`, and the underlying
per-request records are written to `harness_output/usage.jsonl`.

## Current Task Group

### `smoke_test`

Current tasks:
- `hello-world-python`
- `hello-world-prank-python`
- `eos-metropolis-1953`

## Current Harnesses

- `opencode`
- `opencode-hybrid`

## Development

Run tests:

```bash
python -m pytest tests
```

(Use `PYTHONPATH=../automaton python -m pytest tests` when the sibling
automaton component is not installed into your environment.)

Format code:

```bash
black abc_bench/
```

Type checking:

```bash
mypy abc_bench/
```

Documentation tooling:

```bash
bash docs/scripts/setup-docs-env.sh
bash docs/scripts/build-docs.sh
./docs/scripts/serve-docs.sh
```

## CI Overview

ABC Bench is **not** a published package. There is no wheel build, no upload to
any package registry, and no release job; the project is consumed from a git
checkout (or the dev container). CI therefore only tests the code and builds the
documentation.

CI can split responsibilities into portable runner roles:

- A test runner installs sibling `../automaton` and this component editable,
  then runs `python -m pytest tests`. Container integration checks additionally
  need Charliecloud, squashfuse/mksquashfs, and permitted user namespaces.
- A documentation runner installs the docs dependencies and builds with
  MkDocs. Publishing is optional and configured for your hosting service.

See [CI checks](docs/developer-guide/ci-cd.md) for component commands and the
sibling [ABC CI documentation](../abc-ci/README.md) for CI orchestration.

## Troubleshooting

### Missing runtime container

If a runtime is missing, build the harness/task-group pair:

```bash
abc-bench build-container <harness-id>/<task-group>
```

### Charliecloud errors

Verify Charliecloud is installed:

```bash
ch-run --version
```

### Dry run mode

Use `--dry-run` to inspect commands without executing them:

```bash
abc-bench run --task smoke_test/hello-world-python --agent opencode-default --dry-run
```

## License

MIT
