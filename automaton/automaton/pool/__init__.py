"""Automaton agent pool: coordinate embarrassingly-parallel agent jobs.

An :class:`AgentPool` runs a batch of caller-defined agent *jobs* in parallel
and gathers their outputs under a single checkpoint root, with crash-safe,
definitive per-job completion markers and no resuming of unfinished agents.

Automaton stays content-free about what a job does: the caller supplies a
``runner`` callable that performs the work (typically building a
:class:`~automaton.sandbox.Sandbox` run) and writes artifacts into each job's
``output_dir``.

The concrete :class:`MPIAgentPool` requires ``mpi4py`` (the ``automaton[mpi]``
extra). It is imported lazily here so that ``import automaton.pool`` works even
without mpi4py installed; accessing :class:`MPIAgentPool` in that case raises a
clear :class:`ImportError`.
"""

from __future__ import annotations

from typing import Any

from .base import AgentPool, CleanJob, DoneCheck, Runner
from .checkpoint import DONE_MARKER, MANIFEST_NAME
from .job import AgentJob, JobResult, JobStatus
from .serial_pool import SerialAgentPool

__all__ = [
    "AgentJob",
    "AgentPool",
    "CleanJob",
    "DoneCheck",
    "JobResult",
    "JobStatus",
    "Runner",
    "SerialAgentPool",
    "MPIAgentPool",
    "DONE_MARKER",
    "MANIFEST_NAME",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve :class:`MPIAgentPool` so mpi4py stays optional."""
    if name == "MPIAgentPool":
        from .mpi_pool import MPIAgentPool

        return MPIAgentPool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
