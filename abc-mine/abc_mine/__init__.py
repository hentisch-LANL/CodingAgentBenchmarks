"""abc-mine — mine a git codebase for agentic-coding benchmarks.

A three-stage funnel that turns the commit history of an arbitrary git
repository into ready-to-run ABC Bench
tasks:

.. code-block:: text

    screen   every selected commit  ->  per-commit rubric summary   (parallel)
    rank     all viable summaries   ->  ranked shortlist            (one agent)
    build    each shortlisted commit ->  a complete abc-bench task  (parallel)

Every agent runs inside a caller-supplied Charliecloud container, so it can read
the real source tree and — when warranted — compile and test the codebase.

The public surface is the ``abc-mine`` CLI (see :mod:`abc_mine.cli`).
The modules below are importable for embedding or testing:

- :mod:`abc_mine.config`    — the run configuration schema
- :mod:`abc_mine.manifest`  — run manifest, config lock, drift detection
- :mod:`abc_mine.repo`      — commit enumeration/selection, worktrees,
  and the deterministic bundle-cutoff starting-state builder
- :mod:`abc_mine.rubric`    — the single source of truth for the scoring
  rubric (axes, weights, gates) and its deterministic scorer
- :mod:`abc_mine.prompts`   — prompt templates + per-codebase slot packs
- :mod:`abc_mine.preflight` — proves the container can build/test the repo
- :mod:`abc_mine.parallel`  — serial / local-process / MPI backends
- :mod:`abc_mine.stages`    — the three stage implementations
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
