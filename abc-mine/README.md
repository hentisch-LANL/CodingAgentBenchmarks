# abc-mine

Mine the git history of an **arbitrary codebase** for agentic-coding benchmarks
and emit ready-to-run [ABC Bench](../abc-bench) tasks.

Given a git repository and a container that can build and test it, three stages
funnel commit history down to finished benchmarks:

```text
screen   every selected commit   ->  per-commit rubric summary      (parallel)
rank     all viable summaries    ->  ranked shortlist               (one agent)
build    each shortlisted commit ->  a complete abc-bench task      (parallel)
```

Each stage increases scrutiny while reducing volume. Every agent runs inside your
container, so it reads the real source tree and — when warranted — compiles and
tests the code.

This generalizes a workflow originally built for the Truchas multiphysics code;
see [`docs/guide/limitations.md`](docs/guide/limitations.md) for what it deliberately does
*not* do.

## Requirements

- Python 3.10+
- [`automaton`](../automaton) — the sandboxed
  agent-execution engine
- **Charliecloud** (`ch-run`). Containers are Charliecloud SquashFS images or
  unpacked image directories; Docker/Podman/Apptainer images are not
  interchangeable.
- A complete (non-shallow) git clone of the codebase to mine
- An OpenAI-compatible model endpoint

The **container**, not just the host Python environment, must also provide:

- Bash, Python 3 with PyYAML (`import yaml`), Git, and coreutils (`timeout`).
- An OpenCode executable at `/usr/local/bin/opencode`, plus the target codebase's
  build and test dependencies. A differently located executable needs a symlink
  at that path or a customized harness.

The bundled harness uses `opencode run` with `--dir`, `--model`, `--title`,
`--format default`, and `--dangerously-skip-permissions`, and passes the prompt
on standard input. No OpenCode version is pinned by this snapshot: verify that
your selected version supports these options using `opencode run --help` inside
the image, and pin the version in your own container recipe. Toolchain preflight
does not validate this agent invocation. See the sibling
[OpenCode container template](../abc-bench/containers/harnesses/opencode.Dockerfile.jinja2)
for an installation example, and install Python/PyYAML alongside it as needed.
Permission bypass is intended only inside the isolated benchmark container.

MPI is **optional**. The default backend is serial and needs nothing beyond the
standard library.

## Install

```bash
pip install -e ../automaton      # the agent engine
pip install -e ../abc-bench      # run and grade exported tasks
pip install -e .                 # abc-mine
pip install -e '.[mpi]'          # optional: multi-node fan-out
```

## Configure

Two things are required: a repository and a container. A third — how to build and
test inside that container — is what makes the results worth anything.

```yaml
run:
  id: my-codebase-v1
  data_root: data

codebase:
  name: MyCode
  repo: ./my-code-src
  source_mount: /workspace/src

container:
  image: ./containers/mycode-dev.sqfs

toolchain:                                  # commands run INSIDE the container
  configure: cmake -S {source} -B {build} -G Ninja
  build:     cmake --build {build} --parallel
  test:      ctest --test-dir {build}
  test_one:  ctest --test-dir {build} -R '{selector}'
  preflight_selector: some_real_test_name

selection:
  mode: all
  exclude_merges: true
  max_files_changed: 40
```

The model comes from the environment, never the config file, so a config is safe
to commit:

```bash
export LLM_BASE_URL=https://your-endpoint/v1
export LLM_MODEL=your-model-id
export LLM_API_KEY=...
export LLM_API=chat_completions      # or: responses, anthropic_messages
```

### Why the toolchain is structured data

Those commands are not documentation. Three consumers need them, and two are not
agents at all:

1. screening and ranking agents, which are *told* them in their prompts;
2. `preflight`, which *executes* them to prove they work and to measure real cost;
3. the emitted task verifiers, which must call something that still exists at
   grading time.

Prose could serve (1). Only structured data serves (2) and (3).

## Run

```bash
abc-mine init      --config mine.yaml
abc-mine preflight --config mine.yaml --write-config
abc-mine screen    --config mine.yaml --parallel local --workers 8
abc-mine rank      --config mine.yaml
abc-mine build     --config mine.yaml --parallel local --workers 4
abc-mine export    --config mine.yaml --dest ../abc-bench
```

`abc-mine status --config mine.yaml` reports where a run stands at any
point.

### Start with preflight

`preflight` runs your configure/build/test commands in the real container against
the real repository, proves a *targeted* test selection works, and times
everything.

Do not skip it. The entire workflow rests on a precondition that "a repo and a
container" does not capture: **the container must be able to verify a change
deterministically.** If it cannot, the workflow still runs — it just produces
garbage, and you find out after paying for hundreds of agent invocations.

`--write-config` records the measured timings, after which every stage quotes the
same measured figure instead of guessing. The predecessor pipeline hard-coded "a
full build takes ~30+ minutes" into its screening prompt; the real figure was
about four minutes, discovered late, and worked around by adding a paragraph to a
*later* prompt explaining that the earlier stage's cost scores were systematically
wrong. A measurement costs one command and avoids that entirely.

## Parallelism

| Backend | Flag | Needs | Use when |
|---|---|---|---|
| Serial | *(default)* | nothing | debugging, small runs, laptops |
| Local pool | `--parallel local --workers N` | nothing | one workstation or node |
| MPI | `--parallel mpi` | `[mpi]` extra + launcher | multi-node Slurm |

Screen and build parallelize per commit. Rank is a single agent by design.

If MPI is requested but unusable — not installed, its runtime not loaded, or only
one rank — the run degrades to serial with an explanatory warning rather than
crashing.

## Checkpointing and resume

Every stage resumes by re-reading its own durable artifacts, so an interrupted run
continues where it stopped:

- **screen** — one summary file per commit; commits with one are skipped.
- **build** — an append-only manifest; tasks recorded `ok` are skipped, and
  partial task directories are removed and rebuilt.
- **rank** — single-shot; it either finished or it did not.

Because progress lives in readable files rather than a backend's bookkeeping, you
can start serially and resume under MPI.

Failures are recorded in `attempts.jsonl` with reasons, bounded by
`execution.max_attempts`, and (by default) their agent transcripts are kept.

### Configuration is locked at `init`

A sweep may span days and several allocations. `init` records the resolved
configuration — rubric, selection, toolchain, prompts, model id — and every later
stage compares against it. A change that would make results **incomparable**
(rubric weights, prompt content, the model) is refused with a diff. Execution
details (workers, retention) only warn.

Override with `--allow-config-drift` when you intend a mixed run; the manifest
records what changed and when.

This is **provenance, not reproducibility.** Agents are nondeterministic and an
endpoint can be repointed behind a stable model id. Re-running will not reproduce
a run byte-for-byte; the manifest tells you what produced it.

## Customizing prompts

Shipped prompts are codebase-agnostic. Add per-codebase context with a **prompt
pack**:

```bash
abc-mine prompts scaffold ./prompts
abc-mine prompts slots              # what each slot is for
```

Fill in the slots you want, delete the rest, and point your config at the pack.
Each slot renders as a titled section that **disappears entirely when unset**, so
the prompt reads correctly either way.

Rewriting a whole template is also supported, but deliberately gated — see
[`docs/guide/prompt-customization.md`](docs/guide/prompt-customization.md).

Inspect the composed result at any time:

```bash
abc-mine prompts render --config mine.yaml screen
```

## What gets emitted

```text
data/<run-id>/
  run.json                      # manifest: config lock, provenance, history
  preflight.json
  screen/summaries/<sha>.json   # per-commit rubric verdicts
  rank/{shortlist.json,rationale.md}
  build/
    manifest.jsonl
    tasks/<group>/<task-id>/
      prompt.md                 # agent-authored task statement
      build_env.sh              # agent-authored setup
      workspace/                # PIPELINE-built starting state
      verification/verify_solution.sh
```

`workspace/` is built deterministically by git bundle-cutoff at the parent commit
— never by an agent. It carries genuine history up to that commit and **no later
objects**, so the reference solution cannot be recovered from it. Those properties
are asserted, not assumed.

Verifier polarity is enforced **twice**: once by the authoring agent, then again
by the pipeline on the shipped artifact in an environment mirroring the real
grader. A task is `ok` only when its verifier fails on the starting state and
passes on the change. `export` copies only verified tasks.

## Documentation

| Document | Contents |
|---|---|
| [`docs/guide/configuration.md`](docs/guide/configuration.md) | Every config key |
| [`docs/guide/prompt-customization.md`](docs/guide/prompt-customization.md) | Slots and overrides |
| [`docs/guide/rubric.md`](docs/guide/rubric.md) | Scoring axes, gates, re-weighting |
| [`docs/guide/checkpointing.md`](docs/guide/checkpointing.md) | Resume and drift detection |
| [`docs/guide/limitations.md`](docs/guide/limitations.md) | **What this does not do** |
| [`docs/guide/cli.md`](docs/guide/cli.md) | Full CLI reference |
| [`docs/guide/slurm.md`](docs/guide/slurm.md) | Site-independent Slurm submission |

## Tests

```bash
pytest tests -q
```

Covers the rubric scorer, prompt composition and the override gate, config
validation, git selection, the bundle-cutoff invariants (including the
solution-leak check), and the verifier PASS/FAIL contract. No containers, agents,
network, or credentials required.
