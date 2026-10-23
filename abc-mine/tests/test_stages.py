"""Tests for the pure/offline helpers in the pipeline stages.

No agent, container, MPI, or network is involved anywhere in this file. What is
covered are the deterministic decision functions whose *polarity* and
*fail-closed* behaviour the correctness of the whole pipeline rests on:

* :func:`build.decide_verifier_result` must agree with abc-bench exactly. If the
  two disagreed, a task could satisfy this pipeline's polarity check and then be
  graded differently in production — the precise failure the check exists to
  prevent.
* :func:`build._sweep_incomplete` must never delete anything it did not plan.
* :func:`manifest._severity` must classify unknown keys as *critical*, so a field
  added in a future version fails closed rather than slipping through.
* :func:`preflight._looks_like_no_tests_ran` must catch an exit-0 run that tested
  nothing, which would otherwise propagate into shipped verifiers that pass
  vacuously.
"""

from __future__ import annotations

import json

import pytest

from abc_mine import manifest as M
from abc_mine import preflight as PF
from abc_mine.stages import build as B
from abc_mine.stages import rank as K


# ===========================================================================
# build.decide_verifier_result — POLARITY
# ===========================================================================


@pytest.mark.parametrize(
    "output,returncode,expected",
    [
        # The happy path: a clean PASS with a zero exit.
        ("PASS\n", 0, "PASS"),
        # A script that prints PASS then dies in cleanup has NOT passed.
        ("PASS\n", 1, "FAIL"),
        # An explicit FAIL is a FAIL even on a zero exit.
        ("FAIL\n", 0, "FAIL"),
        # The LAST status line wins, both directions.
        ("PASS\nFAIL\n", 0, "FAIL"),
        ("FAIL\nPASS\n", 0, "PASS"),
        # A bare successful exit with no status line is not a pass.
        ("no status\n", 0, "FAIL"),
        # Substrings must not count: only lines that are EXACTLY PASS or FAIL.
        ("PASSING but not exact\n", 0, "FAIL"),
        # returncode=None means "not applicable" and must not veto a PASS.
        ("PASS\n", None, "PASS"),
    ],
)
def test_decide_verifier_result(output, returncode, expected):
    """POLARITY CONTRACT, exhaustively. Both halves of the rule matter: the last
    exact status line wins, AND a PASS additionally requires a zero exit. A
    verifier judged PASS here but FAIL by abc-bench (or vice versa) would make
    every polarity check meaningless."""
    assert B.decide_verifier_result(output, returncode) == expected


@pytest.mark.parametrize(
    "output",
    ["", "\n\n", "  PASSED  \n", "pass\n", "Pass\n", "FAILED\n", "[PASS]\n", "PASS FAIL\n"],
)
def test_decide_verifier_result_is_fail_closed(output):
    """FAIL-CLOSED: anything that is not an exact ``PASS`` line is a FAIL. Empty
    output, lowercase, decorated, or near-miss text must never certify a task."""
    assert B.decide_verifier_result(output, 0) == "FAIL"


def test_decide_verifier_result_tolerates_surrounding_whitespace():
    """The status line may be indented by a shell script; stripped content is what
    is compared."""
    assert B.decide_verifier_result("   PASS   \n", 0) == "PASS"
    assert B.decide_verifier_result("\t FAIL \t\n", 0) == "FAIL"


def test_decide_verifier_result_ignores_noise_around_the_status():
    output = "compiling...\nrunning test foo\nPASS\ncleanup done\n"
    assert B.decide_verifier_result(output, 0) == "PASS"


def test_decide_verifier_result_nonzero_exit_vetoes_even_a_late_pass():
    assert B.decide_verifier_result("FAIL\nPASS\n", 3) == "FAIL"


# ===========================================================================
# build.task_id
# ===========================================================================


def test_task_id_is_a_slug_plus_eight_sha_characters():
    sha = "0123456789abcdef" * 2 + "01234567"
    tid = B.task_id("Fix the broken solver", sha)
    assert tid == "fix-the-broken-solver-01234567"
    assert tid.endswith(sha[:8])


def test_task_id_is_filesystem_safe():
    """The id becomes a directory name inside the emitted task tree."""
    tid = B.task_id("Fix I/O: handle 'weird' paths (like ../x)!", "abcdef1234567890")
    assert all(ch.isalnum() or ch == "-" for ch in tid), tid
    assert "/" not in tid and ".." not in tid
    assert not tid.startswith("-") and not tid.endswith("-")


def test_task_id_falls_back_when_the_description_slugs_to_nothing():
    assert B.task_id("---", "abcdef1234567890") == "task-abcdef12"
    assert B.task_id("", "abcdef1234567890") == "task-abcdef12"


def test_identical_descriptions_with_different_shas_get_different_ids():
    """UNIQUENESS: histories with templated commit subjects produce identical
    descriptions constantly. Colliding ids would make two tasks overwrite each
    other in the emitted tree and confuse manifest resume."""
    a = B.task_id("Update dependencies", "aaaaaaaaaaaaaaaa")
    b = B.task_id("Update dependencies", "bbbbbbbbbbbbbbbb")
    assert a != b
    assert a.rsplit("-", 1)[0] == b.rsplit("-", 1)[0]


def test_task_id_is_capped_in_length():
    tid = B.task_id("word " * 100, "abcdef1234567890")
    assert len(tid) <= 56 + 1 + 8


# ===========================================================================
# build manifest round-trip
# ===========================================================================


def test_manifest_round_trip(tmp_path):
    path = tmp_path / "manifest.jsonl"
    assert B.read_manifest(path) == []  # missing file is empty, not an error

    B.append_manifest(path, {"task_id": "t1", "status": "ok"})
    B.append_manifest(path, {"task_id": "t2", "status": "failed"})
    entries = B.read_manifest(path)
    assert [e["task_id"] for e in entries] == ["t1", "t2"]
    assert B.completed_tasks(path) == {"t1"}


def test_append_manifest_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "manifest.jsonl"
    B.append_manifest(path, {"task_id": "t", "status": "ok"})
    assert path.is_file()


def test_append_manifest_writes_one_line_per_entry(tmp_path):
    """One complete line per append is what makes O_APPEND atomic for parallel
    workers, so the manifest can be the source of truth without a lock."""
    path = tmp_path / "manifest.jsonl"
    for i in range(5):
        B.append_manifest(path, {"task_id": f"t{i}", "status": "ok"})
    lines = path.read_text().splitlines()
    assert len(lines) == 5
    for line in lines:
        assert json.loads(line)["status"] == "ok"


def test_completed_tasks_failed_then_ok_counts_as_complete(tmp_path):
    """LATEST-ENTRY-WINS: a task that failed and was later rebuilt successfully
    must be skipped on resume, not rebuilt forever."""
    path = tmp_path / "manifest.jsonl"
    B.append_manifest(path, {"task_id": "t1", "status": "failed"})
    B.append_manifest(path, {"task_id": "t1", "status": "ok"})
    assert B.completed_tasks(path) == {"t1"}


def test_completed_tasks_ok_then_failed_does_not_count(tmp_path):
    """The other direction: a task that was ok but failed on a later forced
    rebuild is NOT complete. Trusting the earlier ok would ship a task directory
    that the forced rebuild already tore down."""
    path = tmp_path / "manifest.jsonl"
    B.append_manifest(path, {"task_id": "t1", "status": "ok"})
    B.append_manifest(path, {"task_id": "t1", "status": "failed"})
    assert B.completed_tasks(path) == set()


def test_completed_tasks_ignores_other_statuses(tmp_path):
    path = tmp_path / "manifest.jsonl"
    B.append_manifest(path, {"task_id": "t1", "status": "dry_run"})
    B.append_manifest(path, {"task_id": "t2", "status": ""})
    B.append_manifest(path, {"task_id": "t3"})
    assert B.completed_tasks(path) == set()


def test_read_manifest_skips_malformed_lines_without_being_fatal(tmp_path):
    """A manifest truncated by a killed job must not prevent the resume it exists
    to support."""
    path = tmp_path / "manifest.jsonl"
    path.write_text(
        '{"task_id": "t1", "status": "ok"}\n'
        "not json at all\n"
        '{"task_id": "t2", "status": "ok"\n'  # truncated
        "\n"
        '{"task_id": "t3", "status": "ok"}\n'
    )
    entries = B.read_manifest(path)
    assert [e["task_id"] for e in entries] == ["t1", "t3"]
    assert B.completed_tasks(path) == {"t1", "t3"}


def test_manifest_entries_without_a_task_id_are_ignored(tmp_path):
    path = tmp_path / "manifest.jsonl"
    B.append_manifest(path, {"status": "ok"})
    assert B.completed_tasks(path) == set()


# ===========================================================================
# build.validate_task_dir
# ===========================================================================


def _complete_task_dir(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / B.OUT_PROMPT).write_text("solve it\n")
    (root / B.OUT_BUILD_ENV).write_text("#!/bin/bash\n")
    (root / "workspace").mkdir()
    (root / "verification").mkdir(exist_ok=True)
    (root / B.OUT_VERIFY).write_text("#!/bin/bash\necho PASS\n")
    return root


def test_validate_task_dir_lists_all_four_missing_components(tmp_path):
    """All four components are required for a abc-bench task; reporting them all
    at once beats one error per run attempt."""
    empty = tmp_path / "task"
    empty.mkdir()
    missing = B.validate_task_dir(empty)
    assert set(missing) == {B.OUT_PROMPT, B.OUT_BUILD_ENV, "workspace", B.OUT_VERIFY}
    assert len(missing) == 4


def test_validate_task_dir_returns_empty_for_a_complete_dir(tmp_path):
    assert B.validate_task_dir(_complete_task_dir(tmp_path / "task")) == []


def test_validate_task_dir_reports_a_single_missing_component(tmp_path):
    root = _complete_task_dir(tmp_path / "task")
    (root / B.OUT_VERIFY).unlink()
    assert B.validate_task_dir(root) == [B.OUT_VERIFY]


def test_validate_task_dir_on_a_nonexistent_dir_reports_everything(tmp_path):
    assert len(B.validate_task_dir(tmp_path / "nope")) == 4


# ===========================================================================
# build._sweep_incomplete
# ===========================================================================


def test_sweep_removes_planned_but_incomplete_keeps_the_rest(tmp_path):
    """SWEEP SCOPE: a partial task directory from a killed job is
    indistinguishable from a complete one by inspection alone, so it is removed
    rather than trusted. But the sweep must touch NOTHING else — not a completed
    task, not an unplanned one from another selection, and never group.yaml."""
    group = tmp_path / "group"
    group.mkdir()
    (group / "group.yaml").write_text("id: g\n")

    incomplete = group / "planned-incomplete"
    incomplete.mkdir()
    (incomplete / B.OUT_PROMPT).write_text("half done\n")

    completed = _complete_task_dir(group / "planned-complete")
    unplanned = _complete_task_dir(group / "not-planned")

    removed = B._sweep_incomplete(
        group,
        planned={"planned-incomplete", "planned-complete"},
        done={"planned-complete"},
        force=False,
    )

    assert removed == ["planned-incomplete"]
    assert not incomplete.exists()
    assert completed.exists()
    assert unplanned.exists()
    assert (group / "group.yaml").is_file()


def test_sweep_with_force_also_removes_the_completed_planned_dir(tmp_path):
    """``--force`` means "rebuild everything planned", so a completed planned dir
    must be cleared too — but unplanned work and group.yaml still survive."""
    group = tmp_path / "group"
    group.mkdir()
    (group / "group.yaml").write_text("id: g\n")
    completed = _complete_task_dir(group / "planned-complete")
    unplanned = _complete_task_dir(group / "not-planned")

    removed = B._sweep_incomplete(
        group, planned={"planned-complete"}, done={"planned-complete"}, force=True
    )

    assert removed == ["planned-complete"]
    assert not completed.exists()
    assert unplanned.exists()
    assert (group / "group.yaml").is_file()


def test_sweep_never_removes_group_yaml_even_if_it_were_planned(tmp_path):
    """Defensive: group.yaml is a FILE, and the sweep only considers directories,
    so group-level state is structurally safe."""
    group = tmp_path / "group"
    group.mkdir()
    (group / "group.yaml").write_text("id: g\n")
    removed = B._sweep_incomplete(group, planned={"group.yaml"}, done=set(), force=True)
    assert removed == []
    assert (group / "group.yaml").is_file()


def test_sweep_on_a_missing_group_dir_is_a_no_op(tmp_path):
    assert B._sweep_incomplete(tmp_path / "nope", planned={"x"}, done=set(), force=False) == []


# ===========================================================================
# rank.build_candidates
# ===========================================================================


def _summary(sha, score, *, disqualified=False, **extra):
    base = {
        "sha": sha,
        "subject": f"subject for {sha}",
        "benchmark_score": score,
        "one_line": f"one line for {sha}",
        "proposed_task": "do the thing",
        "proposed_verification": "run one test",
        "est_difficulty": "medium",
        "task_type": ["bug_fix"],
        "confidence": "high",
        "build_attempted": False,
        "build_outcome": "",
        "disqualified": disqualified,
        # Fields that must be PROJECTED AWAY:
        "rationale": "a very long paragraph " * 50,
        "usage": {"input_tokens": 1234, "output_tokens": 567},
        "screened_at": "2024-01-01T00:00:00Z",
        "model_id": "some-model",
        "axes": {
            "verifiability": {"score": 4, "why": "clear test", "gate": True},
            "verify_cost": {"score": 3, "why": "cheap", "gate": False},
        },
    }
    base.update(extra)
    return base


def test_build_candidates_drops_disqualified_entries():
    """Disqualified commits failed a rubric gate and are unusable by
    construction; spending ranking context on them is pure waste."""
    summaries = [
        _summary("a" * 40, 90),
        _summary("b" * 40, 80, disqualified=True),
        _summary("c" * 40, 70),
    ]
    candidates = K.build_candidates(summaries)
    assert [c["sha"] for c in candidates] == ["a" * 40, "c" * 40]


def test_build_candidates_sorts_by_score_descending():
    summaries = [_summary("a" * 40, 40), _summary("b" * 40, 90), _summary("c" * 40, 65)]
    assert [c["benchmark_score"] for c in K.build_candidates(summaries)] == [90, 65, 40]


def test_build_candidates_treats_a_missing_score_as_zero_when_sorting():
    summaries = [_summary("a" * 40, None), _summary("b" * 40, 10)]
    assert [c["sha"] for c in K.build_candidates(summaries)] == ["b" * 40, "a" * 40]


def test_build_candidates_projects_only_the_intended_fields():
    """BUDGET DISCIPLINE: with hundreds of candidates sharing one context, every
    extra field costs budget in proportion to the candidate count. Only the
    declared projection (plus a trimmed ``axes``) may travel."""
    candidates = K.build_candidates([_summary("a" * 40, 90)])
    assert len(candidates) == 1
    assert set(candidates[0]) == set(K.CANDIDATE_FIELDS) | {"axes"}
    for dropped in ("rationale", "usage", "screened_at", "model_id", "disqualified"):
        assert dropped not in candidates[0]


def test_build_candidates_trims_axes_to_score_and_why():
    candidates = K.build_candidates([_summary("a" * 40, 90)])
    axes = candidates[0]["axes"]
    assert set(axes) == {"verifiability", "verify_cost"}
    assert axes["verifiability"] == {"score": 4, "why": "clear test"}
    assert "gate" not in axes["verifiability"]


def test_build_candidates_tolerates_missing_and_malformed_axes():
    summaries = [_summary("a" * 40, 50, axes=None), _summary("b" * 40, 40, axes={"x": 3})]
    candidates = K.build_candidates(summaries)
    assert candidates[0]["axes"] == {}
    assert candidates[1]["axes"] == {}  # non-dict entries are skipped


def test_build_candidates_on_an_empty_input_is_empty():
    assert K.build_candidates([]) == []


def test_build_candidates_all_disqualified_is_empty():
    assert K.build_candidates([_summary("a" * 40, 90, disqualified=True)]) == []


# ===========================================================================
# rank: prompt rendering and exact sizing
# ===========================================================================


def _rank_cfg(tmp_path, n=3, *, max_prompt_tokens=300_000, disqualified=()):
    """A loadable config whose summaries dir holds ``n`` summaries."""
    import yaml

    from abc_mine import config as C

    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "image.sqfs").touch()
    path = tmp_path / "mine.yaml"
    path.write_text(yaml.safe_dump({
        "run": {"id": "t"},
        "codebase": {"name": "Demo", "repo": "repo"},
        "container": {"image": "image.sqfs"},
        "rank": {"max_prompt_tokens": max_prompt_tokens},
    }))
    cfg = C.load(path)
    cfg.summaries_dir.mkdir(parents=True)
    for i in range(n):
        sha = f"{i:x}" * 40
        (cfg.summaries_dir / f"{sha[:12]}.json").write_text(
            json.dumps(_summary(sha, 10 * i, disqualified=i in disqualified))
        )
    return cfg


_MODEL = {"base_url": "http://x", "model": "m", "api": "anthropic_messages"}


def test_render_candidate_carries_every_projected_field():
    """COMPLETENESS: the compact rendering exists to save tokens, not content.
    Every projected field, and every axis's score *and* reasoning, must reach
    the model."""
    (cand,) = K.build_candidates([_summary("a" * 40, 90)])
    text = K.render_candidate(cand)
    assert text.startswith("### " + "a" * 40)
    for value in (cand["subject"], cand["one_line"], cand["proposed_task"],
                  cand["proposed_verification"], "90", "medium", "bug_fix", "high"):
        assert str(value) in text
    assert "- verifiability 4: clear test" in text
    assert "- verify_cost 3: cheap" in text
    for dropped in ("a very long paragraph", "some-model", "2024-01-01"):
        assert dropped not in text


def test_render_candidate_tolerates_missing_fields():
    text = K.render_candidate({"sha": "b" * 40})
    assert "subject: none" in text and "axes:" not in text


def test_render_candidates_keeps_reading_order_and_separates_blocks():
    cands = K.build_candidates([_summary("a" * 40, 10), _summary("b" * 40, 90)])
    text = K.render_candidates(cands)
    assert text.index("b" * 40) < text.index("a" * 40)
    assert text.count("\n\n### ") == 1


def test_rank_prompt_embeds_every_candidate_and_names_no_file(tmp_path):
    """The candidates live IN the prompt. A leftover pointer to a candidates
    file would send the agent looking for something that is not there."""
    from abc_mine import prompts as P

    cfg = _rank_cfg(tmp_path, n=4, disqualified={1})
    work_plan = K.plan(cfg, P.load_pack(None), _MODEL, counter=len)
    assert len(work_plan.candidates) == 3
    for cand in work_plan.candidates:
        assert f"### {cand['sha']}" in work_plan.prompt
    assert "1" * 40 not in work_plan.prompt  # disqualified
    assert "candidates.json" not in work_plan.prompt
    assert "@@" not in work_plan.prompt


def test_plan_counts_the_harness_system_prompt_too(tmp_path):
    """EXACT SIZING: the check must measure what the model receives — the
    harness's system prompt plus the task prompt — not only this stage's part."""
    from abc_mine import prompts as P

    seen = []
    cfg = _rank_cfg(tmp_path)
    work_plan = K.plan(cfg, P.load_pack(None), _MODEL,
                       counter=lambda t: seen.append(t) or 123)
    assert work_plan.prompt_tokens == 123
    (text,) = seen
    assert text.endswith(work_plan.prompt)
    assert K.HARNESS_SYSTEM_PROMPT.read_text() in text


def test_plan_flags_an_over_budget_prompt(tmp_path):
    from abc_mine import prompts as P

    cfg = _rank_cfg(tmp_path, max_prompt_tokens=1000)
    work_plan = K.plan(cfg, P.load_pack(None), _MODEL, counter=lambda t: 1001)
    assert work_plan.over_budget
    assert "1,001" in K._budget_error(work_plan)
    ok = K.plan(cfg, P.load_pack(None), _MODEL, counter=lambda t: 1000)
    assert not ok.over_budget


def test_plan_without_a_budget_counts_nothing(tmp_path):
    from abc_mine import prompts as P

    cfg = _rank_cfg(tmp_path, max_prompt_tokens=None)
    work_plan = K.plan(cfg, P.load_pack(None), _MODEL,
                       counter=lambda t: pytest.fail("counted"))
    assert work_plan.prompt_tokens is None and not work_plan.over_budget


def test_count_tokens_refuses_to_estimate(monkeypatch):
    """NO FALLBACK: an endpoint that cannot count is an error, never a guess."""
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(K.TokenCountError, match="anthropic_messages"):
        K.count_tokens("x", {**_MODEL, "api": "chat_completions"})
    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(K.TokenCountError, match="LLM_API_KEY"):
        K.count_tokens("x", _MODEL)


def test_count_tokens_propagates_client_errors(monkeypatch):
    import io
    import urllib.error

    monkeypatch.setenv("LLM_API_KEY", "k")

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "no", {}, io.BytesIO(b"denied"))

    monkeypatch.setattr(K.urllib.request, "urlopen", refuse)
    with pytest.raises(K.TokenCountError, match="HTTP 403: denied"):
        K.count_tokens("x", _MODEL)


@pytest.mark.parametrize("base", ["https://h", "https://h/", "https://h/v1"])
def test_count_tokens_builds_the_endpoint_url(monkeypatch, base):
    import io

    monkeypatch.setenv("LLM_API_KEY", "k")
    urls = []

    def fake(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(b'{"input_tokens": 42}')

    monkeypatch.setattr(K.urllib.request, "urlopen", fake)
    assert K.count_tokens("x", {**_MODEL, "base_url": base}) == 42
    assert urls == ["https://h/v1/messages/count_tokens"]


def test_old_budget_key_is_rejected_not_ignored(tmp_path):
    """The renamed key bounded a different quantity (an estimate of the payload
    alone); silently accepting it would change what the value means."""
    import yaml

    from abc_mine import config as C

    (tmp_path / "repo").mkdir()
    (tmp_path / "image.sqfs").touch()
    path = tmp_path / "mine.yaml"
    path.write_text(yaml.safe_dump({
        "run": {"id": "t"}, "codebase": {"repo": "repo"},
        "container": {"image": "image.sqfs"},
        "rank": {"max_candidate_tokens": 300000},
    }))
    with pytest.raises(C.ConfigError, match="max_prompt_tokens"):
        C.load(path)


def test_harness_sends_the_prompt_on_stdin_not_as_an_attachment():
    """TRUNCATION: OpenCode reads an attached --file through its Read tool, which
    returns at most 2000 lines / 50 KB. A ranking prompt is megabytes, so it must
    arrive on stdin, which OpenCode appends to the message whole."""
    script = (K.sandbox_mod.HARNESS_DIR / "run_benchmark.sh").read_text()
    run_lines = [l for l in script.splitlines() if not l.lstrip().startswith("#")]
    body = "\n".join(run_lines)
    assert "--file" not in body
    assert body.count('< "${WORKSPACE_PROMPT_FILE}"') == 2
    assert body.count('--title "${SESSION_TITLE}"') == 2


# ===========================================================================
# rank._validate_shortlist
# ===========================================================================


def _plan(*shas, n_disqualified=0):
    return K.RankPlan(
        candidates=[{"sha": s} for s in shas], n_disqualified=n_disqualified
    )


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def test_validate_shortlist_accepts_a_good_shortlist():
    plan = _plan(SHA_A, SHA_B, n_disqualified=3)
    shortlist, error = K._validate_shortlist(
        {"selected": [{"sha": SHA_A, "rank": 1}, {"sha": SHA_B, "rank": 2}]}, plan
    )
    assert error == ""
    assert [e["sha"] for e in shortlist["selected"]] == [SHA_A, SHA_B]
    assert shortlist["n_selected"] == 2
    assert shortlist["n_candidates"] == 2
    assert shortlist["n_disqualified"] == 3
    assert "validation_warnings" not in shortlist


def test_validate_shortlist_resolves_an_abbreviated_sha_prefix():
    """Agents routinely abbreviate SHAs. An unambiguous prefix must resolve to the
    full candidate SHA, since the build stage looks commits up by exact sha."""
    plan = _plan(SHA_A, SHA_B)
    shortlist, error = K._validate_shortlist({"selected": [{"sha": "aaaaaaa"}]}, plan)
    assert error == ""
    assert shortlist["selected"][0]["sha"] == SHA_A


def test_validate_shortlist_rejects_a_sha_not_among_the_candidates():
    """A hallucinated or mistyped SHA would otherwise fail much later, inside the
    build stage, as a confusing git error far from its actual cause."""
    plan = _plan(SHA_A, SHA_B)
    shortlist, error = K._validate_shortlist({"selected": [{"sha": SHA_C}]}, plan)
    assert shortlist is None
    assert "not among the candidates" in error


def test_validate_shortlist_keeps_good_entries_and_warns_about_bad_ones():
    """A partially-hallucinated shortlist is salvaged, but the problem is recorded
    in ``validation_warnings`` so it is never invisible."""
    plan = _plan(SHA_A, SHA_B)
    shortlist, error = K._validate_shortlist(
        {"selected": [{"sha": SHA_C}, {"sha": SHA_A}]}, plan
    )
    assert error == ""
    assert [e["sha"] for e in shortlist["selected"]] == [SHA_A]
    assert shortlist["validation_warnings"]
    assert "not among the candidates" in shortlist["validation_warnings"][0]


def test_validate_shortlist_rejects_an_ambiguous_prefix():
    plan = _plan("abc" + "0" * 37, "abc" + "1" * 37)
    shortlist, error = K._validate_shortlist({"selected": [{"sha": "abc"}]}, plan)
    assert shortlist is None
    assert "not among the candidates" in error


def test_validate_shortlist_renumbers_ranks_densely_from_one():
    """Ranks are renumbered whatever the agent emitted, so downstream consumers
    can rely on a dense 1-based ordering."""
    plan = _plan(SHA_A, SHA_B, SHA_C)
    shortlist, _ = K._validate_shortlist(
        {
            "selected": [
                {"sha": SHA_B, "rank": 50},
                {"sha": SHA_A, "rank": 10},
                {"sha": SHA_C, "rank": 99},
            ]
        },
        plan,
    )
    assert [e["rank"] for e in shortlist["selected"]] == [1, 2, 3]
    # Renumbering preserves the agent's relative ordering.
    assert [e["sha"] for e in shortlist["selected"]] == [SHA_A, SHA_B, SHA_C]


def test_validate_shortlist_deduplicates_repeated_shas():
    """A repeated SHA would build the same task twice, wasting agent budget and
    producing colliding task directories."""
    plan = _plan(SHA_A, SHA_B)
    shortlist, _ = K._validate_shortlist(
        {
            "selected": [
                {"sha": SHA_A, "rank": 1},
                {"sha": SHA_A, "rank": 2, "note": "dup"},
                {"sha": SHA_B, "rank": 3},
            ]
        },
        plan,
    )
    assert [e["sha"] for e in shortlist["selected"]] == [SHA_A, SHA_B]
    assert shortlist["n_selected"] == 2


def test_validate_shortlist_dedupes_across_abbreviated_and_full_shas():
    plan = _plan(SHA_A)
    shortlist, _ = K._validate_shortlist(
        {"selected": [{"sha": "aaaaaaa", "rank": 1}, {"sha": SHA_A, "rank": 2}]}, plan
    )
    assert shortlist["n_selected"] == 1


def test_validate_shortlist_defaults_a_missing_or_bad_rank_to_position():
    plan = _plan(SHA_A, SHA_B)
    shortlist, _ = K._validate_shortlist(
        {"selected": [{"sha": SHA_A}, {"sha": SHA_B, "rank": "second"}]}, plan
    )
    assert [e["rank"] for e in shortlist["selected"]] == [1, 2]


def test_validate_shortlist_rejects_a_non_object_payload():
    shortlist, error = K._validate_shortlist(["a list"], _plan(SHA_A))
    assert shortlist is None
    assert "must be a JSON object" in error


def test_validate_shortlist_rejects_a_missing_selected_array():
    shortlist, error = K._validate_shortlist({"rationale": "words"}, _plan(SHA_A))
    assert shortlist is None
    assert "missing a 'selected' array" in error


def test_validate_shortlist_rejects_entries_without_a_sha():
    shortlist, error = K._validate_shortlist(
        {"selected": [{"rank": 1}, "not an object"]}, _plan(SHA_A)
    )
    assert shortlist is None
    assert "no sha" in error or "not an object" in error


def test_validate_shortlist_preserves_other_top_level_keys():
    plan = _plan(SHA_A)
    shortlist, _ = K._validate_shortlist(
        {"selected": [{"sha": SHA_A}], "notes": "agent notes"}, plan
    )
    assert shortlist["notes"] == "agent notes"


def test_validate_shortlist_accepts_an_empty_selection():
    """Selecting nothing is a legitimate (if disappointing) verdict."""
    shortlist, error = K._validate_shortlist({"selected": []}, _plan(SHA_A))
    assert error == ""
    assert shortlist["n_selected"] == 0


# ===========================================================================
# manifest attempt tracking
# ===========================================================================


def test_attempts_round_trip(tmp_path):
    path = tmp_path / "attempts.jsonl"
    assert M.read_attempts(path) == []

    M.record_attempt(path, "item-1", "failed", detail="boom", model_id="m1")
    M.record_attempt(path, "item-1", "ok", model_id="m1", seconds=12.5)
    M.record_attempt(path, "item-2", "failed", detail="also boom")

    records = M.read_attempts(path)
    assert len(records) == 3
    assert records[0]["item_id"] == "item-1"
    assert records[0]["status"] == "failed"
    assert records[0]["detail"] == "boom"
    assert records[0]["model_id"] == "m1"
    assert records[0]["at"]
    assert records[1]["seconds"] == 12.5


def test_record_attempt_creates_parent_directories(tmp_path):
    path = tmp_path / "a" / "b" / "attempts.jsonl"
    M.record_attempt(path, "i", "ok")
    assert path.is_file()


def test_attempt_counts_total_and_filtered(tmp_path):
    """COUNTABILITY: the predecessor pipeline wrote nothing on failure, so "never
    attempted" and "failed five times" were indistinguishable and retries were
    unbounded across resumes."""
    path = tmp_path / "attempts.jsonl"
    for _ in range(3):
        M.record_attempt(path, "flaky", "failed")
    M.record_attempt(path, "flaky", "ok")
    M.record_attempt(path, "fine", "ok")

    assert M.attempt_counts(path) == {"flaky": 4, "fine": 1}
    assert M.attempt_counts(path, status="failed") == {"flaky": 3}
    assert M.attempt_counts(path, status="ok") == {"flaky": 1, "fine": 1}


def test_attempt_counts_ignores_records_without_an_item_id(tmp_path):
    path = tmp_path / "attempts.jsonl"
    M.record_attempt(path, "", "failed")
    assert M.attempt_counts(path) == {}


def test_read_attempts_skips_malformed_lines(tmp_path):
    """A log truncated by a killed job must not prevent the resume it exists to
    support."""
    path = tmp_path / "attempts.jsonl"
    path.write_text(
        '{"item_id": "a", "status": "failed"}\n'
        "garbage\n"
        "\n"
        '{"item_id": "b", "status": "failed"\n'
        '{"item_id": "c", "status": "failed"}\n'
    )
    assert [r["item_id"] for r in M.read_attempts(path)] == ["a", "c"]
    assert M.attempt_counts(path, status="failed") == {"a": 1, "c": 1}


def test_exhausted_items_respects_max_attempts(tmp_path):
    """Bounded retries stop one pathological commit from burning the whole budget
    on every resume."""
    path = tmp_path / "attempts.jsonl"
    for _ in range(3):
        M.record_attempt(path, "hopeless", "failed")
    M.record_attempt(path, "once", "failed")

    assert M.exhausted_items(path, max_attempts=2) == {"hopeless": 3}
    assert M.exhausted_items(path, max_attempts=1) == {"hopeless": 3, "once": 1}
    assert M.exhausted_items(path, max_attempts=4) == {}


def test_exhausted_items_counts_only_failures(tmp_path):
    path = tmp_path / "attempts.jsonl"
    for _ in range(5):
        M.record_attempt(path, "busy", "ok")
    M.record_attempt(path, "busy", "skipped")
    assert M.exhausted_items(path, max_attempts=2) == {}


def test_exhausted_items_on_a_missing_log_is_empty(tmp_path):
    assert M.exhausted_items(tmp_path / "nope.jsonl", max_attempts=1) == {}


# ===========================================================================
# manifest._severity — DRIFT SEVERITY (fail-closed)
# ===========================================================================


@pytest.mark.parametrize(
    "key",
    [
        "model.id",
        "rubric",
        "rubric.axes",
        "codebase.repo",
        "codebase.name",
        "codebase.source_mount",
        "selection",
        "selection.mode",
        "prompts.pack_digest",
        "prompts.overrides",
        "toolchain.test_one",
        "toolchain.env",
        "model.api",
    ],
)
def test_severity_critical_keys(key):
    """CRITICAL: these change *how items are judged*, so a run that mixed them
    would produce incomparable scores with nothing in the output saying so.
    ``model.id`` is deliberately critical: screening half a history with a cheap
    model and half with a frontier model is exactly that failure."""
    assert M._severity(key) == "critical"


@pytest.mark.parametrize(
    "key",
    [
        "execution.workers",
        "execution.parallel",
        "execution.max_attempts",
        "execution.keep_logs",
        "execution.timeout_seconds",
        "execution.sandboxed",
        "container.image",
        "container.image_digest",
        "rank",
        "rank.max_candidates",
        "toolchain.full_build_seconds",
        "toolchain.targeted_test_seconds",
    ],
)
def test_severity_warning_keys(key):
    """WARNING: throughput and retention only. Results stay comparable, so these
    are recorded but never block a resume."""
    assert M._severity(key) == "warning"


@pytest.mark.parametrize("key", ["model.base_url", "execution.scratch_dir"])
def test_severity_exempt_keys(key):
    """EXEMPT: values that legitimately rotate between Slurm allocations."""
    assert M._severity(key) == "exempt"


@pytest.mark.parametrize(
    "key",
    ["something.new", "totally_unknown", "future.feature.flag", "", "model"],
)
def test_severity_unknown_keys_are_critical(key):
    """FAIL-CLOSED: a key added in a future version must block a resume rather
    than slip through unnoticed. Defaulting unknown keys to 'warning' would let a
    newly-significant setting change mid-run silently."""
    assert M._severity(key) == "critical"


def test_severity_prefix_matching_does_not_overreach():
    """``model.base_url`` is exempt but ``model.base_url_extra`` is not a real
    child of it, so prefix matching must be on dotted boundaries."""
    assert M._severity("model.base_url.host") == "exempt"
    assert M._severity("model.base_urlish") == "critical"


def test_every_declared_key_table_is_classified_as_declared():
    """Guards against a key appearing in two tables with the earlier one winning
    by accident."""
    for key in M.CRITICAL_KEYS:
        assert M._severity(key) == "critical", key
    for key in M.WARNING_KEYS:
        assert M._severity(key) == "warning", key
    for key in M.EXEMPT_KEYS:
        assert M._severity(key) == "exempt", key


# ===========================================================================
# preflight._looks_like_no_tests_ran
# ===========================================================================


@pytest.mark.parametrize(
    "log",
    [
        "No tests were found\n",
        "NO TESTS WERE FOUND matching the filter\n",
        "collected 0 items\n\n============ no tests ran in 0.01s =====\n",
        "Total Tests: 0\n",
        "total tests: 0\n",
        "No tests to run.\n",
        "Ran 0 tests in 0.000s\n\nOK\n",
        "0 tests were selected\n",
        "no tests ran in 0.02s\n",
    ],
)
def test_looks_like_no_tests_ran_true(tmp_path, log):
    """SAFETY HEURISTIC: an exit-0 run that tested nothing is the one failure mode
    that would otherwise propagate all the way into shipped verifiers that pass
    VACUOUSLY — certifying solvers that did nothing. False positives are cheap
    (pick a better selector); a false negative can poison a whole suite."""
    path = tmp_path / "test_one.log"
    path.write_text(log)
    assert PF._looks_like_no_tests_ran(path) is True


@pytest.mark.parametrize(
    "log",
    [
        "collected 12 items\n\ntest_solver.py ........\n\n12 passed in 1.24s\n",
        "Test project /tmp/build\n    Start 1: solver_unit\n"
        "1/1 Test #1: solver_unit .......   Passed    0.31 sec\n\n"
        "100% tests passed, 0 tests failed out of 1\n",
        "Ran 3 tests in 0.012s\n\nOK\n",
        "Total Tests: 47\nPassed: 47\n",
        "compiling...\nlinking...\n",
        "",
    ],
)
def test_looks_like_no_tests_ran_false(tmp_path, log):
    """A log showing real tests actually running must NOT be flagged, or preflight
    would reject a perfectly good selector."""
    path = tmp_path / "test_one.log"
    path.write_text(log)
    assert PF._looks_like_no_tests_ran(path) is False


def test_looks_like_no_tests_ran_on_missing_or_none_is_false(tmp_path):
    assert PF._looks_like_no_tests_ran(None) is False
    assert PF._looks_like_no_tests_ran(tmp_path / "absent.log") is False


def test_looks_like_no_tests_ran_is_case_insensitive(tmp_path):
    path = tmp_path / "log"
    path.write_text("COLLECTED 0 ITEMS\n")
    assert PF._looks_like_no_tests_ran(path) is True


def test_looks_like_no_tests_ran_tolerates_undecodable_bytes(tmp_path):
    """Container logs can contain arbitrary bytes; the check must not crash."""
    path = tmp_path / "log"
    path.write_bytes(b"\xff\xfe binary noise \x00 collected 0 items\n")
    assert PF._looks_like_no_tests_ran(path) is True


# ===========================================================================
# preflight.check_config — static, no container
# ===========================================================================


def test_check_config_flags_a_toolchain_with_no_commands(tmp_path):
    from abc_mine.config import (
        CodebaseConfig,
        ContainerConfig,
        MineConfig,
        ToolchainConfig,
    )

    cfg = MineConfig(
        run_id="r",
        data_root=tmp_path,
        codebase=CodebaseConfig(name="X", repo=tmp_path),
        container=ContainerConfig(image=tmp_path / "i.sqfs"),
        toolchain=ToolchainConfig(),
    )
    problems = PF.check_config(cfg)
    assert any("no commands at all" in p for p in problems)
    assert any("test_one is not set" in p for p in problems)


def test_check_config_flags_a_missing_preflight_selector(tmp_path):
    """Without a selector, preflight cannot prove ``test_one`` selects a real
    test — and a selector that silently matches nothing is worse than one that
    errors."""
    from abc_mine.config import (
        CodebaseConfig,
        ContainerConfig,
        MineConfig,
        ToolchainConfig,
    )

    cfg = MineConfig(
        run_id="r",
        data_root=tmp_path,
        codebase=CodebaseConfig(name="X", repo=tmp_path),
        container=ContainerConfig(image=tmp_path / "i.sqfs"),
        toolchain=ToolchainConfig(build="make", test_one="ctest -R '{selector}'"),
    )
    problems = PF.check_config(cfg)
    assert any("preflight_selector is not set" in p for p in problems)
    assert not any("no commands at all" in p for p in problems)


def test_check_config_is_clean_for_a_complete_toolchain(tmp_path):
    from abc_mine.config import (
        CodebaseConfig,
        ContainerConfig,
        MineConfig,
        ToolchainConfig,
    )

    cfg = MineConfig(
        run_id="r",
        data_root=tmp_path,
        codebase=CodebaseConfig(name="X", repo=tmp_path),
        container=ContainerConfig(image=tmp_path / "i.sqfs"),
        toolchain=ToolchainConfig(
            build="make",
            test="ctest",
            test_one="ctest -R '{selector}'",
            preflight_selector="solver_unit",
        ),
    )
    assert PF.check_config(cfg) == []


# ---------------------------------------------------------------------------
# Startup diagnostics (regression guards)
# ---------------------------------------------------------------------------


def test_launcher_rank_reads_slurm_procid(monkeypatch):
    """Rank detection must come from the launcher's environment, not mpi4py.

    It is used on the error path, where MPI may be precisely what is broken, so it
    must not import anything that could fail.
    """
    from abc_mine import cli

    for var in ("SLURM_PROCID", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "MV2_COMM_WORLD_RANK"):
        monkeypatch.delenv(var, raising=False)
    assert cli._launcher_rank() == 0

    monkeypatch.setenv("SLURM_PROCID", "7")
    assert cli._launcher_rank() == 7

    # A non-numeric value must not raise; it falls through to the next source.
    monkeypatch.setenv("SLURM_PROCID", "not-a-number")
    monkeypatch.setenv("PMI_RANK", "3")
    assert cli._launcher_rank() == 3


def test_startup_errors_are_reported_once_under_mpi(monkeypatch, capsys):
    """Only rank 0 prints a startup error.

    A 32-rank job hitting a configuration problem previously produced 32 identical
    errors, which buries the diagnosis it was meant to provide. Every rank still
    exits non-zero, so the job status is unchanged.
    """
    from abc_mine import cli

    monkeypatch.setenv("SLURM_PROCID", "0")
    cli._report_error("boom")
    assert "boom" in capsys.readouterr().err

    monkeypatch.setenv("SLURM_PROCID", "4")
    cli._report_error("boom")
    assert capsys.readouterr().err == ""


def test_missing_manifest_error_names_the_config_and_notes_leftovers(tmp_path):
    """The uninitialized-run error must be actionable from a job log alone.

    It names the real config path (a reader of a job log may not know which config
    was used) and, when the run directory holds other outputs, says so -- that case
    means results exist whose settings were never recorded.
    """
    import pytest
    from abc_mine import config as config_mod, manifest as manifest_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "img.sqfs"
    image.touch()
    cfg_path = tmp_path / "mine.yaml"
    cfg_path.write_text(
        "run:\n  id: r1\n  data_root: data\n"
        f"codebase:\n  name: C\n  repo: {repo}\n"
        f"container:\n  image: {image}\n"
        "toolchain:\n  build: b\n  test_one: t {selector}\n"
    )
    cfg = config_mod.load(cfg_path)

    with pytest.raises(manifest_mod.ManifestError) as exc:
        manifest_mod.load(cfg)
    assert "not initialized" in str(exc.value)
    assert str(cfg_path) in str(exc.value)

    # With unrelated outputs present, the message must call that out.
    cfg.run_dir.mkdir(parents=True)
    (cfg.run_dir / "preflight.json").write_text("{}")
    with pytest.raises(manifest_mod.ManifestError) as exc2:
        manifest_mod.load(cfg)
    assert "preflight.json" in str(exc2.value)


def test_resource_env_is_recorded_but_never_gates_a_resume(monkeypatch):
    """Resource settings are provenance, not configuration.

    BUILD_JOBS, CPU counts, and worker counts change how fast a run goes, not what
    it decides, so a differently-sized allocation must not block a resume -- that
    would defeat the purpose of resumability. They are still recorded, because a
    build's measured duration depends on its width and that duration reaches the
    agents through the verify_cost axis.
    """
    from abc_mine import manifest as manifest_mod

    for var in manifest_mod.RESOURCE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Unset variables are omitted rather than reported as defaults.
    assert manifest_mod.resource_environment() == {}

    monkeypatch.setenv("BUILD_JOBS", "8")
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "32")
    captured = manifest_mod.resource_environment()
    assert captured == {"SLURM_CPUS_PER_TASK": "32", "BUILD_JOBS": "8"}

    # None of these may appear in the drift fingerprint's severity tables, or a
    # resume on a different allocation would be refused.
    for name in manifest_mod.RESOURCE_ENV_VARS:
        assert name not in manifest_mod.CRITICAL_KEYS
        assert name not in manifest_mod.WARNING_KEYS


def test_measured_timings_are_warning_not_critical_drift():
    """A re-measured build time must not block a resume, but must be reported.

    The figure is rendered into every prompt and shapes the verify_cost axis, so a
    change is worth surfacing -- but it reflects the allocation that measured it,
    not a decision about how commits are judged, so it cannot be a hard gate.
    """
    from abc_mine import manifest as manifest_mod

    assert manifest_mod._severity("toolchain.full_build_seconds") == "warning"
    assert manifest_mod._severity("toolchain.targeted_test_seconds") == "warning"
    # By contrast, the commands themselves are critical: changing what 'build' means
    # does change how commits are judged.
    assert manifest_mod._severity("toolchain.build") == "critical"
    assert manifest_mod._severity("toolchain.test_one") == "critical"


def test_manifest_save_is_safe_under_concurrent_writers(tmp_path, monkeypatch):
    """Concurrent saves must not fail on a shared temp filename.

    Regression guard: save() used a single run.json.tmp, so under a parallel
    launcher every rank wrote the same path and renamed it. The first rename won and
    the rest died with FileNotFoundError -- their temp file had been moved out from
    under them. That killed 7 of 8 ranks in a real job, leaving the survivor blocked
    in an MPI recv waiting for a coordinator that no longer existed, so the
    allocation hung until its wall clock expired.
    """
    import json
    import os
    from concurrent.futures import ThreadPoolExecutor
    from abc_mine import manifest as manifest_mod

    path = tmp_path / "run.json"

    def save_once(i: int) -> None:
        m = manifest_mod.Manifest(path=path, data={"schema": 1, "writer": i})
        m.save()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save_once, range(8)))

    # One valid manifest, and no temp files left beside it.
    assert json.loads(path.read_text())["schema"] == 1
    assert not list(tmp_path.glob("*.tmp*"))


def test_only_rank_zero_persists_stage_records(tmp_path, monkeypatch):
    """A parallel stage invocation appends one record, not one per rank.

    Every rank keeps the record in memory so nothing downstream diverges, but only
    rank 0 writes -- otherwise N ranks would append N records for a single logical
    invocation and serialize through one file on a shared filesystem.
    """
    import json
    from abc_mine import manifest as manifest_mod

    path = tmp_path / "run.json"
    path.write_text(json.dumps({"schema": 1, "stages": []}))

    for rank in range(4):
        monkeypatch.setenv("SLURM_PROCID", str(rank))
        # Each rank is a separate process in reality, so each starts from the
        # on-disk state rather than sharing an object.
        man = manifest_mod.Manifest(path=path, data=json.loads(path.read_text()))
        before = len(man.data["stages"])
        man.record_stage("screen", {"model_id": "m"})
        # The record is present in memory on every rank.
        assert len(man.data["stages"]) == before + 1

    # Only rank 0's write reached disk.
    assert len(json.loads(path.read_text())["stages"]) == 1
