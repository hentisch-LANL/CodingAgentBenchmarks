# Automaton

**Automaton** is a general-purpose engine for running autonomous coding agents
inside hardened, reproducible sandboxes. It builds layered
[Charliecloud](https://hpc.github.io/charliecloud/) containers, brokers all
model traffic through a loopback-only model proxy, runs an agent to produce a
deliverable inside the container's workspace, and lets you run arbitrary
non-agentic code against that deliverable.

Automaton is **content-free**: it ships no concrete harnesses, container
templates, or tasks. You supply those as inputs. This makes it reusable across
projects — for example, [ABC Bench](../abc-bench/README.md)
uses Automaton to run benchmark agents and then verifies their deliverables.

## What it does

1. **Builds layered sandboxes.** A generic `base -> harness -> runtime`
   container build pipeline. You provide the Dockerfile templates and describe
   your runtime layers; Automaton renders, builds, and materializes them.
2. **Runs an autonomous agent in a sandbox.** `Sandbox.run_agent(...)` starts a
   loopback-only model proxy (which injects auth, honors provider TLS, records
   per-request usage/cost, and retries dropped upstream requests within a
   bounded deadline), launches the harness inside the container with no direct
   network access, and returns a content-agnostic `ExecutionResult`. The
   agent's deliverable is left in the bind-mounted workspace directory.
3. **Runs arbitrary code in the same sandbox.** `Sandbox.run_command(...)` runs
   any command inside the container (loopback-only by default, no model proxy)
   — the general-purpose path for setup, tooling, or verifying a deliverable.

## Core concepts

- **`AutomatonConfig`** — providers, models, agents, and harnesses.
- **`Agent`** — a harness paired with one or more resolved models.
- **`Harness`** — how an agent is invoked (run script + prompts), loaded from an
  explicit directory you supply.
- **`ContainerManager` / `ContainerLayout`** — the layered build mechanism; the
  layout tells Automaton where your templates live and what runtime layers
  exist.
- **`ModelProxy`** — the loopback-only broker between the sandboxed harness and
  upstream model APIs, with usage accounting.
- **`Sandbox` / `ExecutionResult`** — sandboxed execution of agents and
  commands.

## Installation

```bash
pip install -e .
```

Requires Python 3.10+ and, for container operations, Charliecloud
(`ch-image`, `ch-convert`, `ch-run`).

## Status

Automaton is a young, one-person project extracted from ABC Bench. The public
API surface is exported from the top-level `automaton` package.

## License

MIT
