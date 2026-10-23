from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench import pool_runner as pr
from abc_bench.runner import _agent_identity_hash, _attempt_dir_name, _safe_path_segment


def _models() -> list[dict]:
    return [{"model": "demo/model", "base_url": "https://example.com/v1"}]


def test_build_attempt_jobs_cross_product() -> None:
    jobs = pr.build_attempt_jobs(["g/a", "g/b"], 2)
    assert [(j.task_ref, j.attempt) for j in jobs] == [
        ("g/a", 1),
        ("g/a", 2),
        ("g/b", 1),
        ("g/b", 2),
    ]
    assert all(j.attempts == 2 for j in jobs)


def _make_pool_runner(monkeypatch, tmp_path, results_of):
    """Build a BenchmarkPoolRunner whose BenchmarkRunner.run writes attempt
    metadata and returns a stubbed result, per the ``results_of`` map
    keyed by (task_ref, attempt) -> status.
    """
    archive_root = tmp_path / "results"
    agent_archive = f"demo-agent_{_agent_identity_hash('opencode', _models())}"

    # Stub Agent used by BenchmarkPoolRunner._agent_archive_dir.
    monkeypatch.setattr(
        pr,
        "Agent",
        lambda agent_id, config: SimpleNamespace(
            agent_id=agent_id,
            harness_id="opencode",
            models=[SimpleNamespace(config=_models()[0])],
        ),
    )

    calls = []

    class FakeRunner:
        def __init__(self, config, resolver, **kwargs):
            pass

        def run(self, *, task_id, agent_id, timeout, results_dir, attempt, attempts):
            calls.append((task_id, attempt))
            status = results_of[(task_id, attempt)]
            # Recreate the on-disk attempt dir + a timestamped run with metadata.
            group_id, task_id_only = task_id.split("/", 1)
            base = (
                Path(results_dir)
                / agent_archive
                / _safe_path_segment(group_id)
                / _safe_path_segment(task_id_only)
            )
            if attempts > 1:
                base = base / _attempt_dir_name(attempt)
            run_dir = base / f"run_{attempt}"
            run_dir.mkdir(parents=True, exist_ok=True)
            complete = status in {"PASS", "FAIL"}
            (run_dir / "metadata.json").write_text(
                json.dumps({"status": status, "complete": complete})
            )
            return SimpleNamespace(
                status=status,
                result_dir=run_dir,
                usage_summary={"totals": {"total_tokens": 10, "tokens_per_second": 2.0, "total_cost": 0.1}},
            )

    monkeypatch.setattr(pr, "BenchmarkRunner", FakeRunner)

    runner = pr.BenchmarkPoolRunner(
        config=SimpleNamespace(results_dir=archive_root, package_root=tmp_path),
        resolver=SimpleNamespace(),
        agent_id="demo-agent",
        results_dir=archive_root,
        timeout=None,
        runner_kwargs={},
    )
    return runner, calls, archive_root


def test_serial_pool_runs_all_attempts(monkeypatch, tmp_path) -> None:
    results_of = {
        ("g/a", 1): "PASS",
        ("g/a", 2): "FAIL",
    }
    runner, calls, archive_root = _make_pool_runner(monkeypatch, tmp_path, results_of)
    jobs = pr.build_attempt_jobs(["g/a"], 2)
    pool = runner.make_pool(jobs, tmp_path / "ckpt", use_mpi=False)
    results = pool.run()

    assert sorted(calls) == [("g/a", 1), ("g/a", 2)]
    assert len(results) == 2
    assert all(r.success for r in results)  # PASS and FAIL are both conclusive


def test_serial_pool_resume_skips_conclusive_reruns_inconclusive(monkeypatch, tmp_path) -> None:
    # First run: attempt 1 conclusive (PASS), attempt 2 inconclusive (HARNESS_ERROR).
    results_of = {
        ("g/a", 1): "PASS",
        ("g/a", 2): "HARNESS_ERROR",
    }
    runner, calls, archive_root = _make_pool_runner(monkeypatch, tmp_path, results_of)
    jobs = pr.build_attempt_jobs(["g/a"], 2)
    runner.make_pool(jobs, tmp_path / "ckpt", use_mpi=False).run()
    assert sorted(calls) == [("g/a", 1), ("g/a", 2)]

    # Second run (resume): attempt 2 now succeeds. Only attempt 2 should re-run;
    # attempt 1 was conclusive and is kept.
    calls.clear()
    results_of[("g/a", 2)] = "PASS"
    runner2, calls2, _ = _make_pool_runner(monkeypatch, tmp_path, results_of)
    jobs2 = pr.build_attempt_jobs(["g/a"], 2)
    results = runner2.make_pool(jobs2, tmp_path / "ckpt", use_mpi=False).run()

    assert calls2 == [("g/a", 2)]
    assert len(results) == 2
    assert all(r.success for r in results)
