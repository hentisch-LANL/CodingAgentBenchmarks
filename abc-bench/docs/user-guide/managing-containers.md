# Managing Containers

ABC Bench uses Charliecloud to run tasks and harnesses in controlled software
environments. Most of the time, the CLI hides the details: when you ask to run a
benchmark, the runner simply expects the appropriate runtime container to exist.

This page explains the layer underneath that behavior: what these containers
are, how the CLI names them, where they are stored, and when you need to think
about them explicitly.

## Build Model

Containers are built in three layers:

```text
base -> harness -> task group
```

Template sources come from two places. The base and harness layers are
ABC Bench-owned assets in the package root:

```text
containers/
  base.Dockerfile.jinja2
  harnesses/
    <harness>.Dockerfile.jinja2
```

The runtime (task-group) layer template lives inside the resolved task-group
directory as `Dockerfile.jinja2`, so each group carries its own runtime layer.

## Container Refs

The CLI exposes the layering directly:

```text
base
<harness>
<harness>/<task-group>
```

Examples:

- `base`
- `opencode`
- `opencode-hybrid`
- `opencode/smoke_test`
- `opencode-hybrid/smoke_test`

`base/<harness>` and `base/<harness>/<task-group>` are also accepted.

## What Gets Built

When you build containers, ABC Bench produces derived artifacts outside the
repository checkout. There are two related storage areas:

- Charliecloud image-builder storage used during builds
- unpacked runtime directories that `ch-run` can execute

Runtime artifacts live under:

```text
/var/tmp/abc-bench-{uid}/containers/
```

Typical paths:

```text
/var/tmp/abc-bench-{uid}/containers/base/
/var/tmp/abc-bench-{uid}/containers/harness__opencode/
/var/tmp/abc-bench-{uid}/containers/harness__opencode-hybrid/
/var/tmp/abc-bench-{uid}/containers/runtime__opencode__smoke_test/
/var/tmp/abc-bench-{uid}/containers/runtime__opencode-hybrid__smoke_test/
```

Charliecloud builder storage lives separately under:

```text
/var/tmp/abc-bench-{uid}/ch-image-storage/
```

## Build Commands

Build the base layer:

```bash
abc-bench build-container base
```

Build a harness layer:

```bash
abc-bench build-container opencode
abc-bench build-container opencode-hybrid
```

Build a final runtime:

```bash
abc-bench build-container opencode/smoke_test
abc-bench build-container opencode-hybrid/smoke_test
```

Build everything:

```bash
abc-bench build-container --all
```

Force rebuild:

```bash
abc-bench build-container opencode/smoke_test --rebuild
abc-bench build-container opencode-hybrid/smoke_test --rebuild
abc-bench build-container --all --rebuild
```

## List Status

```bash
abc-bench list-containers
abc-bench list-containers opencode
abc-bench list-containers opencode-hybrid
abc-bench list-containers 'opencode*'
```

Use an exact ref for detailed status, or a glob to list matching refs.

## Delete Artifacts

Delete the base container:

```bash
abc-bench delete-container base
```

Delete one harness layer:

```bash
abc-bench delete-container opencode
abc-bench delete-container opencode-hybrid
```

Delete one runtime:

```bash
abc-bench delete-container opencode/smoke_test
abc-bench delete-container opencode-hybrid/smoke_test
```

Delete all built artifacts:

```bash
abc-bench delete-container --all
```

Clear both runtime artifacts and Charliecloud build cache:

```bash
abc-bench clear-cache
```

## Important Behavior

- parent layers matter at build time, not runtime
- built artifacts are independent after build
- deleting `opencode` does not delete `opencode/smoke_test`
- deleting `opencode-hybrid` does not delete `opencode-hybrid/smoke_test`
- the runner uses the final harness-plus-group runtime, not the harness layer

The shared base layer should contain common harness/runtime dependencies used by
multiple harnesses. For example, OpenCode's `glob` and `grep` tools require
`ripgrep`, so ABC Bench installs `ripgrep` in the base container rather than in
each OpenCode-derived harness layer.

In practice, this means the CLI presents a fairly simple interface while hiding
most of the container bookkeeping. You usually only need to care about this page
when a container is missing, a build fails, or you want to understand where disk
space is going.

## Custom Storage Root

Override the default root with:

```bash
export ABC_BENCH_CONTAINER_BASE=/scratch/${USER}/abc-bench-containers
```

This is useful on systems where `/var/tmp` is small or slow.

## Troubleshooting

### Build failed because a parent is missing

Normally parents build automatically, but you can build explicitly:

```bash
abc-bench build-container base
abc-bench build-container opencode
abc-bench build-container opencode/smoke_test
abc-bench build-container opencode-hybrid
abc-bench build-container opencode-hybrid/smoke_test
```

### Need to inspect the actual runtime path

```bash
abc-bench list-containers opencode/smoke_test
abc-bench list-containers opencode-hybrid/smoke_test
```

### Out of space

```bash
abc-bench clear-cache
export ABC_BENCH_CONTAINER_BASE=/scratch/${USER}/abc-bench-containers
```

## See Also

- [Container System](../developer-guide/container-system.md)
- [Running Benchmarks](running-benchmarks.md)
