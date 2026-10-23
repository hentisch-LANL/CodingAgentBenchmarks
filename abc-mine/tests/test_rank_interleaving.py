"""Tests for ``rank.method: interleaving``.

No agent or container runs: the per-group agent (:func:`rank.rank_group`) is
replaced by a fake. Covered:

* The split is a uniform, deterministic, input-order-independent partition into
  near-equal groups.
* Interleaving takes one pick per group per round, handles unequal shortlist
  lengths, and does not systematically favour any group within a round.
* Each group's agent is told it holds a random sample, not the whole field.
* Resume: completed groups are not re-ranked; a changed split is refused.
* The merged shortlist is what the build stage expects.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
import yaml

from abc_mine import config as C
from abc_mine import prompts as P
from abc_mine.stages import rank as K
from abc_mine.stages import rank_interleaving as IL

MODEL = {"base_url": "http://x", "model": "m", "api": "anthropic_messages"}


# -- split -------------------------------------------------------------------


def test_split_is_a_partition_into_near_equal_groups():
    shas = [f"{i:040x}" for i in range(101)]
    groups = IL.split_groups(shas, 3, seed=0)
    assert sorted(s for g in groups for s in g) == sorted(shas)
    assert sorted(len(g) for g in groups) == [33, 34, 34]


def test_split_is_deterministic_and_ignores_input_order():
    shas = [f"{i:040x}" for i in range(50)]
    a = IL.split_groups(shas, 2, seed=7)
    assert a == IL.split_groups(list(reversed(shas)), 2, seed=7)
    assert a != IL.split_groups(shas, 2, seed=8)


def test_split_is_random_not_by_score_or_order():
    """The whole method rests on the groups being random samples. A split that
    tracked the input order (e.g. alternating the score-sorted list) would bias
    every group the same way."""
    shas = [f"{i:040x}" for i in range(200)]
    (first, _second) = IL.split_groups(shas, 2, seed=0)
    first_half = set(shas[:100])
    overlap = len(first_half & set(first))
    assert 35 < overlap < 65  # far from the 0 or 100 of an order-based split


def test_split_rejects_too_few_candidates_and_duplicates():
    with pytest.raises(ValueError, match="into 3 groups"):
        IL.split_groups(["a", "b"], 3, seed=0)
    with pytest.raises(ValueError, match="unique"):
        IL.split_groups(["a", "a", "b"], 2, seed=0)


# -- interleave --------------------------------------------------------------


def test_interleave_takes_one_per_group_per_round():
    merged = IL.interleave([["a1", "a2", "a3"], ["b1", "b2", "b3"]], seed=0)
    items = [m[2] for m in merged]
    for r in range(3):
        assert set(items[2 * r:2 * r + 2]) == {f"a{r + 1}", f"b{r + 1}"}
    assert [(g, rank) for g, rank, _ in merged][:2] in ([(0, 1), (1, 1)], [(1, 1), (0, 1)])


def test_interleave_handles_unequal_lengths_and_empty_groups():
    merged = [m[2] for m in IL.interleave([["a1", "a2", "a3", "a4"], ["b1"], []], seed=3)]
    assert sorted(merged) == ["a1", "a2", "a3", "a4", "b1"]
    assert set(merged[:2]) == {"a1", "b1"}
    assert merged[2:] == ["a2", "a3", "a4"]
    assert IL.interleave([], seed=0) == []


def test_interleave_does_not_favour_a_fixed_group_within_rounds():
    """FAIRNESS: with a fixed within-round order, group 1's k-th pick would always
    precede group 2's. The shuffled order should put each group first in about
    half the rounds."""
    rankings = [[f"a{i}" for i in range(400)], [f"b{i}" for i in range(400)]]
    firsts = Counter(IL.interleave(rankings, seed=0)[2 * r][0] for r in range(400))
    assert 150 < firsts[0] < 250


def test_interleave_is_deterministic():
    r = [["a", "b"], ["c", "d"], ["e"]]
    assert IL.interleave(r, seed=1) == IL.interleave(r, seed=1)


def test_merge_shortlists_renumbers_and_records_group_provenance():
    groups = [
        {"selected": [{"sha": "a2", "rank": 2}, {"sha": "a1", "rank": 1}]},
        {"selected": [{"sha": "b1", "rank": 1}]},
    ]
    merged = IL.merge_shortlists(groups, seed=0)
    assert [e["rank"] for e in merged] == [1, 2, 3]
    assert merged[-1] == {"sha": "a2", "rank": 3, "group": 1, "group_rank": 2}
    assert {(e["sha"], e["group"], e["group_rank"]) for e in merged[:2]} == {
        ("a1", 1, 1), ("b1", 2, 1)}


def test_split_mismatch_reports_each_change():
    base = IL.split_record([["a"], ["b"]], 0, ["a", "b"])
    assert IL.split_mismatch(base, base) == []
    assert "seed changed" in IL.split_mismatch(base, IL.split_record([["a"], ["b"]], 1, ["a", "b"]))[0]
    assert "candidate set changed" in IL.split_mismatch(
        base, IL.split_record([["a"], ["c"]], 0, ["a", "c"]))[0]
    assert any("groups changed" in p for p in IL.split_mismatch(
        base, IL.split_record([["a", "b"], ["c"], ["d"]], 0, ["a", "b"])))


# -- config ------------------------------------------------------------------


def _cfg(tmp_path, n=10, **rank):
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "image.sqfs").touch()
    path = tmp_path / "mine.yaml"
    path.write_text(yaml.safe_dump({
        "run": {"id": "t"},
        "codebase": {"name": "Demo", "repo": "repo"},
        "container": {"image": "image.sqfs"},
        "rank": {"method": "interleaving", "max_prompt_tokens": 10**6, **rank},
    }))
    cfg = C.load(path)
    cfg.summaries_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        sha = f"{i + 1:02x}" * 20
        (cfg.summaries_dir / f"{sha[:12]}.json").write_text(json.dumps({
            "sha": sha, "subject": f"s{i}", "benchmark_score": i, "disqualified": False,
            "axes": {"verifiability": {"score": 3, "why": "w"}},
        }))
    return cfg


def test_config_defaults_and_validation(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.rank.method == "interleaving"
    assert (cfg.rank.interleaving.groups, cfg.rank.interleaving.seed) == (2, 0)
    cfg = C.load(cfg.source_path, overrides={"rank.interleaving.groups": 4})
    assert cfg.rank.interleaving.groups == 4
    for bad, match in (
        ({"rank.interleaving.groups": 1}, "groups must be >= 2"),
        ({"rank.method": "tournament"}, "rank.method must be one of"),
        ({"rank.interleaving.typo": 1}, "unknown rank.interleaving"),
        ({"rank.methd": "x"}, "unknown rank setting"),
    ):
        with pytest.raises(C.ConfigError, match=match):
            C.load(cfg.source_path, overrides=bad)


# -- plan --------------------------------------------------------------------


def test_plan_gives_each_group_its_own_prompt_and_honest_scope(tmp_path):
    cfg = _cfg(tmp_path, n=11)
    il_plan = IL.plan(cfg, P.load_pack(None), MODEL, counter=len)
    assert [len(g) for g in il_plan.groups] == [6, 5]
    for i, group_plan in enumerate(il_plan.group_plans):
        members = set(il_plan.groups[i])
        assert {c["sha"] for c in group_plan.candidates} == members
        for other in il_plan.groups[1 - i]:
            assert other not in group_plan.prompt
        assert "random sample" in group_plan.prompt
        assert f"group {i + 1} of 2" in group_plan.prompt
        assert "**every** viable candidate" not in group_plan.prompt
        assert group_plan.prompt_tokens == len(K.harness_prompt_text(group_plan.prompt))
    assert "interleaving" in il_plan.describe()


def test_single_context_prompt_still_claims_the_whole_field(tmp_path):
    cfg = C.load(_cfg(tmp_path).source_path, overrides={"rank.method": "single_context"})
    single = K.plan(cfg, P.load_pack(None), MODEL, counter=len)
    assert K.SCOPE_ALL in single.prompt and "random sample" not in single.prompt


def test_plan_flags_each_over_budget_group(tmp_path):
    cfg = _cfg(tmp_path, max_prompt_tokens=10)
    il_plan = IL.plan(cfg, P.load_pack(None), MODEL, counter=len)
    assert len(il_plan.over_budget) == 2
    assert "OVER BUDGET" in il_plan.describe()


# -- run (fake agent) --------------------------------------------------------


class FakeAgent:
    """Stands in for rank_group: selects every candidate, best score first."""

    def __init__(self, fail_on=()):
        self.calls: list[str] = []
        self.fail_on = set(fail_on)

    def __call__(self, cfg, model, work_plan, *, logs_dir, item_id, dry_run=False):
        self.calls.append(work_plan.label)
        if work_plan.label in self.fail_on:
            raise K.RankError(f"ranking failed ({work_plan.label}): simulated")
        ranked = sorted(work_plan.candidates, key=lambda c: -c["benchmark_score"])
        raw = {"selected": [{"sha": c["sha"], "rank": i + 1} for i, c in enumerate(ranked)]}
        shortlist, error = K._validate_shortlist(raw, work_plan)
        assert shortlist is not None, error
        return K.GroupResult(shortlist=shortlist, rationale=f"why {work_plan.label}",
                             usage={"input_tokens": 10}, seconds=1.0)


def _run(cfg, monkeypatch, agent, **kwargs):
    monkeypatch.setattr(K, "rank_group", agent)
    monkeypatch.setattr(K, "count_tokens", lambda text, model, **kw: len(text))
    return IL.run(cfg, P.load_pack(None), MODEL, progress=False, **kwargs)


def test_run_interleaves_groups_into_one_shortlist(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, n=10)
    agent = FakeAgent()
    shortlist = _run(cfg, monkeypatch, agent)
    assert agent.calls == ["group 1 of 2", "group 2 of 2"]
    assert shortlist["n_selected"] == 10
    sel = shortlist["selected"]
    assert [e["rank"] for e in sel] == list(range(1, 11))
    for r in range(5):  # one pick from each group per round
        assert {e["group"] for e in sel[2 * r:2 * r + 2]} == {1, 2}
        assert {e["group_rank"] for e in sel[2 * r:2 * r + 2]} == {r + 1}
    assert shortlist["method"]["name"] == "interleaving"
    assert shortlist["method"]["group_sizes"] == [5, 5]
    assert shortlist["usage"] == {"input_tokens": 20}

    on_disk = K.load_shortlist(cfg)
    assert on_disk["selected"] == sel
    rationale = K.rationale_path(cfg).read_text()
    assert "why group 1 of 2" in rationale and "why group 2 of 2" in rationale


def test_run_resumes_completed_groups(tmp_path, monkeypatch):
    """A failure in group 2 keeps group 1's result; the rerun ranks only group 2."""
    cfg = _cfg(tmp_path, n=8)
    with pytest.raises(K.RankError, match="group 2 of 2"):
        _run(cfg, monkeypatch, FakeAgent(fail_on={"group 2 of 2"}))
    assert (IL.group_dir(cfg, 0) / IL.GROUP_META).is_file()
    assert not (IL.group_dir(cfg, 1) / IL.GROUP_META).exists()
    assert not K.shortlist_path(cfg).exists()

    agent = FakeAgent()
    shortlist = _run(cfg, monkeypatch, agent)
    assert agent.calls == ["group 2 of 2"]
    assert shortlist["n_selected"] == 8


def test_run_refuses_to_resume_across_a_changed_split(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, n=8)
    with pytest.raises(K.RankError):
        _run(cfg, monkeypatch, FakeAgent(fail_on={"group 2 of 2"}))
    reseeded = C.load(cfg.source_path, overrides={"rank.interleaving.seed": 5})
    with pytest.raises(K.RankError, match="seed changed"):
        _run(reseeded, monkeypatch, FakeAgent())
    shortlist = _run(reseeded, monkeypatch, FakeAgent(), force=True)
    assert shortlist["method"]["seed"] == 5


def test_run_refuses_to_overwrite_a_shortlist_without_force(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, n=6)
    _run(cfg, monkeypatch, FakeAgent())
    with pytest.raises(K.RankError, match="already exists"):
        _run(cfg, monkeypatch, FakeAgent())


def test_run_refuses_an_over_budget_group_before_any_agent(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, n=6, max_prompt_tokens=10)
    with pytest.raises(K.RankError, match="group 1 of 2"):
        _run(cfg, monkeypatch, lambda *a, **k: pytest.fail("agent ran"))


def test_stage_entry_point_dispatches_on_method(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, n=4)
    monkeypatch.setattr(K, "rank_group", FakeAgent())
    monkeypatch.setattr(K, "count_tokens", lambda text, model, **kw: len(text))
    shortlist = K.run(cfg, P.load_pack(None), MODEL, progress=False)
    assert shortlist["method"]["name"] == "interleaving"


# -- round-robin parallelism -------------------------------------------------

from abc_mine import parallel as PAR  # noqa: E402


@pytest.mark.parametrize("n,size,expected", [
    (2, 2, [[0], [1]]),
    (5, 2, [[0, 2, 4], [1, 3]]),
    (2, 4, [[0], [1], [], []]),
    (0, 3, [[], [], []]),
])
def test_round_robin_assignment(n, size, expected):
    assert PAR.round_robin(n, size) == expected


def test_round_robin_rejects_an_empty_world():
    with pytest.raises(ValueError):
        PAR.round_robin(3, 0)


class FakeComm:
    """Simulate one rank of an MPI world whose other ranks have finished.

    ``others`` are the (index, result-dict) lists the other ranks would gather.
    """

    def __init__(self, rank, others=()):
        self.rank = rank
        self.others = list(others)
        self.bcast_root = None

    def bcast(self, obj, root=0):
        if self.rank == 0:
            self.bcast_root = obj
            return obj
        return self.bcast_root

    def Ibarrier(self):
        class Done:
            def Test(self):
                return True
        return Done()

    def gather(self, obj, root=0):
        return [obj, *self.others] if self.rank == 0 else None


def test_run_round_robin_runs_only_this_ranks_share_and_merges_on_rank_0():
    ran = []

    def worker(item):
        ran.append(item)
        return PAR.ItemResult(item, "ok")

    # Rank 1 of 2: runs items 1 and 3 only; reports nothing.
    comm1 = FakeComm(1)
    comm1.bcast_root = ["a", "b", "c", "d"]
    out = PAR.run_round_robin(None, worker, PAR.World(1, 2, comm1))
    assert ran == ["b", "d"] and not out.is_primary and out.results == []

    # Rank 0 of 2: runs items 0 and 2, gathers rank 1's, returns item order.
    ran.clear()
    others = [[(1, PAR.ItemResult("b").to_dict()), (3, PAR.ItemResult("d").to_dict())]]
    out = PAR.run_round_robin(["a", "b", "c", "d"], worker, PAR.World(0, 2, FakeComm(0, others)))
    assert ran == ["a", "c"] and out.is_primary
    assert [r.item_id for r in out.results] == ["a", "b", "c", "d"]


def test_run_round_robin_on_the_trivial_world_is_serial():
    out = PAR.run_round_robin([1, 2, 3], lambda i: PAR.ItemResult(str(i)), PAR.World())
    assert [r.item_id for r in out.results] == ["1", "2", "3"] and out.is_primary


def test_mpi_world_refuses_when_mpi_is_broken_under_a_multi_task_launcher(monkeypatch):
    """SAFETY: if MPI cannot load under `srun -n 2`, each task would think it is
    alone and rank EVERY group, racing on the same files. Refuse instead."""
    monkeypatch.setattr(PAR, "mpi_available", lambda: (False, "no MPI"))
    monkeypatch.setenv("SLURM_NTASKS", "2")
    with pytest.raises(PAR.ParallelError, match="2 tasks"):
        PAR.mpi_world("mpi")
    monkeypatch.setenv("SLURM_NTASKS", "1")
    assert PAR.mpi_world("mpi").size == 1
    assert PAR.mpi_world("local").size == 1


def test_interleaving_ranks_share_the_groups_round_robin(tmp_path, monkeypatch):
    """Rank 1 of 2 ranks exactly group 2 and writes no shortlist; rank 0 ranks
    group 1 and, once group 2's result is on disk, merges both."""
    cfg = C.load(_cfg(tmp_path, n=8).source_path,
                 overrides={"execution.parallel": "mpi"})
    pack = P.load_pack(None)
    monkeypatch.setattr(K, "count_tokens", lambda text, model, **kw: len(text))

    # Rank 0 plans first (as it would, before the broadcast).
    items = list(enumerate(IL.plan(cfg, pack, MODEL).group_plans))

    agent1 = FakeAgent()
    monkeypatch.setattr(K, "rank_group", agent1)
    # What rank 0 broadcasts, in order: no setup error, then the work items.
    comm1 = FakeComm(1)
    broadcasts = iter([None, items])
    comm1.bcast = lambda obj, root=0: next(broadcasts)
    monkeypatch.setattr(PAR, "mpi_world", lambda backend: PAR.World(1, 2, comm1))
    out = IL.run(cfg, pack, MODEL, progress=False)
    assert out == {"is_primary": False}
    assert agent1.calls == ["group 2 of 2"]
    assert not K.shortlist_path(cfg).exists()
    assert (IL.group_dir(cfg, 1) / IL.GROUP_META).is_file()

    agent0 = FakeAgent()
    monkeypatch.setattr(K, "rank_group", agent0)
    others = [[(1, PAR.ItemResult("group-2").to_dict())]]
    monkeypatch.setattr(PAR, "mpi_world", lambda backend: PAR.World(0, 2, FakeComm(0, others)))
    shortlist = IL.run(cfg, pack, MODEL, progress=False)
    assert agent0.calls == ["group 1 of 2"]
    assert shortlist["n_selected"] == 8
    assert {e["group"] for e in shortlist["selected"]} == {1, 2}


def test_a_failed_group_does_not_stop_the_others(tmp_path, monkeypatch):
    """Group 1 fails; group 2 still runs and is saved. The error names group 1,
    and a rerun ranks only group 1."""
    cfg = _cfg(tmp_path, n=8)
    agent = FakeAgent(fail_on={"group 1 of 2"})
    with pytest.raises(K.RankError, match="1 of 2 group"):
        _run(cfg, monkeypatch, agent)
    assert agent.calls == ["group 1 of 2", "group 2 of 2"]
    assert (IL.group_dir(cfg, 1) / IL.GROUP_META).is_file()
    retry = FakeAgent()
    _run(cfg, monkeypatch, retry)
    assert retry.calls == ["group 1 of 2"]


def test_setup_errors_reach_every_rank(tmp_path, monkeypatch):
    """A setup failure on rank 0 (here: an existing shortlist) is broadcast, so
    rank 1 exits instead of waiting forever in a collective."""
    cfg = C.load(_cfg(tmp_path, n=4).source_path, overrides={"execution.parallel": "mpi"})
    monkeypatch.setattr(K, "count_tokens", lambda text, model, **kw: len(text))
    comm1 = FakeComm(1)
    comm1.bcast = lambda obj, root=0: "a shortlist already exists"
    monkeypatch.setattr(PAR, "mpi_world", lambda backend: PAR.World(1, 2, comm1))
    monkeypatch.setattr(K, "rank_group", lambda *a, **k: pytest.fail("agent ran"))
    assert IL.run(cfg, P.load_pack(None), MODEL, progress=False) == {"is_primary": False}
