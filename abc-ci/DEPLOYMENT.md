# Portable GitLab / Slurm deployment

Create your `.gitlab-ci.yml` using the outline in [README.md](README.md).
Review runner isolation and data publication policy before deployment.

## Sources and prerequisites

Make these source checkouts available on every allocated node:

```text
workspace/
  abc-ci/
  abc-bench/
  automaton/
```

These are sibling directories, not nested repositories or submodules. Pin and
record their revisions in your pipeline. In a monorepo checkout, change to
`abc-ci` before invoking scripts; for separate projects, securely provision the
two siblings before the job. Do not fetch untrusted revisions with production
credentials. The runner installs both Python projects editable to retain access
to benchmark assets. Use an isolated Python 3.11+ environment with pip, PyYAML,
and the dependencies declared by those projects. Install the launcher separately
on clients (`pip install ./abc-ci-launcher`).

Provide Bash, Git, Charliecloud build/run tools, the image-building and task
archive tools required by your chosen benchmark, and the harness runtimes.
Check namespace/container support, outbound model-provider access, certificates,
image registry access, disk quotas, and writable container caches. Module names,
virtual environment activation, container images and package mirrors are site
choices; none is assumed by these scripts.

## Runner and allocation

Choose GitLab runner tags that select your site's authorized execution runner.
Use its documented scheduler integration to request account, partition, QoS,
wall time, memory, CPUs and nodes. `SCHEDULER_PARAMETERS` is a convention of some
Slurm custom executors, not a universal GitLab setting. Validate how your runner
expands variables and grants allocations; a shell runner may instead need a
site-owned `sbatch --wait` wrapper.

Run `ci/run_abc_bench.sh` **inside an existing allocation**. `NODES` defaults to
one and does not request an allocation itself. For `NODES > 1`, allocate at least
that many nodes, provide compatible Slurm `srun` and MPI libraries on every node,
and make Python, sources, configs, tasks, containers and archive storage visible
to every rank. The script installs `automaton[mpi]` and launches one rank per node
with `--mpi`. Verify MPI/PMIx compatibility at the site. Single-node runs do not
use MPI.

## Variables

| Variable | Purpose |
| --- | --- |
| `ABC_BENCH_CONFIG` | Required readable benchmark YAML path on the runner; define the requested agent and provider `api_key_env` names. Do not embed secrets. |
| `ABC_BENCH_RESULTS_DIR` | Required writable durable results root for production. |
| `ABC_BENCH_RESULTS_DIR_DEV` | Required when `DEV` is truthy or the branch matches `ABC_BENCH_DEV_REF`; use a distinct root. |
| `ABC_BENCH_DEV_REF` | Development branch name, default `dev`. |
| `ABC_BENCH_WORK_DIR` | Optional job-local scratch; default `.abc-ci-work/<pipeline>/<job>`. |
| `TASK_GROUP_SOURCE` | Optional directory, `.sqfs` image or `.tar.gz` task group to install. |
| `TASK_PATH` | Optional benchmark task discovery path instead of installation. |
| `ABC_CI_ARTIFACTS_DIR` | Used by artifact collection; the runner sets it to `api_artifacts/` in this checkout. |

Trigger variables are exactly one of `TASK=group/task` or `TASK_GROUP=group`,
plus `AGENT`. Optional variables: `TIMEOUT_SECONDS`, `ATTEMPTS` (positive integer,
default one), `NODES` (positive integer, default one), `RUN_GROUP`, `RUN_LABEL`,
and `DEV`. Limit allowed targets, node counts and timeouts in your pipeline or
runner policy. Launcher validation is convenience, not an authorization boundary.

The runner creates a runtime YAML copy, builds the selected harness/group
container, and invokes `python3 -m abc_bench.cli run-tasks`. The explicit
`--results-dir` is the effective durable archive root, overriding the scratch
default in the runtime YAML. Conclusive attempts are retained for resumability;
current-job `HARNESS_ERROR` and `ENDPOINT_ERROR` results are cleaned so they can
be retried. Compact manifests and cleanup information go to `api_artifacts/`.
Inspect actual benchmark output layout rather than relying on a fixed run-id
directory convention.

## Authentication and secrets

Set the launcher's GitLab URL, project ID, branch and token environment name in
`~/.abc-bench/abc-ci/config.yaml`. Export `ABC_CI_TRIGGER_TOKEN` locally; never
commit it. Prefer scoped, rotated credentials and protected branches/runners.
Use masked/protected GitLab variables or an approved secrets manager for provider
keys when possible. `abc-ci --help` documents the launcher; `--key-envs` on a
trigger forwards named local environment variables to GitLab as trigger variables.
These are **not automatically masked** and may be visible to project members or
through pipeline metadata. GitLab variable precedence and protected-variable
availability depend on your configuration. Restrict trigger permissions.

The launcher dry-run redacts the trigger token and explicitly forwarded key
variables. Still use dummy credentials for validation and avoid logging real
requests. Disable shell tracing around secrets. Never put credentials in config files,
artifacts, model metadata, URLs or generated Pages. Review logs and raw archives
for prompts, source code and credentials before retaining or sharing them.

## Reporting and Pages

Run `bash ci/render_pages.sh` from `abc-ci/` with the results variables above.
It resolves production/development selection and generates `public/`. It replaces
the specified output directory, so use a dedicated build directory. The reporting
job needs Python with NumPy, SciPy, Pillow, and read access to the selected archive,
not Slurm or model
credentials. Keep that archive mounted or securely transfer approved results.
Optional docs can be built with MkDocs and published under `public/docs/`.

Define a GitLab Pages job and `public/` artifacts using your GitLab version's
syntax. Publish only on approved branches. Isolate development results and Pages
deployments (separate projects or supported `pages.path_prefix`); never let a
feature branch overwrite production. Apply Pages access controls and retention
policy. Site catalogs are presentation metadata, not model endpoint configuration;
see [site/CATALOGS.md](site/CATALOGS.md).

## Validation

Start with one bundled smoke-test task, one attempt and one node. Confirm input
validation, sibling-source import checks, container build, execution, archive
permissions, artifact collection and secret redaction. Then validate MPI if used,
failure/retry behavior, dev archive isolation and Pages access controls. A live
GitLab/Slurm deployment cannot be validated by the local unit tests alone.
