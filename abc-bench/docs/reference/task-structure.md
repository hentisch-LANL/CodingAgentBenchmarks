# Task Structure

Reference for how ABC Bench represents task groups and tasks on disk.

The task schema is intentionally filesystem-oriented. A task is not defined by a
separate registry entry or metadata object. Instead, the directory layout is the
schema, and the runner discovers runnable tasks by checking whether a directory
contains the required parts.

## Task Groups Are Self-Contained And External

A *task group* is the discrete, installable unit of benchmarks. It is a
self-contained directory — or a squashfs (`.sqfs`) image of one — that bundles
everything needed to build and run its tasks: group metadata, an optional shared
prompt, the group's runtime container-layer template, and one subdirectory per
task.

Crucially, task groups do **not** have to live inside the `abc-bench` source
tree. A group is a relocatable directory that can live anywhere on disk, be
packed into a `.sqfs` image, be shipped in a `.tar.gz` archive, or be installed
into `~/.abc-bench/tasks`. See [Discovery and Resolution](#discovery-and-resolution)
below.

## Directory Layout

```text
<group>/
  group.yaml
  group_prompt.md            # optional; shared prompt guidance for the whole group
  Dockerfile.jinja2          # the group's runtime container-layer template
  <task-id>/
    build_env.sh
    prompt.md
    workspace/
    verification/
      verify_solution.sh
```

Example:

```text
smoke_test/
  group.yaml
  Dockerfile.jinja2
  hello-world-python/
    build_env.sh
    prompt.md
    workspace/
    verification/
      verify_solution.sh
```

The group's `Dockerfile.jinja2` is the runtime (task-group) container layer. It
lives **inside the group directory** so the group is fully self-contained and
relocatable. (In earlier versions of ABC Bench this template lived in the
source tree at `containers/task_groups/<group>.Dockerfile.jinja2`; it now travels
with the group.) See [Container System](../developer-guide/container-system.md).

## Group Metadata

Each task group defines `group.yaml`:

```yaml
id: smoke_test
name: Smoke Test
description: Small sanity-check benchmarks for ABC Bench infrastructure and basic agent behavior.
```

Fields:

- `id`: canonical task-group identifier; must match the group directory name
- `name`: human-readable label
- `description`: short description of the group
- `container_template`: **optional** filename, relative to the group directory,
  for the runtime container-layer template. Defaults to `Dockerfile.jinja2` when
  omitted, so most groups need not set it.

## Runtime Container Template

Every group ships its own runtime container-layer template. By default this is
`<group>/Dockerfile.jinja2`. It is rendered on top of the harness layer as the
final `base -> harness -> task group` layer:

```dockerfile
FROM {{ parent_image }}

RUN pip install numpy scipy

WORKDIR /workspace
```

If you want a different filename, set `container_template:` in `group.yaml` to a
path relative to the group directory.

## Optional Group Prompt

A task group may include a `group_prompt.md` alongside its `group.yaml`. When
present, the runner stages it and the harness injects its contents into the
agent's prompt as a **"Task Group Instructions"** section, placed between the
harness system prompt and the per-task `prompt.md`. When absent, no group
section is added and behavior is unchanged.

This is the right place for guidance that is common to every task in the group
but does not belong in the generic harness system prompt — most importantly, how
to build, run, and test the group's software stack.

The `Task.group_prompt_file` property returns the path when the file exists and
`None` otherwise.

## Canonical Task Identity

Tasks are identified canonically by path:

```text
<task-group>/<task-id>
```

Examples:

- `smoke_test/hello-world-python`
- `smoke_test/eos-metropolis-1953`

There is no separate `task.yaml` today.

## Required Task Files

Each runnable task directory must contain:

- `build_env.sh`
- `prompt.md`
- `workspace/`
- `verification/`
- `verification/verify_solution.sh`

In practice, `workspace/solution.py` and `verification/test_solution.py` are
also common and expected for normal Python tasks.

Every task ships an expanded `workspace/` on disk. Heavy benchmarks that need
large or generated workspaces ship those expanded workspaces inside the group
(built in a separate `benchmarks` monorepo and delivered via
`abc-bench install`); there is no runtime workspace synthesis.

## Meaning Of The Main Files

### `build_env.sh`

Runs inside the final runtime container before the harness starts. Use it for
task-local environment setup.

### `prompt.md`

The visible task description passed to the agent.

### `workspace/`

The staged working directory the agent can inspect and modify.

### `verification/`

Hidden verification injected after the agent completes.

## Discovery and Resolution

Task groups are discovered from a prioritized set of *search roots*, analogous
to directories on the shell `PATH`. A search root is a directory that *contains*
task groups (`<root>/<group>/` or `<root>/<group>.sqfs`). Priority, highest
first:

1. an explicit `--task-dir <dir>` supplied on the command line,
2. the colon-separated `TASK_PATH` environment variable (left-to-right), and
3. the installed root `~/.abc-bench/tasks` (populated by `abc-bench install`).

The first root that provides a given group id wins; lower-priority roots that
also define that id are shadowed, with a warning printed to stderr. This
resolution is implemented by `abc_bench.groups.GroupResolver`.

The tiny bundled `smoke_test` group is auto-installed into the installed root on
first use of any command, so ABC Bench works out of the box.

### Squashfs Groups

A group may be supplied as a `<group>.sqfs` image instead of a directory.
ABC Bench mounts it read-only with `squashfuse` and copies its contents to a
temporary directory, then treats it exactly like a directory-backed group. Both
`squashfuse` and `mksquashfs` are required for this path; ABC Bench raises a
clear error if `squashfuse` is missing.

## Discovery Rules

ABC Bench scans each group directory and only includes structurally valid task
directories in discovery.

- `Task.list_task_groups(resolver)` returns task-group ids
- `Task.list_tasks(resolver)` returns canonical `group/task` refs
- `Task.list_tasks_by_group(resolver)` returns grouped short task ids

This allows a task group to keep helper directories, source corpora, or
generation inputs without exposing them as runnable tasks.

## CLI Resolution Rules

Tasks can be referenced in two ways.

Canonical form:

```bash
abc-bench run --task smoke_test/eos-metropolis-1953 ...
```

Short id with explicit group when needed:

```bash
abc-bench run --task eos-metropolis-1953 --task-group smoke_test ...
```

If a short task id is ambiguous across groups, `--task-group` is required.

Point the resolver at a group directory that is not installed with `--task-dir`:

```bash
abc-bench run --task-dir /path/to/groups --task mygroup/mytask ...
```

## Local Testing

```bash
cd smoke_test/hello-world-python/workspace
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
```

## Recommendations

- use task groups to cluster benchmarks with similar dependencies
- put shared system stack changes in the group's `Dockerfile.jinja2`
- put shared build/test instructions for the group in `group_prompt.md`
- keep `build_env.sh` focused on task-local setup
- keep verification deterministic and hidden

## See Also

- [Adding Tasks](../developer-guide/adding-tasks.md)
- [Container System](../developer-guide/container-system.md)
