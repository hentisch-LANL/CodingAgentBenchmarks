"""Tests for ``rank.output: full`` and building down a ranking.

Covered:

* The ranking checker accepts only an exact permutation of the candidates, and
  reports every kind of defect — the same code the agent is told to run.
* The full-ranking prompt spells out the completeness requirement and points at
  the staged checker and SHA list.
* A validated ranking becomes a complete, rank-ordered ``shortlist.json``; null
  refinements fall back to the screening pass's text.
* Interleaving merges full rankings into one complete order.
* The build stage builds in rank order, skips built and exhausted commits, and
  honours a per-invocation ``--limit`` that later jobs continue past.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from abc_mine import config as C
from abc_mine import prompts as P
from abc_mine import ranking_check as RC
from abc_mine.manifest import record_attempt
from abc_mine.stages import build as B
from abc_mine.stages import rank as K
from abc_mine.stages import rank_interleaving as IL

MODEL = {"base_url": "http://x", "model": "m", "api": "anthropic_messages"}
SHAS = [f"{i:02x}" * 20 for i in range(1, 6)]


def _line(sha, **extra):
    base = {"sha": sha, "viability": "v", "favorability": "f",
            "est_difficulty": "medium", "task_type": ["bug_fix"]}
    return json.dumps({**base, **extra})


def _problems(lines, shas=SHAS):
    entries, problems = RC.parse_lines("\n".join(lines))
    return problems + RC.check(entries, shas)


# -- checker -----------------------------------------------------------------


def test_checker_accepts_an_exact_permutation_in_any_order():
    assert _problems([_line(s) for s in reversed(SHAS)]) == []


@pytest.mark.parametrize("mutate,match", [
    (lambda ls: ls[:-1], "1 candidate(s) are missing"),
    (lambda ls: ls + [ls[0]], "duplicate"),
    (lambda ls: ls + [_line("f" * 40)], "not one of the candidates"),
    (lambda ls: [_line(SHAS[0][:12])] + ls[1:], "abbreviated; write the full SHA"),
    (lambda ls: ["not json"] + ls, "not valid JSON"),
    (lambda ls: ['["a list"]'] + ls, "expected a JSON object"),
    (lambda ls: [json.dumps({"sha": SHAS[0]})] + ls[1:], "missing viability"),
])
def test_checker_rejects_every_kind_of_defect(mutate, match):
    """REJECTED IN FULL: omissions, duplicates, strangers, abbreviations, and
    malformed lines each fail the check, with a message saying which."""
    problems = _problems(mutate([_line(s) for s in SHAS]))
    assert any(match in p for p in problems), problems


def test_checker_script_runs_standalone(tmp_path):
    """The agent runs the checker as a plain script with the container's
    python3, so it must work with no package installed."""
    shas = tmp_path / "shas.txt"
    shas.write_text("\n".join(SHAS) + "\n")
    ranking = tmp_path / "ranking.jsonl"
    ranking.write_text("\n".join(_line(s) for s in SHAS) + "\n")
    script = tmp_path / "check_ranking.py"
    script.write_text(open(RC.__file__).read())

    ok = subprocess.run([sys.executable, "-I", str(script), str(shas), str(ranking)],
                        capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.startswith("OK")

    ranking.write_text("\n".join(_line(s) for s in SHAS[1:]) + "\n")
    bad = subprocess.run([sys.executable, "-I", str(script), str(shas), str(ranking)],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and "missing from the ranking" in bad.stdout


# -- config + prompt ---------------------------------------------------------


def _cfg(tmp_path, n=6, **rank):
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "image.sqfs").touch()
    path = tmp_path / "mine.yaml"
    path.write_text(yaml.safe_dump({
        "run": {"id": "t"},
        "codebase": {"name": "Demo", "repo": "repo"},
        "container": {"image": "image.sqfs"},
        "rank": {"output": "full", "max_prompt_tokens": 10**6, **rank},
    }))
    cfg = C.load(path)
    cfg.summaries_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        sha = f"{i + 1:02x}" * 20
        (cfg.summaries_dir / f"{sha[:12]}.json").write_text(json.dumps({
            "sha": sha, "subject": f"s{i}", "benchmark_score": i, "disqualified": False,
            "one_line": f"screened one line {i}", "proposed_task": f"task {i}",
            "axes": {"verifiability": {"score": 3, "why": "w"}},
        }))
    return cfg


def test_output_is_validated(tmp_path):
    assert _cfg(tmp_path).rank.output == "full"
    with pytest.raises(C.ConfigError, match="rank.output"):
        C.load(_cfg(tmp_path).source_path, overrides={"rank.output": "top10"})


def test_full_prompt_demands_completeness_and_names_the_checker(tmp_path):
    cfg = _cfg(tmp_path)
    prompt = K.plan(cfg, P.load_pack(None), MODEL, counter=len).prompt
    assert "MUST BE COMPLETE AND EXACT" in prompt
    assert "rejected in full" in prompt
    assert f"/workspace/{K.CHECKER_FILENAME}" in prompt
    assert f"/workspace/{K.CANDIDATE_SHAS_FILENAME}" in prompt
    assert f"/workspace/out/{K.RANKING_FILENAME}" in prompt
    assert "shortlist.json" not in prompt
    assert "How many to select" not in prompt  # shortlist-only guidance
    for field in RC.REQUIRED_FIELDS:
        assert f"`{field}` (required)" in prompt
    assert "@@" not in prompt


def test_shortlist_prompt_is_unchanged_in_substance(tmp_path):
    cfg = C.load(_cfg(tmp_path).source_path, overrides={"rank.output": "shortlist"})
    prompt = K.plan(cfg, P.load_pack(None), MODEL, counter=len).prompt
    assert "shortlist.json" in prompt and "How many to select" in prompt
    assert "MUST BE COMPLETE" not in prompt


# -- validation into shortlist form -----------------------------------------


def test_validate_ranking_builds_a_complete_ordered_selection(tmp_path):
    cfg = _cfg(tmp_path, n=3)
    work_plan = K.plan(cfg, P.load_pack(None), MODEL, counter=len)
    shas = [c["sha"] for c in work_plan.candidates]
    text = "\n".join([_line(shas[2], one_line="refined"), _line(shas[0]), _line(shas[1])])
    shortlist, error = K._validate_ranking(text, work_plan)
    assert error == ""
    assert shortlist["complete"] is True and shortlist["n_selected"] == 3
    assert [e["sha"] for e in shortlist["selected"]] == [shas[2], shas[0], shas[1]]
    assert [e["rank"] for e in shortlist["selected"]] == [1, 2, 3]
    assert shortlist["selected"][0]["one_line"] == "refined"
    # A null refinement falls back to the screening text.
    by_sha = {c["sha"]: c for c in work_plan.candidates}
    assert shortlist["selected"][1]["one_line"] == by_sha[shas[0]]["one_line"]


def test_validate_ranking_rejects_an_incomplete_ranking_whole(tmp_path):
    cfg = _cfg(tmp_path, n=3)
    work_plan = K.plan(cfg, P.load_pack(None), MODEL, counter=len)
    shortlist, error = K._validate_ranking(_line(work_plan.candidates[0]["sha"]), work_plan)
    assert shortlist is None
    assert "2 candidate(s) are missing" in error


# -- interleaving full rankings ---------------------------------------------


class FullAgent:
    """Stands in for rank_group under rank.output: full."""

    def __init__(self):
        self.calls = []

    def __call__(self, cfg, model, work_plan, *, logs_dir, item_id, dry_run=False):
        self.calls.append(work_plan.label)
        ranked = sorted(work_plan.candidates, key=lambda c: -c["benchmark_score"])
        shortlist, error = K._validate_ranking(
            "\n".join(_line(c["sha"]) for c in ranked), work_plan)
        assert shortlist is not None, error
        return K.GroupResult(shortlist=shortlist, rationale="r", usage={}, seconds=1.0)


def test_interleaving_full_rankings_yields_one_complete_order(tmp_path, monkeypatch):
    cfg = C.load(_cfg(tmp_path, n=9).source_path,
                 overrides={"rank.method": "interleaving"})
    monkeypatch.setattr(K, "rank_group", FullAgent())
    monkeypatch.setattr(K, "count_tokens", lambda text, model, **kw: len(text))
    shortlist = IL.run(cfg, P.load_pack(None), MODEL, progress=False)
    assert shortlist["complete"] is True and shortlist["n_selected"] == 9
    assert sorted(e["sha"] for e in shortlist["selected"]) == sorted(
        f"{i + 1:02x}" * 20 for i in range(9))
    assert [e["rank"] for e in shortlist["selected"]] == list(range(1, 10))
    assert shortlist["method"]["output"] == "full"
    assert "ranking of every candidate" in K.rationale_path(cfg).read_text()


def test_interleaving_refuses_to_resume_across_an_output_change(tmp_path):
    a = IL.split_record([["a"], ["b"]], 0, ["a", "b"], "shortlist")
    b = IL.split_record([["a"], ["b"]], 0, ["a", "b"], "full")
    assert any("rank.output changed" in p for p in IL.split_mismatch(a, b))


# -- building down the order -------------------------------------------------


def _write_full_shortlist(cfg, shas):
    cfg.rank_dir.mkdir(parents=True, exist_ok=True)
    K.shortlist_path(cfg).write_text(json.dumps({
        "complete": True,
        # Deliberately out of rank order on disk: the build must sort.
        "selected": [{"sha": s, "rank": r, "one_line": f"task {r}"}
                     for r, s in reversed(list(enumerate(shas, start=1)))],
    }))


def test_build_plan_follows_rank_order_and_honours_limit(tmp_path):
    cfg = _cfg(tmp_path)
    _write_full_shortlist(cfg, SHAS)
    plan = B.plan(cfg, limit=2)
    assert plan.full_ranking
    assert [e["rank"] for e in plan.pending] == [1, 2]
    assert plan.deferred == 3
    assert "left for later jobs: 3" in plan.describe()
    assert "ranks 1–2" in plan.describe()
    assert [e["rank"] for e in B.plan(cfg).pending] == [1, 2, 3, 4, 5]


def test_build_plan_continues_past_built_and_exhausted_commits(tmp_path):
    """A later job with the same --limit picks up where the last left off:
    built commits are skipped, and so are commits out of attempts."""
    cfg = _cfg(tmp_path)
    _write_full_shortlist(cfg, SHAS)
    B.append_manifest(B.manifest_path(cfg), {"task_id": B.task_id("task 1", SHAS[0]),
                                             "status": "ok"})
    tid2 = B.task_id("task 2", SHAS[1])
    for _ in range(cfg.execution.max_attempts):
        record_attempt(B.attempts_path(cfg), tid2, "failed", detail="x")
    plan = B.plan(cfg, limit=2)
    assert [e["rank"] for e in plan.pending] == [3, 4]
    assert plan.already_done == 1 and plan.exhausted == 1 and plan.deferred == 1
    # --force ignores both records.
    assert [e["rank"] for e in B.plan(cfg, limit=2, force=True).pending] == [1, 2]


def test_build_plan_rejects_a_nonpositive_limit(tmp_path):
    cfg = _cfg(tmp_path)
    _write_full_shortlist(cfg, SHAS)
    with pytest.raises(K.RankError, match="--limit"):
        B.plan(cfg, limit=0)


def test_build_prompt_falls_back_to_screening_text(tmp_path):
    cfg = _cfg(tmp_path, n=1)
    summary = json.loads(next(cfg.summaries_dir.glob("*.json")).read_text())
    entry = {"sha": summary["sha"], "rank": 7}
    prompt = B._compose_prompt(cfg, P.load_pack(None), entry, summary)
    assert "task 0" in prompt and "screened one line 0" in prompt
    assert "| Rank | 7 |" in prompt
