"""Pytest configuration: ensure the automaton package is importable.

The automaton repository has no installed package entry at collection time in
all environments, so insert the repo root onto ``sys.path``. This mirrors the
``sys.path.insert`` pattern used by the ported abc_bench tests but centralizes
it here so individual test modules stay clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
