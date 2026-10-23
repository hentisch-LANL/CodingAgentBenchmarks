"""Job value objects for the Automaton agent pool.

An :class:`AgentPool` runs a set of embarrassingly-parallel agent *jobs*. A job
is a small, canonical wrapper the pool assigns around a caller-defined,
otherwise-opaque ``payload``. The pool never inspects the payload; it is handed
verbatim to the caller-supplied runner callable that knows how to build a
:class:`~automaton.sandbox.Sandbox` run (or do whatever work the job entails)
and write its artifacts into the job's :attr:`AgentJob.output_dir`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    """Lifecycle status of a job within a pool run."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class AgentJob:
    """A single unit of work handed to an :class:`AgentPool`.

    Job identity is canonical and assigned by the pool at ingest time from the
    job's position in the input sequence; it is stable across resumes.

    Attributes
    ----------
    index : int
        Canonical ordinal assigned by insertion order into the pool.
    job_id : str
        Canonical, filesystem-safe identifier derived from :attr:`index`
        (zero-padded), used for checkpoint directories, result routing, and
        skip-on-resume.
    payload : Any
        Opaque, caller-defined description of the work. The pool never
        inspects this; it is passed verbatim to the runner callable.
    output_dir : Path
        Per-job directory (``<checkpoint_root>/<job_id>/``) the runner must
        write all artifacts (workspace, logs, deliverable) into.
    """

    index: int
    job_id: str
    payload: Any
    output_dir: Path


@dataclass(frozen=True)
class JobResult:
    """Lightweight, JSON-serializable result of a finished job.

    This is the *only* thing that crosses the MPI boundary from a worker back
    to the coordinator. Heavy artifacts stay on the (shared) filesystem in the
    job's :attr:`AgentJob.output_dir`; here we carry paths as strings and keep
    ``summary`` small.

    Attributes
    ----------
    job_id : str
        Canonical job identifier this result corresponds to.
    success : bool
        Whether the runner completed the job cleanly.
    output_dir : str
        String path to the job's output directory (artifacts live here).
    returncode : int | None
        Optional process return code reported by the runner.
    summary : dict
        Small, caller-defined, JSON-serializable summary (e.g. usage totals).
    error : str | None
        Failure detail when ``success`` is False.
    """

    job_id: str
    success: bool
    output_dir: str
    returncode: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a plain JSON-serializable dict representation."""
        return {
            "job_id": self.job_id,
            "success": self.success,
            "output_dir": self.output_dir,
            "returncode": self.returncode,
            "summary": self.summary,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobResult:
        """Reconstruct a :class:`JobResult` from :meth:`to_dict` output."""
        return cls(
            job_id=str(data["job_id"]),
            success=bool(data["success"]),
            output_dir=str(data["output_dir"]),
            returncode=data.get("returncode"),
            summary=dict(data.get("summary", {}) or {}),
            error=data.get("error"),
        )
