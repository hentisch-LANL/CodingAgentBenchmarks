# Adding Tasks

Guide to creating new benchmark tasks and task groups.

A task is the unit of work an agent is asked to solve. In practice, a task is a
directory that contains a prompt, a visible workspace, and hidden verification.
Tasks live inside a *task group*, which is a self-contained, installable
directory. ABC Bench discovers task groups from the filesystem, resolves them,
stages a task into a run directory, and then asks a harness to operate on the
staged workspace.

## Task Group First

A task group is a self-contained directory (or a `.sqfs` image of one) that does
not need to live inside the `abc-bench` source tree. It bundles everything its
tasks need:

```text
<group>/
  group.yaml
  group_prompt.md            # optional shared prompt guidance
  Dockerfile.jinja2          # the group's runtime container-layer template
  <task>/
```

If you are creating a new group, add `group.yaml` and a `Dockerfile.jinja2`
first.

Example `group.yaml`:

```yaml
id: smoke_test
name: Smoke Test
description: Small sanity-check benchmarks for ABC Bench infrastructure and basic agent behavior.
```

`container_template` is optional; it defaults to `Dockerfile.jinja2` in the group
directory. Set it only if you want a different template filename.

Example `Dockerfile.jinja2` (the runtime, task-group container layer):

```dockerfile
FROM {{ parent_image }}

RUN pip install numpy scipy

WORKDIR /workspace
```

`{{ parent_image }}` is rendered by the container manager so the group layer
inherits from the harness layer. See
[Container System](container-system.md).

## Task Layout

Each runnable task directory must contain:

```text
<group>/<task>/
  build_env.sh
  prompt.md
  workspace/
  verification/
    verify_solution.sh
```

In practice, `verification/verify_solution.sh` is required, and `workspace/`
normally contains the editable solution stub and supporting files.

## Quick Start

Copy an existing task in the same group:

```bash
cp -r smoke_test/eos-metropolis-1953 smoke_test/my-new-task
chmod +x smoke_test/my-new-task/build_env.sh
chmod +x smoke_test/my-new-task/verification/verify_solution.sh
```

## Core Files

### `build_env.sh`

Runs inside the final runtime container before the agent starts.

Use it for task-local environment setup. Keep it idempotent.

Minimal example:

```bash
#!/usr/bin/env bash
set -euo pipefail
exit 0
```

If a whole task group needs the same system stack, prefer putting that in the
group's `Dockerfile.jinja2` instead of repeating work in every task.

### `prompt.md`

This is the task description visible to the agent. It should clearly describe:

- what to implement
- expected function signatures
- input and output format
- constraints and edge cases
- any scientific or numerical context the agent actually needs

### `workspace/`

This is the visible working directory staged for the run.

Typical contents:

- `solution.py`
- `README.md`
- reference data, papers, or support files

### `verification/`

This directory is hidden from the agent until execution is complete.

Typical contents:

- `verify_solution.sh`
- `test_solution.py`

`verify_solution.sh` should print `PASS` or `FAIL` as the final status line and
exit nonzero on failure.

## Local Verification

```bash
cd smoke_test/my-new-task/workspace
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
```

## Installing And Discovering Groups

Task groups are discovered from a prioritized set of search roots (like a shell
`PATH`), highest priority first:

1. the `--task-dir <dir>` CLI flag (a directory that *contains* task groups)
2. the `TASK_PATH` environment variable (colon-separated directories)
3. the installed root `~/.abc-bench/tasks`, populated by `abc-bench install`

While developing a group, point the CLI directly at the directory that contains
it:

```bash
abc-bench list-tasks --task-dir /path/to/groups
abc-bench run --task-dir /path/to/groups --task my_group/my-new-task --agent opencode-default
```

When a group is ready, install it so it can be referenced by name without a
path. Sources may be a directory, a `.sqfs` image, or a `.tar.gz`/`.tgz`
archive:

```bash
abc-bench install /path/to/groups/my_group        # a group directory
abc-bench install my_group.sqfs                    # a squashfs image
abc-bench install my_group.tar.gz                  # an archive
```

With no argument, `abc-bench install` seeds the bundled `smoke_test` group if
nothing is installed yet. `smoke_test` is also auto-installed on first use of any
command.

Check discovery with:

```bash
abc-bench list-task-groups
abc-bench list-tasks
```

## Discovery Rules

ABC Bench does not assume every subdirectory under a group is a task. It only
treats structurally valid directories as runnable tasks.

That means you can keep helper or source-corpus directories in a task group
without exposing them as benchmarks, as long as they do not look like full task
directories.

## Dependency Placement

Use the narrowest correct place for dependencies:

- task-group-wide system stack: the group's `Dockerfile.jinja2`
- task-local runtime setup: `build_env.sh`
- editable benchmark inputs: `workspace/`

This keeps grouped tasks coherent and avoids repeating the same setup logic.

## Squashfs Groups

A task group may be delivered as a `<group>.sqfs` image instead of a plain
directory; the two are interchangeable everywhere a group is accepted.
ABC Bench mounts the image read-only with `squashfuse` and copies its contents
to a temporary directory. Both `squashfuse` and `mksquashfs` are required for
this path, and ABC Bench raises a clear error if `squashfuse` is missing.

## Heavy And Generated Task Families

Some benchmarks need large or generated workspaces that are impractical to keep
in the `abc-bench` source tree. These are built and packaged in a separate
`benchmarks` monorepo and shipped as expanded workspaces inside a task group
(directory, `.sqfs`, or archive), then installed with `abc-bench install`. There
is no runtime workspace synthesis: every task ships an expanded `workspace/` on
disk.

The Fortran transpilation group is one example of such a generated task family
whose canonical source of truth is a maintained corpus plus generation scripts
rather than the runnable task directories themselves. It is no longer part of
the `abc-bench` repository; it lives in the separate `benchmarks` monorepo and
is delivered as an installable task group.

## Good Task Design

For most tasks:

- keep verification deterministic
- give the agent enough context to solve the task without revealing hidden tests
- prefer reference values, fixed seeds, and explicit tolerances for scientific work
- keep the visible workspace focused on what the agent should actually edit or read

## See Also

- [Task Structure](../reference/task-structure.md)
- [Architecture](architecture.md)
