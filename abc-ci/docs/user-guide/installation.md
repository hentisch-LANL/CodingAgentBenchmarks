# Installation

Use a Python 3.11+ environment for the full CI workflow. The launcher alone
supports Python 3.10+. From `abc-ci/`, with the sibling sources available:

```bash
python -m pip install -e ../automaton -e ../abc-bench
python -m pip install ./abc-ci-launcher
abc-ci
abc-ci doctor --no-token
```

Edit `~/.abc-bench/abc-ci/config.yaml` to set your GitLab base URL, project ID,
ref and trigger token environment name. Export `ABC_CI_TRIGGER_TOKEN` locally;
do not put it in the config. Install the intended tasks and configure matching
local and runner agent IDs before triggering.

```bash
abc-ci doctor
abc-ci trigger --task smoke_test/hello-world-python --agent example-agent
```

`--abc-bench-config PATH` selects a local benchmark validation config;
`--config PATH` selects the launcher config. Neither uploads a benchmark config
to the runner. See the repository deployment guide for runner setup.
