# abc-ci Documentation

**abc-ci** is the GitLab trigger, CI orchestration and reporting layer around
**abc-bench**. The `abc-ci` launcher submits requests; `ci/` prepares runtime
configuration and executes benchmarks; `site/` builds static reports from results.

Benchmark and agent execution come from sibling checkouts `../abc-bench` and
`../automaton`, respectively.

## Start here

- [Installation](user-guide/installation.md)
- [CLI reference](reference/cli.md)
- [GitLab contract](reference/gitlab-contract.md)
- [Python API](developer-guide/api-reference/index.md)

The repository `README.md` and `DEPLOYMENT.md` describe creating a site-specific
GitLab/Slurm pipeline, prerequisite tools, secrets handling and Pages publication.
