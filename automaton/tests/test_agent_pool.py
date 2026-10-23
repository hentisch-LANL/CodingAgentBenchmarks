"""Tests for the Automaton agent pool: job canonicalization, checkpointing,
and the base pool's execution/resume logic.

These tests are MPI-free: they exercise the abstract pool's shared helpers
through a trivial concrete subclass and an in-process runner.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path (also covered by conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton.pool import checkpoint
from automaton.pool.base import AgentPool
from automaton.pool.job import AgentJob, JobResult


class SerialPool(AgentPool):
    """Minimal concrete pool that runs pending jobs in-process, in order."""

    def run(self) -> list[JobResult]:
        results = {r.job_id: r for r in self.completed_results()}
        for job in self.pending_jobs():
            result = self.execute_job(job)
            results[job.job_id] = result
        return [results[j.job_id] for j in self.jobs if j.job_id in results]


# -- job canonicalization ----------------------------------------------------


def test_job_ids_are_canonical_and_ordered(tmp_path):
    payloads = [f"task-{i}" for i in range(3)]
    pool = SerialPool(payloads, lambda job: None, checkpoint_root=tmp_path)

    assert [j.index for j in pool.jobs] == [0, 1, 2]
    assert [j.job_id for j in pool.jobs] == ["0000", "0001", "0002"]
    # payloads carried verbatim, in order
    assert [j.payload for j in pool.jobs] == payloads
    # output dirs are per-job under the checkpoint root
    assert pool.jobs[1].output_dir == tmp_path / "0001"


def test_job_identity_frozen_at_ingest(tmp_path):
    payloads = ["a", "b"]
    pool = SerialPool(payloads, lambda job: None, checkpoint_root=tmp_path)
    # Mutating the source list afterward must not change the pool's jobs.
    payloads.append("c")
    assert len(pool) == 2


# -- checkpoint marker semantics ---------------------------------------------


def test_write_done_is_atomic_and_readable(tmp_path):
    job_dir = tmp_path / "0000"
    result = JobResult(job_id="0000", success=True, output_dir=str(job_dir))
    checkpoint.write_done(job_dir, result)

    assert checkpoint.is_done(job_dir)
    loaded = checkpoint.load_result(job_dir)
    assert loaded is not None
    assert loaded.job_id == "0000"
    assert loaded.success is True
    # No leftover temp files from the atomic write.
    assert not list(job_dir.glob(".DONE.*.tmp"))


def test_corrupt_marker_is_not_done(tmp_path):
    job_dir = tmp_path / "0000"
    job_dir.mkdir()
    checkpoint.done_marker_path(job_dir).write_text("{not json")
    assert checkpoint.is_done(job_dir) is False
    assert checkpoint.load_result(job_dir) is None


def test_ensure_clean_job_dir_wipes_unfinished(tmp_path):
    job_dir = tmp_path / "0000"
    job_dir.mkdir()
    (job_dir / "partial.txt").write_text("stale")
    checkpoint.ensure_clean_job_dir(job_dir)
    assert job_dir.exists()
    assert list(job_dir.iterdir()) == []


# -- pool execution + resume --------------------------------------------------


def test_execute_job_writes_marker_on_success_only(tmp_path):
    def ok_runner(job: AgentJob) -> JobResult:
        (job.output_dir / "artifact.txt").write_text("done")
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    def fail_runner(job: AgentJob) -> JobResult:
        raise RuntimeError("boom")

    pool_ok = SerialPool(["x"], ok_runner, checkpoint_root=tmp_path / "ok")
    [res_ok] = pool_ok.run()
    assert res_ok.success
    assert checkpoint.is_done(pool_ok.jobs[0].output_dir)

    pool_fail = SerialPool(["y"], fail_runner, checkpoint_root=tmp_path / "fail")
    [res_fail] = pool_fail.run()
    assert res_fail.success is False
    assert "boom" in (res_fail.error or "")
    # No done marker on failure -> job is retryable.
    assert not checkpoint.is_done(pool_fail.jobs[0].output_dir)


def test_resume_skips_done_jobs_and_reruns_others(tmp_path):
    calls: list[str] = []

    def runner(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        # Only the first job succeeds; second always fails.
        success = job.index == 0
        if not success:
            raise RuntimeError("fail")
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    # First run: job 0 succeeds (marker), job 1 fails (no marker).
    pool1 = SerialPool(["a", "b"], runner, checkpoint_root=tmp_path)
    pool1.run()
    assert calls == ["0000", "0001"]

    # Second run with resume: job 0 skipped, job 1 re-run.
    calls.clear()
    pool2 = SerialPool(["a", "b"], runner, checkpoint_root=tmp_path)
    pool2.run()
    assert calls == ["0001"]


def test_no_resume_reruns_everything(tmp_path):
    calls: list[str] = []

    def runner(job: AgentJob) -> JobResult:
        calls.append(job.job_id)
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    SerialPool(["a", "b"], runner, checkpoint_root=tmp_path).run()
    calls.clear()
    SerialPool(["a", "b"], runner, checkpoint_root=tmp_path, resume=False).run()
    assert calls == ["0000", "0001"]


def test_completed_results_returns_full_batch(tmp_path):
    def runner(job: AgentJob) -> JobResult:
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    results = SerialPool(["a", "b", "c"], runner, checkpoint_root=tmp_path).run()
    assert [r.job_id for r in results] == ["0000", "0001", "0002"]
    assert all(r.success for r in results)


def test_jobresult_roundtrip():
    original = JobResult(
        job_id="0007",
        success=False,
        output_dir="/tmp/x",
        returncode=1,
        summary={"cost": 0.5},
        error="nope",
    )
    assert JobResult.from_dict(original.to_dict()) == original


def test_manifest_appends_one_line_per_result(tmp_path):
    def runner(job: AgentJob) -> JobResult:
        return JobResult(job_id=job.job_id, success=True, output_dir=str(job.output_dir))

    pool = SerialPool(["a", "b"], runner, checkpoint_root=tmp_path)
    for job in pool.jobs:
        checkpoint.append_manifest(tmp_path, pool.execute_job(job))

    lines = (tmp_path / checkpoint.MANIFEST_NAME).read_text().strip().splitlines()
    assert len(lines) == 2
    assert {json.loads(line)["job_id"] for line in lines} == {"0000", "0001"}
