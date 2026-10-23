# API reference

`abc-mine` is primarily a CLI, but every module is importable for embedding or
testing. The layering:

```text
cli                          argument parsing, reporting, drift enforcement
  stages/{screen,rank,build}  the three workflow stages
    sandbox                   staging and running one agent in a container
    parallel                  serial / local-process / MPI backends
    prompts                   templates, slot packs, gated overrides
    rubric                    axes, weights, gates, deterministic scoring
    repo                      commit selection, worktrees, bundle-cutoff
    preflight                 proving the toolchain works, measuring cost
    manifest                  run provenance, config lock, drift detection
    config                    the run configuration schema
```

Nothing below `stages/` depends on anything above it.

## Where to look first

| If you want to | Read |
|---|---|
| Understand the config schema | [`config`](abc_mine.config.md) |
| Change how commits are scored | [`rubric`](abc_mine.rubric.md) |
| Understand prompt composition | [`prompts`](abc_mine.prompts.md) |
| Understand starting-state construction | [`repo`](abc_mine.repo.md) — `build_cutoff_workspace` |
| Understand verifier polarity checking | [`stages.build`](abc_mine.stages.build.md) |
| Understand resume and drift | [`manifest`](abc_mine.manifest.md) |
| Add a parallel backend | [`parallel`](abc_mine.parallel.md) |

## Notable entry points

- [`rubric.score`](abc_mine.rubric.md) — the deterministic scorer. The model supplies axis
  scores; this computes the total and the gate outcome.
- [`repo.build_cutoff_workspace`](abc_mine.repo.md) — builds a benchmark's starting state and
  asserts it leaks no post-cutoff objects. The correctness keystone.
- [`stages.build.decide_verifier_result`](abc_mine.stages.build.md) — the PASS/FAIL contract,
  deliberately identical to abc-bench's own logic.
- [`manifest.enforce`](abc_mine.manifest.md) — refuses to continue a run whose settings would
  make results incomparable.
- [`prompts.load_pack`](abc_mine.prompts.md) — slot loading and the override gate.
