# abc-ci

Portable CI orchestration and reporting for **abc-bench**. This directory owns
GitLab trigger requests, job setup, results handling and static reports; the
sibling `../abc-bench` owns benchmark execution and `../automaton` owns agent
execution. There are no submodules or bundled site-specific pipeline files.

## Components

- `abc-ci-launcher/`: distribution `abc-ci-launcher`, Python package
  `abc_ci_launcher`, command `abc-ci`; validates local task/agent selections and
  submits GitLab trigger requests.
- `ci/run_abc_bench.sh`: validates inputs, installs sibling projects editable,
  builds a runtime container and runs one task or a task-group batch, optionally
  across an existing Slurm allocation via MPI.
- Other `ci/` scripts resolve production/dev archives, create runtime config,
  remove current-job inconclusive outcomes and collect compact job artifacts.
- `site/build.py`: aggregates archived results into leaderboard, model, harness
  and run pages. `ci/render_pages.sh` builds `public/` for GitLab Pages.
- `docs/`, `mkdocs.yml`, `doc_scripts/`: optional MkDocs documentation.

## Local launcher

Use Python 3.11+ for the full CI workflow (the launcher alone supports 3.10+).
From this directory, in an isolated environment:

```bash
python -m pip install -e ../automaton -e ../abc-bench
python -m pip install ./abc-ci-launcher
abc-ci
abc-ci doctor --no-token
```

Edit `~/.abc-bench/abc-ci/config.yaml` with your GitLab URL, project ID, ref and
token environment name. Set `ABC_CI_TRIGGER_TOKEN` in your shell, not in the file.
Configure matching local/runner agent IDs and installed tasks, then:

```bash
abc-ci trigger --task smoke_test/hello-world-python --agent example-agent
abc-ci trigger-group --task-group smoke_test --agent example-agent --attempts 2
```

Use `--abc-bench-config PATH` for local validation against a different benchmark
config; it does not upload that config. `--dev` targets `gitlab.dev_ref` and the
separate dev archive. `--key-envs MODEL_API_KEY` can forward a local provider key,
but GitLab trigger variables are not automatically masked. Launcher dry-run
redacts forwarded keys; use dummy keys for validation anyway.
See [DEPLOYMENT.md](DEPLOYMENT.md) for security.

## Create a site pipeline

Create your own `.gitlab-ci.yml` in your deployment project. Supply runner tags,
Python environment/dependencies, Charliecloud and harness prerequisites, sibling
sources, authentication, and writable shared results storage. Configure the
runner's scheduler integration to grant an allocation before the benchmark job;
setting `NODES` alone does not allocate nodes. Account, partition, QoS, resources,
time limits and MPI modules are site choices. Do not copy machine paths or secrets
into the pipeline. A **schematic** GitLab job outline is:

```yaml
stages: [benchmark, report]
variables:
  NODES: "1"
  ATTEMPTS: "1"
benchmark:
  stage: benchmark
  tags: ["$BENCHMARK_RUNNER_TAG"]
  rules:
    - if: '$CI_PIPELINE_SOURCE == "trigger" && $AGENT && ($TASK || $TASK_GROUP)'
  # Site-owned before_script: prepare Python/tools, sibling sources and allocation.
  script:
    - bash ci/run_abc_bench.sh
  artifacts:
    when: always
    paths: [api_artifacts/]
pages:
  stage: report
  tags: ["$REPORT_RUNNER_TAG"]
  when: always
  script:
    - bash ci/render_pages.sh
  artifacts:
    paths: [public/]
```

This is not a ready-to-run scheduler configuration. It assumes the working
directory is `abc-ci/`; adjust `cd` and artifact paths for a parent monorepo.
Set `ABC_BENCH_CONFIG`, `ABC_BENCH_RESULTS_DIR`, and (for dev)
`ABC_BENCH_RESULTS_DIR_DEV` in site CI configuration. The reporting runner must
read the selected archive. Add branch/source rules and Pages deployment syntax
appropriate to your GitLab version so only authorized pipelines publish.
Provision a Python environment with pip and PyYAML; benchmark installs pull their
declared Python dependencies. Multi-node execution additionally requires MPI,
mpi4py and `srun`; container builds require Charliecloud and supporting build
tools. Never use untrusted pipeline code with protected model credentials.

The report runner also needs NumPy, SciPy, and Pillow:
`python -m pip install numpy scipy Pillow`. Install these separately from the
launcher, whose dependencies do not include report-rendering libraries.

Use masked/protected CI variables or a secrets manager for provider credentials,
and restricted GitLab trigger tokens for launcher authentication. Configure proxy,
CA certificates and registry credentials without embedding endpoints or keys in
public source. Review archive/artifact contents for sensitive material, set
retention and access permissions, and publish only approved data through Pages.
See [DEPLOYMENT.md](DEPLOYMENT.md) for the variable contract, scheduler guidance,
dev isolation and validation checklist.

## Reports and tests

For local reports, set `ABC_BENCH_ARCHIVE_DIR` and run `python site/build.py`, or
use `bash site/serve_local.sh /path/to/approved/archive`. Catalog metadata is
documented in [site/CATALOGS.md](site/CATALOGS.md).

```bash
PYTHONPATH=abc-ci-launcher/src python -m pytest abc-ci-launcher/tests tests
```
