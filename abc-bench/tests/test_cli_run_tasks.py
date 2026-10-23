from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from abc_bench import cli
from abc_bench import pool_runner as pool_runner_mod


def _fake_config(**overrides):
    defaults = {
        "package_root": Path("/tmp/data"),
        "installed_root": Path("/tmp/installed"),
        "group_resolver": (lambda task_dir=None, warn=True: SimpleNamespace()),
        "agents": {},
        "results_dir": Path("/tmp/results"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _args(**overrides):
    defaults = {
        "patterns": ["demo/*"],
        "agent": "demo-agent",
        "config": Path("/tmp/demo-config.yaml"),
        "timeout": None,
        "results_dir": None,
        "dry_run": False,
        "no_stream_output": False,
        "attempts": 1,
        "mpi": False,
        "harness_memory_limit": None,
        "harness_memory_limit_fraction": 0.9,
        "no_harness_memory_limit": False,
        "no_clean_env": False,
        "no_sandbox_network": False,
        "verbose": False,
        "task_dir": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeManager:
    def __init__(self, package_root, resolver):
        self.package_root = package_root
        self.resolver = resolver

    def check_charliecloud_available(self):
        return True, None


def _job_result(job_id, status):
    return SimpleNamespace(
        job_id=job_id,
        success=status in {"PASS", "FAIL"},
        summary={"status": status},
    )


def _patch_common(monkeypatch, config, matched, unmatched):
    monkeypatch.setattr(cli, "Config", lambda path: config)
    monkeypatch.setattr(cli, "_resolver", lambda config, args: SimpleNamespace())
    monkeypatch.setattr(cli, "ContainerManager", _FakeManager)
    monkeypatch.setattr(cli.Task, "match_task_refs", lambda patterns, resolver: (matched, unmatched))
    monkeypatch.setattr(pool_runner_mod, "build_required_containers", lambda *a, **k: None)


def test_cmd_run_tasks_warns_for_unmatched_patterns_and_runs_matches(monkeypatch, capsys) -> None:
    config = _fake_config()
    made = {}

    class FakePool:
        def __init__(self, jobs):
            self.jobs = jobs

        def run(self):
            # one job per matched task (attempts=1)
            return [_job_result(f"{i:04d}", "PASS" if i == 0 else "FAIL") for i in range(len(self.jobs))]

    class FakePoolRunner:
        def __init__(self, *args, **kwargs):
            made["kwargs"] = kwargs

        def make_pool(self, jobs, checkpoint_root, *, use_mpi):
            made["use_mpi"] = use_mpi
            made["n_jobs"] = len(jobs)
            return FakePool(jobs)

    _patch_common(monkeypatch, config, ["demo/a", "demo/b"], ["missing/*"])
    monkeypatch.setattr(pool_runner_mod, "BenchmarkPoolRunner", FakePoolRunner)

    exit_code = cli.cmd_run_tasks(_args(patterns=["demo/*", "missing/*"]))

    captured = capsys.readouterr()
    assert "Warning: the following pattern(s) matched no canonical task refs: missing/*" in captured.err
    assert "Matched 2 task(s):" in captured.out
    assert made["n_jobs"] == 2  # 2 tasks x 1 attempt
    assert made["use_mpi"] is False
    # one PASS, one FAIL -> both conclusive, exit 0
    assert exit_code == 0
    assert "PASS: 1" in captured.out
    assert "FAIL: 1" in captured.out


def test_cmd_run_tasks_fails_when_all_patterns_miss(monkeypatch, capsys) -> None:
    config = _fake_config()
    _patch_common(monkeypatch, config, [], ["missing/*"])

    exit_code = cli.cmd_run_tasks(_args(patterns=["missing/*"]))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: no tasks matched the supplied canonical task-ref pattern(s): missing/*" in captured.err


def test_cmd_run_tasks_expands_attempts_into_jobs(monkeypatch, capsys) -> None:
    config = _fake_config()
    made = {}

    class FakePool:
        def __init__(self, jobs):
            self.jobs = jobs

        def run(self):
            return [_job_result(f"{i:04d}", "PASS") for i in range(len(self.jobs))]

    class FakePoolRunner:
        def __init__(self, *args, **kwargs):
            pass

        def make_pool(self, jobs, checkpoint_root, *, use_mpi):
            made["n_jobs"] = len(jobs)
            return FakePool(jobs)

    _patch_common(monkeypatch, config, ["demo/a", "demo/b"], [])
    monkeypatch.setattr(pool_runner_mod, "BenchmarkPoolRunner", FakePoolRunner)

    exit_code = cli.cmd_run_tasks(_args(attempts=3))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert made["n_jobs"] == 6  # 2 tasks x 3 attempts
    assert "Attempts/task: 3" in captured.out
    assert "Jobs: 6" in captured.out


def test_cmd_run_tasks_rejects_zero_attempts(monkeypatch, capsys) -> None:
    config = _fake_config()
    _patch_common(monkeypatch, config, ["demo/a"], [])

    exit_code = cli.cmd_run_tasks(_args(attempts=0))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--attempts must be >= 1" in captured.err


def test_cmd_run_tasks_reports_inconclusive_as_retryable(monkeypatch, capsys) -> None:
    config = _fake_config()

    class FakePool:
        def __init__(self, jobs):
            self.jobs = jobs

        def run(self):
            # one conclusive, one inconclusive
            return [_job_result("0000", "PASS"), _job_result("0001", "HARNESS_ERROR")]

    class FakePoolRunner:
        def __init__(self, *args, **kwargs):
            pass

        def make_pool(self, jobs, checkpoint_root, *, use_mpi):
            return FakePool(jobs)

    _patch_common(monkeypatch, config, ["demo/a", "demo/b"], [])
    monkeypatch.setattr(pool_runner_mod, "BenchmarkPoolRunner", FakePoolRunner)

    exit_code = cli.cmd_run_tasks(_args())

    captured = capsys.readouterr()
    # an inconclusive job leaves work pending -> nonzero exit
    assert exit_code == 1
    assert "Inconclusive (will retry on resume): 1" in captured.out
