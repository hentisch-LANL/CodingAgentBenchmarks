"""The three workflow stages.

- :mod:`abc_mine.stages.screen` — score every selected commit against the
  rubric, one fresh agent per commit, in parallel.
- :mod:`abc_mine.stages.rank` — collapse the viable summaries into a ranked
  shortlist using one decision-making agent.
- :mod:`abc_mine.stages.build` — turn each shortlisted commit into a
  complete abc-bench task, in parallel, with verifier polarity enforced.

Each stage is independently resumable from its own durable on-disk artifacts, and
each is invoked through the ``abc-mine`` CLI.
"""

from __future__ import annotations

__all__ = ["screen", "rank", "build"]
