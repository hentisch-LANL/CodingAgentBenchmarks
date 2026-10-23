"""Filesystem checkpointing for the Automaton agent pool.

The checkpoint store is a single root directory containing one subdirectory per
job (``<root>/<job_id>/``). A job is considered *definitively done* only when a
:data:`DONE_MARKER` file exists inside its directory and parses cleanly; that
marker is written **atomically** so it is never observed half-written.

Completion semantics (per the pool's design): a done marker is written **only
on success**. A job directory that exists without a valid done marker is treated
as *unfinished* — on resume it is wiped and the job re-run from scratch. There
is no resuming of partially-completed agents.

This module knows nothing about MPI; it is pure host-side filesystem logic and
is safe to call from any rank against a shared filesystem, provided each job_id
is written by at most one process.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .job import JobResult

#: Name of the definitive completion marker written into each job directory.
DONE_MARKER = "DONE.json"

#: Name of the coordinator-side aggregated manifest at the checkpoint root.
MANIFEST_NAME = "manifest.jsonl"


def job_dir(root: Path, job_id: str) -> Path:
    """Return the per-job directory ``<root>/<job_id>/``."""
    return Path(root) / job_id


def done_marker_path(job_directory: Path) -> Path:
    """Return the path to the definitive done marker inside ``job_directory``."""
    return Path(job_directory) / DONE_MARKER


def is_done(job_directory: Path) -> bool:
    """Return True iff a valid (parseable) done marker exists.

    A truncated or corrupt marker is treated as *not done*, so the job will be
    re-run rather than trusted.
    """
    marker = done_marker_path(job_directory)
    if not marker.is_file():
        return False
    try:
        json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    return True


def load_result(job_directory: Path) -> JobResult | None:
    """Load the :class:`JobResult` recorded in a job's done marker.

    Returns
    -------
    JobResult | None
        The recorded result, or ``None`` if the marker is missing/unreadable.
    """
    marker = done_marker_path(job_directory)
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    try:
        return JobResult.from_dict(data)
    except (KeyError, TypeError):
        return None


def write_done(job_directory: Path, result: JobResult) -> Path:
    """Atomically write the definitive done marker for a job.

    The marker is written to a temporary file in the same directory and then
    :func:`os.replace`-d into place, so a reader never observes a partial
    marker even if the process crashes mid-write.

    Returns
    -------
    Path
        Path to the written marker.
    """
    directory = Path(job_directory)
    directory.mkdir(parents=True, exist_ok=True)
    marker = done_marker_path(directory)
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".DONE.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, marker)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return marker


def ensure_clean_job_dir(job_directory: Path) -> Path:
    """Return a clean, empty job directory ready for a fresh run.

    If the directory already exists (an unfinished remnant), it is removed
    entirely and recreated — unfinished agents are never resumed.
    """
    directory = Path(job_directory)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def append_manifest(root: Path, result: JobResult) -> None:
    """Append one JSON line describing ``result`` to the root manifest.

    Intended to be called only by the coordinator (a single writer) as results
    arrive, giving an aggregated, human-inspectable view of the run.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result.to_dict(), sort_keys=True)
    with open(root / MANIFEST_NAME, "a") as handle:
        handle.write(line + "\n")
