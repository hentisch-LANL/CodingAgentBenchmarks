# Configuration reference

One YAML file per run. Relative paths resolve against the **config file's
directory**, so a config plus its prompt pack and container form a relocatable
bundle.

The model is **never** configured here — see [Environment](#environment).

---

## `run`

```yaml
run:
  id: my-codebase-v1        # required; lowercase slug; namespaces all outputs
  data_root: data           # default: data
```

`id` must be a slug (lowercase alphanumerics and hyphens) because it becomes a
directory name. Outputs land in `<data_root>/<id>/`.

A new `id` is the clean way to start over: each run's outputs stay internally
consistent.

---

## `codebase`

```yaml
codebase:
  name: MyCode                    # required; used in prompts
  repo: ./my-code-src             # required; a COMPLETE git clone
  source_mount: /workspace/src    # in-container path; default /workspace/src
  group_id: mycode                # abc-bench group id; default: slug of name
  group_description: ...          # written into the emitted group.yaml
```

`repo` must not be shallow — starting-state construction needs full history. This is
checked up front rather than failing later inside the build stage.

`source_mount` is where agents and toolchain commands find the code. It must be
absolute, and must not be a path your image already occupies.

---

## `container`

```yaml
container:
  image: ./containers/mycode-dev.sqfs   # required
  writable: true                        # --write-fake overlay; default true
```

A Charliecloud SquashFS file or unpacked image directory, passed verbatim to
`ch-run`. Not a Docker/Podman/Apptainer image.

Keep `writable: true` for any codebase that compiles in-image.

---

## `toolchain`

How to build and test **inside the container**. The most important section in the
file.

```yaml
toolchain:
  configure: cmake -S {source} -B {build} -G Ninja
  build:     cmake --build {build} --parallel
  test:      ctest --test-dir {build}
  test_one:  ctest --test-dir {build} -R '{selector}'

  build_dir:   /tmp/mycode-build
  install_dir: /tmp/mycode-install

  env:
    CC: gcc-11
    MYCODE_SOURCE_DIR: "{source}"

  preflight_selector: a_real_test_name
  full_build_seconds: 243.0        # written by `preflight --write-config`
  targeted_test_seconds: 18.5
```

Each command is a shell string run with `bash -lc`, so pipes, quoting, and `&&` work.

**Placeholders:** `{source}`, `{build}`, `{install}`, and — in `test_one` only —
`{selector}`. An unknown placeholder is an error rather than a literal `{foo}` on a
command line.

### `test_one` is the one that matters

`test_one` is required to contain `{selector}`. Targeted verification is what makes
emitted benchmarks affordable to grade, and the rubric's `verify_cost` axis rewards
commits checkable this way. A toolchain that can only run everything will produce
expensive benchmarks.

`preflight_selector` must match at least one real test at HEAD, so `preflight` can
prove `test_one` actually selects something. A selector that silently matches nothing
is worse than one that errors — verifiers built on it would pass vacuously.

### Measured timings

`full_build_seconds` and `targeted_test_seconds` should be written by
`preflight --write-config`, not by hand. They are rendered identically into every
stage's prompt. When absent, the prompts say so explicitly rather than inventing a
figure — see [`limitations.md`](limitations.md) and the README on why a guessed
constant is a bug.

### Scratch paths

`build_dir` / `install_dir` must be **outside** `source_mount` and outside
`/workspace`: at grading time abc-bench bind-mounts the task's code tree onto
`/workspace`, hiding anything beneath it. `/tmp/...` is the safe choice.

---

## `selection`

Which commits to screen. Mechanical filters run **before** the mode, so
`recent: 100` means 100 *usable* commits.

```yaml
selection:
  mode: all                 # all | recent | sample | range
  count: 200                # for recent/sample
  stride: 10                # for sample (else derived from count)
  range: [abc1234, def5678] # for range, inclusive
  paths: ["src/physics/**"] # only commits touching these
  exclude_merges: true      # default true
  max_files_changed: 40
  max_diff_lines: 2000
```

| Mode | Meaning |
|---|---|
| `all` | Every usable commit |
| `recent` | The newest `count` |
| `sample` | Evenly spaced across history — good for validation slices |
| `range` | Inclusive span between two SHAs |

Filtering in Python is free; paying an agent to score a sprawling commit 0 is not.
The selection report always states what each filter removed, so over-filtering is
visible. Commits without a parent (the root) are always dropped — a starting state is
defined as "the repository at the parent commit".

---

## `prompts`

```yaml
prompts:
  pack: ./prompts                    # optional slot/override pack
  allow_template_overrides: false    # advanced; default false
```

See [`prompt-customization.md`](prompt-customization.md). Both keys are optional;
with no pack the generic prompts are used unchanged.

---

## `execution`

```yaml
execution:
  parallel: serial          # serial | local | mpi
  workers: 1                # for local
  max_attempts: 2           # attempts per item before skipping it
  timeout_seconds: 3600     # per agent; null for none
  scratch_dir: /var/tmp     # node-local, NOT shared storage
  keep_logs: failed         # none | failed | all
  sandboxed: true           # loopback model proxy + network isolation
```

`serial` is the default and needs no MPI. `local` forks worker processes on one node.
`mpi` requires the `[mpi]` extra and a launcher; if unusable it degrades to serial
with a warning rather than crashing.

`max_attempts` bounds retries across resumes. `keep_logs: failed` keeps what you need
to diagnose failures without hoarding successful transcripts.

---

## `rank`

```yaml
rank:
  method: single_context         # or: interleaving
  max_prompt_tokens: 300000      # refuse if a composed prompt exceeds this (endpoint count)
  max_candidates: null           # hard cap on candidates (after sorting)
  interleaving:                  # used only by method: interleaving
    groups: 2                    # random groups, each ranked by its own agent
    seed: 0                      # fixes the split and the within-round order
```

Every ranking agent sees all of its candidates embedded in one prompt.
`max_prompt_tokens` enforces that rather than letting a context silently overflow.
The whole prompt the model receives — the harness system prompt, the instructions,
and the candidates — is counted through the endpoint's `/v1/messages/count_tokens`
(so `LLM_API=anthropic_messages` is required whenever a budget is set). There is no
estimate fallback: if the count fails, ranking does not start. Exceeding the budget
is a hard error with an actionable menu — see
[`limitations.md`](limitations.md#1-ranking-is-single-context-only).

The check is only as good as the endpoint's counter. Some endpoints report fewer
tokens than the model is charged for the same text. Calibrate your endpoint and
leave a conservative margin rather than assuming the count matches billed usage.

Set it **below** the model's hard context limit. The remainder has to hold the
agent harness's tool definitions, the agent's reasoning and tool calls, and the
shortlist and rationale it writes. `null` disables the check (and the count).

`max_prompt_tokens` replaced `max_candidate_tokens`, which bounded a character-based
estimate of the candidates alone. The old key is rejected rather than reinterpreted.

### `method: interleaving`

For pools too large for one context. The candidates are split uniformly at random
into `groups` groups of near-equal size; each group is ranked by its own
single-context agent (same prompt, same validation), told truthfully that it holds a
random sample; and the per-group shortlists are merged by **interleaving** — one pick
from each group per round, with the within-round group order shuffled so no group is
systematically first.

The assumption: each group is a random sample of the same pool, so a group's k-th
pick is as strong as any other group's k-th pick. No candidate is ever compared with
one outside its group. The assumption weakens as groups shrink, and when the few
best candidates happen to land together (the runner-up of a strong group then ranks
alongside the best of a weaker one).

Groups are independent, so under `execution.parallel: mpi` (e.g. `srun -n 2
abc-mine rank --parallel mpi ...`) they run concurrently: group `i` goes to MPI
rank `i % world_size` (static round-robin), each rank ranks its share in turn, and
rank 0 merges once all ranks finish. Rank 0 alone plans and writes the split and the
final shortlist. Any other backend ranks the groups serially in one process. A
failed group does not stop the others; each group is saved as it finishes
(`rank/interleaving/group-N/`), so a rerun ranks only the missing groups. The run
directory must be on a filesystem every rank can see. The split is recorded in
`rank/interleaving/split.json`; changing `groups`, `seed`, or the candidate set makes
a resume refuse, and `--force` sets the recorded state aside. Each merged shortlist
entry carries `group` and `group_rank` alongside its overall `rank`.

---

## `rubric`

Optional overrides to the scoring axes. See [`rubric.md`](rubric.md).

```yaml
rubric:
  axes:
    verifiability: {weight: 0.35}
    verify_cost:   {weight: 0.05}
  task_types: [bug_fix, feature_add, refactor]
```

Weights must sum to 1.0. Unknown axis keys are an error.

---

## Environment

Never in the config file, so a config is safe to commit and no credential is ever
serialized into a manifest.

| Variable | Required | Meaning |
|---|---|---|
| `LLM_BASE_URL` | yes | OpenAI-compatible endpoint |
| `LLM_MODEL` | yes | Model id sent to the endpoint |
| `LLM_API_KEY` | yes | Credential; never recorded |
| `LLM_API` | no | `chat_completions` (default), `responses`, or `anthropic_messages` |
| `LBM_SCRATCH_DIR` | no | Overrides `execution.scratch_dir` |

---

## CLI overrides

Common execution settings have flags: `--parallel`, `--workers`, `--max-attempts`,
`--timeout`, `--scratch-dir`, `--keep-logs`, `--run-id`, `--data-root`.

They win over the file but are folded into the same fingerprint the manifest locks, so
an override is visible to drift detection rather than being an invisible
per-invocation difference.

---

## Minimal example

```yaml
run:
  id: mycode-v1

codebase:
  name: MyCode
  repo: ./my-code-src

container:
  image: ./containers/mycode-dev.sqfs

toolchain:
  build:    cmake --build {build} --parallel
  test_one: ctest --test-dir {build} -R '{selector}'
  configure: cmake -S {source} -B {build}
  preflight_selector: smoke
```

Validate it without running anything:

```bash
abc-mine init      --config mine.yaml
abc-mine preflight --config mine.yaml --dry-run
abc-mine screen    --config mine.yaml --plan
```
