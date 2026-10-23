# Installation

This guide covers installing ABC Bench and its dependencies.

## Prerequisites

- Charliecloud
- Python 3.10+

Verify Charliecloud:

```bash
ch-run --version
```

## Install From Source

```bash
git clone https://github.com/example/CodingAgentBenchmarks.git
cd CodingAgentBenchmarks/abc-bench
pip install -e ../automaton
pip install -e ".[dev]"
```

## Documentation Environment

Set up the dedicated docs environment:

```bash
./docs/scripts/setup-docs-env.sh
```

Build docs:

```bash
./docs/scripts/build-docs.sh
```

Serve locally:

```bash
./docs/scripts/serve-docs.sh
```

## Verify Installation

```bash
abc-bench list-task-groups
abc-bench list-tasks
abc-bench list-containers
```

## HPC Notes

ABC Bench uses `/var/tmp/abc-bench-{uid}/` by default to store built container
artifacts and Charliecloud image-builder state. In practice, that directory is
used for:

- unpacked runtime containers used by `ch-run`
- Charliecloud image storage used while building layered containers
- other container-related cache files that should not live inside the repository

This is separate from your benchmark result directories. The point of using
`/var/tmp` is to keep large, derived container artifacts in a per-user scratch
area rather than in the checkout itself.

Override container storage if `/var/tmp` is not suitable:

```bash
export ABC_BENCH_CONTAINER_BASE=/scratch/${USER}/abc-bench-containers
```
