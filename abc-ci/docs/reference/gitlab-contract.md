# GitLab contract

No site pipeline is included. Supply runner tags, Python/tool prerequisites,
source provisioning, scheduler allocation and publication rules in your own
GitLab YAML. See the repository `README.md` and `DEPLOYMENT.md` for an outline.

## Required configuration

| Variable | Purpose |
| --- | --- |
| `ABC_BENCH_CONFIG` | Runner-visible benchmark YAML containing requested agents. |
| `ABC_BENCH_RESULTS_DIR` | Writable durable production archive root. |
| `ABC_BENCH_RESULTS_DIR_DEV` | Separate root required for dev mode. |

Optional: `ABC_BENCH_WORK_DIR`, `ABC_BENCH_DEV_REF` (default `dev`),
`TASK_GROUP_SOURCE`, and `TASK_PATH`. Provider secrets must match the benchmark
config's `api_key_env` names. The launcher's `ABC_CI_TRIGGER_TOKEN` is a client
credential, not a model provider key.

## Trigger variables

Exactly one of `TASK=group/task` or `TASK_GROUP=group` is required, plus `AGENT`.
Optional: `TIMEOUT_SECONDS`, `ATTEMPTS`, `NODES`, `RUN_GROUP`, `RUN_LABEL`, `DEV`.
`ATTEMPTS` and `NODES` must be positive integers; both default to one. Enforce
resource and target authorization in the site pipeline/runner, not just locally.

## Runtime

Run `bash ci/run_abc_bench.sh` from `abc-ci/` inside a site-granted allocation.
The script installs `../automaton` and `../abc-bench` editable, validates imports,
builds the selected runtime container, and invokes
`python3 -m abc_bench.cli run-tasks` with explicit config and results root.
`NODES > 1` installs `automaton[mpi]` and uses `srun` with one rank per node;
the site must provide compatible MPI and shared paths on all nodes. Single-node
runs execute directly.

Conclusive attempts are retained up to the attempt limit for resumability.
Current-job `HARNESS_ERROR` and `ENDPOINT_ERROR` results are removed as
inconclusive. `ci/collect_artifacts.py` writes compact artifacts in
`api_artifacts/` and warns about older inconclusive results. Preserve these as
GitLab artifacts even when benchmark execution fails.

`ci/resolve_results_dir.sh` selects the dev archive when `DEV` is truthy or
`CI_COMMIT_REF_NAME` equals `ABC_BENCH_DEV_REF`. Both execution and reporting
consume this selection. Keep development and production roots distinct.

## Reports

`bash ci/render_pages.sh` reads the selected archive and replaces `public/` with
static HTML. The reporting job needs Python and archive read access. Define
GitLab Pages publishing, branch isolation, access controls and retention in your
site YAML. Do not publish raw credentials, configs or sensitive benchmark data.
