# CLI reference

The `abc-ci-launcher` distribution installs the `abc-ci` command.

```bash
abc-ci  # Creates a starter config on first use and displays help.
abc-ci doctor [--config PATH] [--no-token]
abc-ci trigger --task group/task --agent example-agent
abc-ci trigger-group --task-group group --agent 'example-*'
```

Trigger options:

- `--config PATH`: launcher config (default `~/.abc-bench/abc-ci/config.yaml`).
- `--abc-bench-config PATH`: local benchmark config for agent validation.
- `--agent ID_OR_GLOB`: defaults to the launcher config's agent; a glob creates
  one pipeline per matching local agent.
- `--timeout-seconds N`: per-run timeout override.
- `--attempts N`: positive attempt limit per task, default one.
- `--nodes N`: positive count of site-allocated Slurm nodes, default one;
  multi-node runs use one MPI rank per node. The site must grant the allocation.
- `--run-group LABEL`, `--run-label LABEL`: provenance annotations.
- `--dev`: target the configured development branch and separate results archive.
- `--force`: skip validation confirmations, not server authorization.
- `--dry-run`: display the request without sending it.
- `--key-envs NAME [NAME ...]`: forward named local environment variables.

Forwarded keys become GitLab trigger variables, which are not automatically
masked. Launcher dry-run redacts forwarded keys; use dummy credentials for
validation anyway. Prefer
site-managed protected/masked secrets and restrict project access.

Run `abc-ci trigger --help` or `abc-ci trigger-group --help` for current syntax.
