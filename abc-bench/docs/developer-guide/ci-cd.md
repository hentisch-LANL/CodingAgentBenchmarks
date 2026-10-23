# CI and Documentation Checks

ABC Bench is consumed from this monorepo, not a package registry. The release
checkout intentionally contains no organization-specific CI configuration.
Configure your CI runner to install the sibling engine first:

```bash
cd abc-bench
pip install -e ../automaton
pip install -e '.[dev,docs]'
python -m pytest tests
mkdocs build --strict
```

Without installing the local components, run tests from `abc-bench` with:

```bash
PYTHONPATH=../automaton:. python -m pytest tests
```

`docs/scripts/setup-docs-env.sh` checks for the sibling `../automaton` component
before creating its conda environment and installs it for API imports. It does
not fetch repositories or initialize submodules. Documentation uses the Python
package `abc_bench`; the public command is `abc-bench`.

Runtime assets (`harnesses/`, `containers/`, and bundled `tasks/smoke_test/`)
are resolved from the component checkout. `ABC_BENCH_PACKAGE_ROOT` can override
the runtime asset root. Other task groups are installed separately using
`abc-bench install <archive-or-directory>`; tests do not generate datasets or
build container images.

Use provider `cert` settings or standard CA-bundle environment variables for
private endpoints. No private credentials, internal service URLs, or custom CA
files are assumed by the release checkout.
