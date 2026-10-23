"""Tests for the serial agent pool and the base pool's injectable completion
hooks (custom ``done_check`` / ``clean_job`` / ``write_marker``).

These are MPI-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton.pool import SerialAgentPool, checkpoint
from automaton.pool.job import AgentJob, JobResult


def _ok_runner(job: AgentJob) -> JobResult:
    (job.output_dir / "artifact.txt").write_text("done")
    return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))


def test_serial_pool_runs_all_and_writes_manifest(tmp_path):
    results = SerialAgentPool(["a", "b", "c"], _ok_runner, checkpoint_root=tmp_path).run()
    assert [r.job_id for r in results] == ["0000", "0001", "0002"]
    assert all(r.success for r in results)
    lines = (tmp_path / checkpoint.MANIFEST_NAME).read_text().strip().splitlines()
    assert len(lines) == 3


def test_serial_pool_resume_skips_done(tmp_path):
    calls: list[str] = []

    def runner(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        return _ok_runner(job)

    SerialAgentPool(["a", "b"], runner, checkpoint_root=tmp_path).run()
    assert sorted(calls) == ["0000", "0001"]
    calls.clear()
    SerialAgentPool(["a", "b"], runner, checkpoint_root=tmp_path).run()
    assert calls == []


def test_custom_done_check_and_clean_job(tmp_path):
    """A caller-supplied done_check/clean_job drives completion, not DONE.json."""
    calls: list[str] = []

    def runner(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        (job.output_dir / "result.txt").write_text("PASS")
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    def done_check(job: AgentJob) -> bool:
        # "Done" iff our own result file exists and says PASS.
        marker = job.output_dir / "result.txt"
        return marker.is_file() and marker.read_text() == "PASS"

    cleaned: list[str] = []

    def clean_job(job: AgentJob) -> None:
        cleaned.append(job.job_id)
        job.output_dir.mkdir(parents=True, exist_ok=True)

    kwargs = dict(
        checkpoint_root=tmp_path,
        done_check=done_check,
        clean_job=clean_job,
        write_marker=False,
    )

    SerialAgentPool(["a", "b"], runner, **kwargs).run()
    assert sorted(calls) == ["0000", "0001"]
    # No DONE.json markers were written (write_marker=False).
    assert not checkpoint.is_done(tmp_path / "0000")

    # Resume: both are done per our custom predicate, so nothing re-runs.
    calls.clear()
    results = SerialAgentPool(["a", "b"], runner, **kwargs).run()
    assert calls == []
    # completed_results synthesizes success results even without a marker.
    assert [r.job_id for r in results] == ["0000", "0001"]
    assert all(r.success for r in results)


def test_custom_done_check_reruns_incomplete(tmp_path):
    """A job our predicate reports NOT done is cleaned and re-run on resume."""

    def done_check(job: AgentJob) -> bool:
        marker = job.output_dir / "result.txt"
        return marker.is_file() and marker.read_text() == "PASS"

    def clean_job(job: AgentJob) -> None:
        if job.output_dir.exists():
            import shutil

            shutil.rmtree(job.output_dir)
        job.output_dir.mkdir(parents=True, exist_ok=True)

    # First run: job 0 passes, job 1 leaves a non-PASS result (not done).
    def runner1(job: AgentJob) -> JobResult:
        text = "PASS" if job.index == 0 else "FAIL"
        (job.output_dir / "result.txt").write_text(text)
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    kwargs = dict(
        checkpoint_root=tmp_path,
        done_check=done_check,
        clean_job=clean_job,
        write_marker=False,
    )
    SerialAgentPool(["a", "b"], runner1, **kwargs).run()

    # Resume: only job 1 should be cleaned + re-run.
    calls: list[str] = []

    def runner2(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        (job.output_dir / "result.txt").write_text("PASS")
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    SerialAgentPool(["a", "b"], runner2, **kwargs).run()
    assert calls == ["0001"]
