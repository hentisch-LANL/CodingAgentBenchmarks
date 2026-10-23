# Public source snapshot

This import prepares the four components for a single public repository without
importing their previous Git histories. The original source checkouts are not
modified. The GitHub README's release-approval statement and copyright notices
are retained.

## Naming

| Component | Distribution / command | Python import |
| --- | --- | --- |
| Agent engine | `automaton` (library only) | `automaton` |
| Benchmark suite | `abc-bench` | `abc_bench` |
| Mining tools | `abc-mine` | `abc_mine` |
| CI launcher | `abc-ci` command (see launcher packaging) | `abc_ci_launcher` |

Underscores are used for Python imports, avoiding a collision with Python's
standard-library `abc` module. The renamed configuration paths, environment
variables, and commands are a breaking change; consult each component's guide.

## Source provenance

The snapshots originate from these local source revisions. The mining snapshot
also includes working-tree changes and new source/tests present when exported;
the revision alone does not reproduce those changes.

| Original component | Source revision |
| --- | --- |
| Automaton | `c7f97d9d337b79d31b1682f509430174e635666b` |
| Benchmark suite | `fd700745776264633b5e89c1a58136f916a60e6f` |
| CI tooling | `675a1b7dabe093c5ea74b9f7877db7215325883a` |
| Mining tools | `172da6fd40db03a19eb5d182e410b1bcfb1112aa` |

## Release boundary

Included: component source code, tests, documentation, benchmark smoke tasks,
container templates, harnesses, the report renderer, and its source assets.

Excluded: nested repository metadata and histories, private submodule
configuration, site-specific GitLab pipelines, the site CA certificate, Darwin
batch-submission scripts, development-agent instruction files, generated sites,
local environments/caches, result archives, crash dumps, and unrelated benchmark
or legacy-project directories. The CI renderer directory named `site/` contains
source and is intentionally retained. Vendor/model logo image files are excluded;
the scientific paper bundled with the Monte Carlo smoke task is retained, as
confirmed by the repository owner.

Examples use configurable or placeholder provider endpoints and paths. Supply
credentials through environment variables or your CI secret store. Do not commit
your local runtime configuration.

## Before publication

- Review the final diff and the release boundary against the approved scope.
- Review your own pipeline, runner isolation, credentials, and storage settings.
- Exercise a real containerized benchmark and a deployment on the intended host;
  unit tests do not validate a site's Charliecloud or Slurm configuration.

No original repository history is needed to install or run this checkout.

## Preparation validation

The renamed source snapshot was checked with Python 3.14:

| Component | Unit tests |
| --- | --- |
| Automaton | 94 passed, 1 skipped |
| ABC Bench | 135 passed |
| ABC Mine | 463 passed |
| ABC CI (launcher and report renderer) | 29 passed |

All four MkDocs sites build successfully. Editable package installation and the
renamed command help were checked; benchmark task discovery and configuration
initialization were exercised using an isolated temporary home directory.
The release scan found no matches for the checked common credential patterns or
the original internal endpoint/path patterns; this is not a comprehensive secret
audit. No live agent, container build, model request, GitLab deployment, or Slurm
allocation was run as part of these checks.
