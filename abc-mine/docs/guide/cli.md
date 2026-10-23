# CLI reference

Complete `--help` output for every subcommand, reproduced from the installed
parser. Regenerate after changing the CLI:

```bash
abc-mine --help
abc-mine <command> --help
```

## Command summary

| Command | Purpose | Parallel |
|---|---|---|
| `init` | Create a run and lock its configuration | — |
| `preflight` | Prove the container can build and test the codebase | — |
| `screen` | Score every selected commit against the rubric | yes |
| `rank` | Collapse viable summaries into a ranked shortlist | no (by design) |
| `build` | Turn each shortlisted commit into a abc-bench task | yes |
| `status` | Report where a run stands | — |
| `export` | Copy verified tasks into a abc-bench checkout | — |
| `prompts` | Inspect, scaffold, and render prompt customization | — |

## Conventions

- **`--config` is required** for every command except `prompts slots`,
  `prompts scaffold`, and `prompts show`.
- **`--plan`** (on `screen`, `rank`, `build`) reports what *would* happen and exits,
  touching nothing. The cheapest way to check a configuration.
- **`--dry-run`** prints the container commands without executing agents.
- **`--only SHA...`** (on `screen`, `build`) restricts to specific commits by prefix
  — useful for reproducing one item's failure.
- Stage commands enforce the run's configuration lock before doing any work; see
  [Checkpointing and resume](checkpointing.md).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A user-facing error, or a stage completed with failed items |
| 130 | Interrupted (Ctrl-C) |

`build` exits 1 if any task failed, even when others succeeded; the successful
tasks are still emitted and exportable.

## Top-level

```text
usage: abc-mine [-h] [--version] <command> ...

Mine a git codebase for agentic-coding benchmarks and emit ABC Bench tasks, in three stages: screen, rank, build.

positional arguments:
  <command>
    init       Create a run and lock its configuration.
    preflight  Prove the container can build and test the codebase, and
               measure cost.
    screen     Score every selected commit against the rubric (parallel).
    rank       Collapse viable summaries into a ranked shortlist (one agent).
    build      Turn each shortlisted commit into a abc-bench task (parallel).
    status     Report where a run stands.
    export     Copy successfully-built tasks into a abc-bench checkout.
    prompts    Inspect, scaffold, and render prompt customization.

options:
  -h, --help   show this help message and exit
  --version    show program's version number and exit

Typical run:
  abc-mine init      --config mine.yaml
  abc-mine preflight --config mine.yaml --write-config
  abc-mine screen    --config mine.yaml --parallel local --workers 8
  abc-mine rank      --config mine.yaml
  abc-mine build     --config mine.yaml --parallel local --workers 4
  abc-mine export    --config mine.yaml --dest ../abc-bench

The model is configured entirely by environment variables:
  LLM_BASE_URL  LLM_MODEL  LLM_API  LLM_API_KEY
```

## `init`

```text
usage: abc-mine init [-h] --config PATH [--notes NOTES]
                           [--allow-missing-model]

options:
  -h, --help            show this help message and exit
  --config PATH         Run configuration YAML.
  --notes NOTES         Free-text note recorded in the manifest.
  --allow-missing-model
                        Initialize without model credentials (e.g. on a
                        submission host).
```

## `preflight`

```text
usage: abc-mine preflight [-h] --config PATH [--dry-run] [--full-test]
                                [--write-config]

options:
  -h, --help      show this help message and exit
  --config PATH   Run configuration YAML.
  --dry-run       Print the container commands without running them.
  --full-test     Also time the full test suite (can be very slow).
  --write-config  Write measured timings back into the config file.
```

## `screen`

```text
usage: abc-mine screen [-h] --config PATH
                             [--parallel {serial,local,mpi}]
                             [--workers WORKERS] [--max-attempts MAX_ATTEMPTS]
                             [--timeout TIMEOUT] [--scratch-dir SCRATCH_DIR]
                             [--keep-logs {none,failed,all}] [--run-id RUN_ID]
                             [--data-root DATA_ROOT] [--allow-config-drift]
                             [--dry-run] [--only SHA [SHA ...]] [--plan]

options:
  -h, --help            show this help message and exit
  --config PATH         Run configuration YAML.
  --only SHA [SHA ...]  Screen only these commits (prefix match).
  --plan                Show what would be screened, then exit.

execution overrides:
  --parallel {serial,local,mpi}
                        Parallel backend. 'serial' (default) needs no MPI;
                        'local' forks worker processes on this node; 'mpi'
                        fans out across ranks and requires the [mpi] extra
                        plus a launcher.
  --workers WORKERS     Worker count for --parallel local.
  --max-attempts MAX_ATTEMPTS
                        Attempts per item before giving up.
  --timeout TIMEOUT     Per-agent timeout, seconds.
  --scratch-dir SCRATCH_DIR
                        Node-local scratch root (keep off shared filesystems).
  --keep-logs {none,failed,all}
                        Agent transcript retention (default: failed).
  --run-id RUN_ID       Override run.id.
  --data-root DATA_ROOT
                        Override run.data_root.
  --allow-config-drift  Proceed even when settings differ from the run's
                        locked configuration. The run's outputs then become a
                        mixture; the change is recorded in the manifest.
  --dry-run             Print container commands without executing agents.
```

## `rank`

```text
usage: abc-mine rank [-h] --config PATH [--parallel {serial,local,mpi}]
                           [--workers WORKERS] [--max-attempts MAX_ATTEMPTS]
                           [--timeout TIMEOUT] [--scratch-dir SCRATCH_DIR]
                           [--keep-logs {none,failed,all}] [--run-id RUN_ID]
                           [--data-root DATA_ROOT] [--allow-config-drift]
                           [--dry-run] [--plan] [--force]

options:
  -h, --help            show this help message and exit
  --config PATH         Run configuration YAML.
  --plan                Show the candidate payload and token estimate, then
                        exit.
  --force               Discard an existing shortlist and rank again.

execution overrides:
  --parallel {serial,local,mpi}
                        Parallel backend. 'serial' (default) needs no MPI;
                        'local' forks worker processes on this node; 'mpi'
                        fans out across ranks and requires the [mpi] extra
                        plus a launcher.
  --workers WORKERS     Worker count for --parallel local.
  --max-attempts MAX_ATTEMPTS
                        Attempts per item before giving up.
  --timeout TIMEOUT     Per-agent timeout, seconds.
  --scratch-dir SCRATCH_DIR
                        Node-local scratch root (keep off shared filesystems).
  --keep-logs {none,failed,all}
                        Agent transcript retention (default: failed).
  --run-id RUN_ID       Override run.id.
  --data-root DATA_ROOT
                        Override run.data_root.
  --allow-config-drift  Proceed even when settings differ from the run's
                        locked configuration. The run's outputs then become a
                        mixture; the change is recorded in the manifest.
  --dry-run             Print container commands without executing agents.
```

## `build`

```text
usage: abc-mine build [-h] --config PATH [--parallel {serial,local,mpi}]
                            [--workers WORKERS] [--max-attempts MAX_ATTEMPTS]
                            [--timeout TIMEOUT] [--scratch-dir SCRATCH_DIR]
                            [--keep-logs {none,failed,all}] [--run-id RUN_ID]
                            [--data-root DATA_ROOT] [--allow-config-drift]
                            [--dry-run] [--only SHA [SHA ...]] [--plan]
                            [--force]

options:
  -h, --help            show this help message and exit
  --config PATH         Run configuration YAML.
  --only SHA [SHA ...]  Build only these commits (prefix match).
  --plan                Show what would be built, then exit.
  --force               Rebuild tasks already recorded as complete.

execution overrides:
  --parallel {serial,local,mpi}
                        Parallel backend. 'serial' (default) needs no MPI;
                        'local' forks worker processes on this node; 'mpi'
                        fans out across ranks and requires the [mpi] extra
                        plus a launcher.
  --workers WORKERS     Worker count for --parallel local.
  --max-attempts MAX_ATTEMPTS
                        Attempts per item before giving up.
  --timeout TIMEOUT     Per-agent timeout, seconds.
  --scratch-dir SCRATCH_DIR
                        Node-local scratch root (keep off shared filesystems).
  --keep-logs {none,failed,all}
                        Agent transcript retention (default: failed).
  --run-id RUN_ID       Override run.id.
  --data-root DATA_ROOT
                        Override run.data_root.
  --allow-config-drift  Proceed even when settings differ from the run's
                        locked configuration. The run's outputs then become a
                        mixture; the change is recorded in the manifest.
  --dry-run             Print container commands without executing agents.
```

## `status`

```text
usage: abc-mine status [-h] --config PATH [-v]

options:
  -h, --help     show this help message and exit
  --config PATH  Run configuration YAML.
  -v, --verbose  Also show the stage invocation history.
```

## `export`

```text
usage: abc-mine export [-h] --config PATH --dest DEST [--abc-bench-root]
                             [--raw] [--force]

options:
  -h, --help        show this help message and exit
  --config PATH     Run configuration YAML.
  --dest DEST       Destination: a abc-bench checkout, or any directory.
  --abc-bench-root  Treat --dest as a abc-bench checkout and write to
                    <dest>/tasks/<group>/ (default).
  --raw             Treat --dest as a literal destination directory.
  --force           Overwrite tasks already present at the destination.
```

## `prompts`

```text
usage: abc-mine prompts [-h] <action> ...

positional arguments:
  <action>
    slots     List the available prompt slots.
    scaffold  Create an empty, commented prompt pack.
    show      Print a shipped base template (a good override starting point).
    render    Render a fully-composed prompt exactly as an agent would receive
              it.

options:
  -h, --help  show this help message and exit
```
