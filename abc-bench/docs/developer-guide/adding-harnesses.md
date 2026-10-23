# Adding Harnesses

Guide to adding a new harness to ABC Bench.

A harness is the layer that tells ABC Bench how to run a particular agent. It
is not the model configuration, and it is not the task. It is the execution
adapter between the benchmark runner and an agent tool.

## Harness Model

A harness has two pieces:

- runtime assets under `harnesses/<harness>/`
- a container template under `containers/harnesses/<harness>.Dockerfile.jinja2`

This split is deliberate. Harness directories contain runtime assets and
metadata. Container recipes live with the rest of the layered container system.

## Required Files

Harness runtime assets:

```text
harnesses/<harness>/
  harness.yaml
  run_benchmark.sh
  system_prompt.md
  ...
```

Container template:

```text
containers/harnesses/<harness>.Dockerfile.jinja2
```

## Harness Metadata

Example `harness.yaml`:

```yaml
id: my-harness
description: My custom harness
entrypoint: run_benchmark.sh
container_template: my-harness
```

`container_template` maps to:

```text
containers/harnesses/my-harness.Dockerfile.jinja2
```

## Standard Interface

`run_benchmark.sh` is the critical file. It must accept the staged benchmark
inputs supplied by the runner.

The effective interface is:

```bash
./run_benchmark.sh \
  --workspace PATH \
  --task-prompt PATH \
  --system-prompt PATH \
  --models-config PATH \
  --harness-config PATH \
  --output-dir PATH \
  --timeout-seconds N \
  --task-env PATH
```

The script should:

- validate arguments
- read the generated models config and harness config
- export the right API credentials for the agent
- combine or otherwise supply the system and task prompts
- run inside the staged workspace
- write logs into `--output-dir`
- respect the timeout contract

That interface is what makes it possible for the runner to treat different agent
backends uniformly.

## Container Template

Example:

```dockerfile
FROM {{ parent_image }}

RUN pip install my-agent-package

WORKDIR /workspace
```

`{{ parent_image }}` is rendered by the container manager so the harness layer
inherits from the shared base image.

Prefer putting common runtime dependencies in `containers/base.Dockerfile.jinja2`
when more than one harness will need them. For example, OpenCode's `glob` and
`grep` tools require `ripgrep`, so ABC Bench installs `ripgrep` in the shared
base layer rather than duplicating it in each OpenCode-derived harness
container.

## Build Flow

Build the harness layer:

```bash
abc-bench build-container my-harness
```

Build a final runtime for one task group:

```bash
abc-bench build-container my-harness/smoke_test
```

The second command is the one that matters for actual benchmark execution.

## Quick Start

The easiest path is to copy an existing harness and then update both the runtime
assets and the container template.

```bash
cp -r harnesses/opencode harnesses/my-harness
```

Then create:

```text
containers/harnesses/my-harness.Dockerfile.jinja2
```

Which harness to copy from matters:

- `harnesses/opencode` is the simplest single-model example.
- `harnesses/opencode-hybrid` shows a two-model planner/builder flow and reading
  `harness_config`.
- `harnesses/codex` shows wrapping a CLI that emits a JSONL event stream, and
  isolating per-run agent state.
- `harnesses/claude-code` shows a CLI on a non-OpenAI wire protocol (the
  Anthropic Messages API), restricting the agent's tool set, and distinguishing
  an errored agent result from a process failure.

Copy the exit-status handling from `opencode-hybrid` or `codex`, not from
`opencode`: see the pipeline warning under [Common Mistakes](#common-mistakes).

## What To Test

Before treating a harness as usable, verify all three layers:

1. `abc-bench build-container my-harness`
2. `abc-bench build-container my-harness/smoke_test`
3. `abc-bench run --task smoke_test/hello-world-python --agent my-agent`

## Common Mistakes

- adding a harness directory without adding a matching container template
- building only the harness layer and forgetting the task-group runtime
- assuming the runner executes the harness layer directly
- not writing logs to the harness output directory
- hard-coding provider credentials instead of using the generated models config
- reading `$?` after piping the agent into `tee`: that captures `tee`'s status,
  which is essentially always `0`, so timeouts and agent failures silently look
  like success. Use `EXIT_CODE="${PIPESTATUS[0]}"`.
- writing agent state into the container `HOME` (`/var/tmp/automaton-home`),
  which is shared and mode `0777` and therefore collides across parallel
  attempts. Create a per-run `mktemp -d` and clean it up in a `trap`.
- treating `--group-prompt` and `--timeout-seconds` as always present: the
  runner omits `--group-prompt` when the task group has no `group_prompt.md`.
- granting a tool's *availability* without granting *permission* to call it. An
  agent that can see a tool but is refused when it calls it typically explains
  that it cannot do the task and exits `0`, which grades as a plausible `FAIL`
  while actually being a harness bug. Check the allow list against the offered
  list directly; note that a denial *event* cannot distinguish this bug from an
  intended refusal, so it is not a usable signal on its own.
- trusting a CLI's "success" field without checking its error field. Some CLIs
  report a successful *invocation* alongside `is_error: true` for an API failure;
  reading only the former grades a run that never reached the model.
- disabling the agent CLI's own request retries on the assumption that the model
  proxy covers them. The proxy retries **transport** failures only; an upstream
  HTTP `5xx` is forwarded to the client verbatim, so the CLI is the only thing
  that can ride out a transient one.

## See Also

- [Container System](container-system.md)
- [Codex Harness](codex-harness.md)
- [Claude Code Harness](claude-code-harness.md)
- [Harness Interface](../reference/harness-interface.md)
- [Architecture](architecture.md)
