"""Smoke tests for the MPI agent pool.

These are unit-level: they verify the import guard and single-process behavior
(a single-rank communicator runs every job on rank 0 itself) without requiring a
real multi-rank ``mpiexec`` launch. Full multi-rank execution is an integration
concern run under ``mpiexec``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip cleanly unless both the mpi4py Python package AND a loadable MPI runtime
# library are available. On many systems mpi4py imports fine but fails when it
# dlopen's libmpi (e.g. the MPI module isn't loaded), which surfaces at first
# real use rather than at import.
pytest.importorskip("mpi4py")
try:
    from mpi4py import MPI  # noqa: F401  (triggers libmpi load)
except Exception as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"MPI runtime unavailable: {exc}", allow_module_level=True)

from automaton.pool import MPIAgentPool, checkpoint
from automaton.pool.job import AgentJob, JobResult


def _noop_runner(job: AgentJob) -> JobResult:
    (job.output_dir / "artifact.txt").write_text("done")
    return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))


def test_single_rank_runs_all_jobs_on_rank_zero(tmp_path):
    """A single-rank communicator: rank 0 coordinates and executes every job."""
    from mpi4py import MPI

    if MPI.COMM_WORLD.Get_size() >= 2:
        pytest.skip("running under multi-rank launch; single-rank path not exercised")

    calls: list[str] = []

    def runner(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        return _noop_runner(job)

    pool = MPIAgentPool(["a", "b", "c"], runner, checkpoint_root=tmp_path)
    results = pool.run()

    assert sorted(calls) == ["0000", "0001", "0002"]
    assert [r.job_id for r in results] == ["0000", "0001", "0002"]
    assert all(r.success for r in results)
    # Every job's completion was recorded to the manifest by the coordinator.
    manifest = (tmp_path / checkpoint.MANIFEST_NAME).read_text().strip().splitlines()
    assert len(manifest) == 3


def test_single_rank_resume_skips_done_jobs(tmp_path):
    """A second single-rank run skips jobs already marked done."""
    from mpi4py import MPI

    if MPI.COMM_WORLD.Get_size() >= 2:
        pytest.skip("running under multi-rank launch; single-rank path not exercised")

    calls: list[str] = []

    def runner(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        return _noop_runner(job)

    MPIAgentPool(["a", "b"], runner, checkpoint_root=tmp_path).run()
    assert sorted(calls) == ["0000", "0001"]

    calls.clear()
    results = MPIAgentPool(["a", "b"], runner, checkpoint_root=tmp_path).run()
    assert calls == []  # both already done
    assert [r.job_id for r in results] == ["0000", "0001"]


def test_pool_is_agentpool_subclass():
    from automaton.pool.base import AgentPool

    assert issubclass(MPIAgentPool, AgentPool)
