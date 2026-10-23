# abc-ci-launcher

The `abc-ci-launcher` distribution provides the `abc-ci` command and
`abc_ci_launcher` Python package. It submits task and task-group requests to a
site-configured GitLab pipeline; it does not run the benchmark itself.

From `abc-ci/`:

```bash
pip install ./abc-ci-launcher
abc-ci
abc-ci doctor --no-token
```

Install sibling `../automaton` and `../abc-bench` locally to resolve task refs
and validate agent IDs. Edit `~/.abc-bench/abc-ci/config.yaml`:

```yaml
gitlab:
  base_url: https://gitlab.example.org
  project_id: "12345"  # Replace with your project's ID.
  ref: main
  dev_ref: dev
  trigger_token_env: ABC_CI_TRIGGER_TOKEN
defaults:
  agent: example-agent
  timeout_seconds: null
```

Export the trigger token locally; do not store it in this file. Then run:

```bash
abc-ci doctor
abc-ci trigger --task smoke_test/hello-world-python --agent example-agent
abc-ci trigger-group --task-group smoke_test --agent 'example-*' --attempts 2
```

`--agent` accepts a shell-style glob and starts a separate pipeline for each
matched agent. Short task IDs are expanded to canonical `group/task` refs when
unambiguous. Use `--abc-bench-config PATH` to select a local validation config;
`--config PATH` selects the launcher config. The runner has its own
`ABC_BENCH_CONFIG` file, which must define matching agent IDs.

Options include `--timeout-seconds`, `--run-group`, `--run-label`, `--attempts`,
`--nodes`, `--dev`, `--force` (skip confirmation), `--dry-run`, and
`--key-envs MODEL_API_KEY`. `--nodes` must match a site-granted allocation;
the launcher cannot allocate resources itself. See `abc-ci trigger --help`.

`--key-envs` forwards named local environment variables as GitLab trigger
variables. They are not automatically masked and may be visible in pipeline
metadata. Prefer site-managed secrets where possible. Dry-run redacts the trigger
token and forwarded key values; use dummy credentials for validation anyway.

See [the repository README](../README.md) and
[deployment guide](../DEPLOYMENT.md) for pipeline setup, authentication,
runner prerequisites, result retention and Pages publication.
