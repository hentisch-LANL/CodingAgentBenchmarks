# CodingAgentBenchmarks
A "monorepo" presenting several tools to assist in the development of coding agent benchmarks and their sandboxed execution on HPC platforms (e.g., in userspace). 

The authors emphasize that the primary motivation of this open-source release is to accelerate internal development (e.g., by not having to treat the code as potentially sensitive); that is to say that this repository is not necessarily intended as a public facing utility, and this code is released publicly primarily in compliance with regulations mandating that we do so. That being said, we are of course happy to see interest in this work and can potentially offer support if you run into issues.

This code has been approved for release by the Feynman-Center and assigned O#5231.

# Authors
This code was primarily developed by Henry Tischler (hentisch@lanl.gov), who is the primary point-of-contact for support or questions. 

> [!NOTE]
> All following text, aside from the final copyright notices, is LLM generated. 

## Components

This repository contains four cooperating tools as ordinary source directories,
not Git submodules:

| Directory | Purpose | Python import / command |
| --- | --- | --- |
| [`automaton/`](automaton/README.md) | Reusable sandbox and model-proxy engine for autonomous coding agents | `automaton` (Python API) |
| [`abc-bench/`](abc-bench/README.md) | Task discovery, container preparation, benchmark execution, and verification | `abc_bench` / `abc-bench` |
| [`abc-ci/`](abc-ci/README.md) | GitLab launch tooling, CI orchestration, and static result reporting | `abc_ci_launcher` / `abc-ci` |
| [`abc-mine/`](abc-mine/README.md) | Mine a repository's history, screen and rank candidate changes, and build benchmark tasks | `abc_mine` / `abc-mine` |

Automaton is content-independent: callers supply harnesses, container templates,
and tasks. ABC Bench supplies those inputs and verifies the resulting agent
deliverables. ABC CI automates benchmark runs and reporting. ABC Mine uses
Automaton to turn selected changes from another repository into benchmark tasks.

## Installation

Use Python 3.10 or newer (3.11+ for the full CI runner workflow). From the
repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ./automaton -e ./abc-bench -e ./abc-mine
python -m pip install -e ./abc-ci/abc-ci-launcher

abc-bench --help
abc-mine --help
abc-ci --help
```

Install from this checkout: ABC Bench locates its harnesses, container templates,
and task files in its source directory. Installing only a wheel does not supply
that complete runtime layout. Automaton is installed separately from its sibling
directory; there is no private submodule to fetch.

For Python API use, start the interpreter **outside the monorepo root**. With a
modern editable install, the top-level `automaton/` component directory can
otherwise shadow the installed `automaton` package as a namespace package.
On Python 3.11+, `python -P` (or `PYTHONSAFEPATH=1`) also avoids this issue; the
CI runner sets this explicitly. The installed CLI commands do not need a change.

Container execution additionally requires [Charliecloud](https://hpc.github.io/charliecloud/)
(`ch-image`, `ch-convert`, and `ch-run`) and the host facilities it requires.
Building images may require network access. A Python-only installation is enough
to inspect the CLIs, but not to run containerized agents. Optional multi-process
execution requires an MPI runtime and the relevant `[mpi]` extras; serial
execution does not require MPI.

## Running benchmarks and mining tasks

1. Follow the [benchmark quickstart](abc-bench/docs/user-guide/quickstart.md)
   to configure providers, models, agents, and local data/results directories.
   The bundled smoke-test tasks demonstrate the task format.
2. Supply API credentials through the environment variables named in your
   provider configuration. Replace example endpoints and model identifiers with
   real values. For a private certificate authority, configure your own CA bundle;
   no site-specific certificate is distributed here.
3. Prepare the required containers before running tasks. Agent harnesses and
   upstream API styles must agree; consult the
   [harness interface](abc-bench/docs/reference/harness-interface.md).
4. For mining, provide a complete Git clone of the target codebase and a
   Charliecloud environment that can build and test it. Follow the
   [mining guide](abc-mine/README.md) for the screen, rank, and build stages.

The sandbox and loopback model proxy reduce exposure during agent execution;
they are not a substitute for reviewing tasks, containers, verification code,
and host permissions. Run untrusted code only on appropriately isolated resources.

## CI deployment

The public release intentionally omits the original site-specific GitLab YAML,
Darwin submission scripts, internal endpoints, and site CA certificate. The CI
implementation is included, but it is not a ready-configured hosted service.

Use the [CI README](abc-ci/README.md) and
[deployment guide](abc-ci/DEPLOYMENT.md) to create your own pipeline. Configure
runner tags, scheduler resources, software environments, credentials, persistent
result storage, and Pages publishing for your site. Keep tokens in protected CI
variables or another secret store, not in committed YAML.

## Tests and documentation

Install test dependencies and run each component separately, so unrelated test
modules with identical names do not collide:

```bash
python -m pip install pytest requests tqdm numpy scipy Pillow
python -m pytest automaton/tests
python -m pytest abc-bench/tests
python -m pytest abc-mine/tests
python -m pytest abc-ci/abc-ci-launcher/tests
python -m pytest abc-ci/tests
```

Some integration paths require external tools or allocations. Passing the unit
tests alone does not demonstrate a working container or cluster deployment.
Each component has its own `docs/` and `mkdocs.yml`; install its documentation
dependencies and build from that component directory. ABC CI's benchmark-report
renderer in `site/` is source code, not a generated documentation directory.

# Copyright
Copyright 2026

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

© 2026. Triad National Security, LLC. All rights reserved.
This program was produced under U.S. Government contract 89233218CNA000001 for Los Alamos
National Laboratory (LANL), which is operated by Triad National Security, LLC for the U.S.
Department of Energy/National Nuclear Security Administration. All rights in the program are
reserved by Triad National Security, LLC, and the U.S. Department of Energy/National Nuclear
Security Administration. The Government is granted for itself and others acting on its behalf a
nonexclusive, paid-up, irrevocable worldwide license in this material to reproduce, prepare
derivative works, distribute copies to the public, perform publicly and display publicly, and to permit
others to do so.
