# ABC Bench Documentation

!!! danger "WARNING"
    **This documentation is almost entirely "vibe coded"; while a helpful resource for humans and agents to understand this codebase, it should not be considered a source of ground truth in the way documentation normally is, and the user is responsible for verifying that all instructions given here are safe.**

ABC Bench is a benchmark harness for giving coding agents a task, letting them
work in an isolated workspace, and then grading the result with hidden
verification. It is organized around a small number of explicit concepts:

- a task, which defines the problem and the visible workspace
- a harness, which defines how an agent is run
- model configurations, which tell harnesses what model endpoints are available
- an agent configuration, which binds a harness to one or more models
- a runtime container, which provides the software environment for the task

The current project is still small enough that the docs should be read as a
description of the repository as it exists now, not as a generic benchmarking
framework. In particular, the task-group structure, the Charliecloud-based
container system, and the existing harnesses are all part of what this
repository presently is.

## How A Run Works

At a high level, a benchmark run looks like this:

1. choose a task and an agent configuration
2. stage a fresh run directory with the task workspace
3. run `build_env.sh` for the task inside the appropriate runtime container
4. run the agent through the selected harness
5. inject hidden verification files after the agent finishes
6. run verification in the same runtime container
7. record a PASS/FAIL result plus logs and metadata

The project currently records a binary benchmark outcome. The main result is
PASS or FAIL, while the run directory preserves the workspace, logs, and
metadata needed to understand how that outcome was produced.

## What Is In This Repository

The benchmark currently consists of:

- grouped tasks under `tasks/<group>/<task>/`
- harnesses under `harnesses/<harness>/`
- layered Charliecloud container templates under `containers/`
- Python orchestration code under `abc_bench/`
- generated API reference pages under the Developer Guide

The current task material is the small bundled `smoke_test` group. Heavier
benchmark groups (for example Fortran transpilation and Truchas) live in a
separate `benchmarks` monorepo and are added with `abc-bench install`.

The currently maintained harnesses are `opencode`, `opencode-hybrid`, `codex`,
and `claude-code`. The hybrid harness uses one configured model for planning and
another for code generation, with both routed through the same localhost
proxy/accounting path. The `codex` harness wraps the OpenAI Codex CLI and
captures its JSONL event stream. The `claude-code` harness wraps the Anthropic
Claude Code CLI, which talks to the Anthropic Messages API rather than an
OpenAI-compatible one.

Container layering:

```text
base -> harness -> task group
```

## Quick Links

| Section | Purpose |
|---------|---------|
| [Installation](user-guide/installation.md) | Set up Python, Charliecloud, and local tooling |
| [Quickstart](user-guide/quickstart.md) | Run a benchmark end to end |
| [Configuration](user-guide/configuration.md) | Configure providers and models |
| [Running Benchmarks](user-guide/running-benchmarks.md) | Use the CLI effectively |
| [Managing Containers](user-guide/managing-containers.md) | Build and inspect runtime containers |
| [Architecture](developer-guide/architecture.md) | Understand the execution model |
| [CI/CD](developer-guide/ci-cd.md) | Build, package, docs, and release flow |
| [Adding Tasks](developer-guide/adding-tasks.md) | Add grouped benchmark tasks |
| [Adding Harnesses](developer-guide/adding-harnesses.md) | Add new agent runtimes |

## Current Tasks

The current `smoke_test` task group is small by design. It exists mostly to
exercise the benchmark machinery end to end.

- `smoke_test/hello-world-python`: a minimal benchmark that asks for a simple
  Python function returning `"Hello, world!"`
- `smoke_test/hello-world-prank-python`: structurally similar, but intentionally
  checks whether the agent follows the actual benchmark rather than assuming the
  usual hello-world answer
- `smoke_test/eos-metropolis-1953`: a larger scientific task based on a simple
  Monte Carlo simulation from a historical equation-of-state calculation

## Where To Start

If you are trying to use the project:

- start with [Installation](user-guide/installation.md)
- then read [Quickstart](user-guide/quickstart.md)
- then use [Running Benchmarks](user-guide/running-benchmarks.md) and
  [Understanding Results](user-guide/results.md)

If you are trying to understand or extend the project:

- read [Architecture](developer-guide/architecture.md)
- then [Adding Tasks](developer-guide/adding-tasks.md)
- then [Adding Harnesses](developer-guide/adding-harnesses.md)
- leave [CI/CD](developer-guide/ci-cd.md) for later, once the project structure
  itself is clear

## Useful Entry Points

```bash
abc-bench list-task-groups
abc-bench list-tasks
abc-bench list-containers
abc-bench list-agents
abc-bench run --task smoke_test/hello-world-python --agent opencode-default
```

## Docs Tooling

The docs are built from this repository with MkDocs:

```bash
./docs/scripts/setup-docs-env.sh
./docs/scripts/build-docs.sh
./docs/scripts/serve-docs.sh
```
