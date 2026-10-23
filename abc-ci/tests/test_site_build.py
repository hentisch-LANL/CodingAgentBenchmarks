import importlib.util
from pathlib import Path


_BUILD_PATH = Path(__file__).resolve().parent.parent / "site" / "build.py"
_SPEC = importlib.util.spec_from_file_location("abc_ci_site_build", _BUILD_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

aggregate_runs = _MODULE.aggregate_runs
generate_leaderboard_html = _MODULE.generate_leaderboard_html
_dimension_views = _MODULE._dimension_views
generate_model_detail_page = _MODULE.generate_model_detail_page
generate_harness_detail_page = _MODULE.generate_harness_detail_page
generate_models_index_page = _MODULE.generate_models_index_page
model_display_name = _MODULE.model_display_name
harness_display_name = _MODULE.harness_display_name
agent_label = _MODULE.agent_label
_bootstrap_mean_ci = getattr(_MODULE, "_bootstrap_mean_ci", None)
_wilson_proportion_ci = _MODULE._wilson_proportion_ci
_mean_ci = _MODULE._mean_ci
_extract_total_tokens = _MODULE._extract_total_tokens
_extract_tokens_per_second = _MODULE._extract_tokens_per_second


def test_build_empty_archive_with_public_catalog(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    output = tmp_path / "new-output"
    monkeypatch.setenv("ABC_BENCH_ARCHIVE_DIR", str(archive))
    monkeypatch.setenv("SITE_OUTPUT_DIR", str(output))
    assert _MODULE.main() == 0
    assert "ABC-Bench Leaderboard" in (output / "index.html").read_text()
    assert (output / "models.html").is_file()


def _run(agent: str, task_ref: str, status: str, total_cost, ended_at: str, elapsed_seconds: float | None = None, harness: str = "opencode", models: list[str] | None = None, total_tokens: float | None = None, tokens_per_second: float | None = None) -> dict:
    models = models if models is not None else ["model-a"]
    metadata = {
        "task_ref": task_ref,
        "ended_at": ended_at,
        "agent": {
            "name": agent,
            "harness": harness,
            "models": models,
        },
    }
    if elapsed_seconds is not None:
        metadata["elapsed_seconds"] = elapsed_seconds
    result = {
        "task_ref": task_ref,
        "agent": agent,
        "harness": harness,
        "models": models,
        "status": status,
    }
    if total_cost is not None:
        result["total_cost"] = total_cost
    if total_tokens is not None or tokens_per_second is not None:
        totals: dict = {}
        if total_tokens is not None:
            totals["total_tokens"] = total_tokens
        if tokens_per_second is not None:
            totals["tokens_per_second"] = tokens_per_second
        result["usage"] = {"totals": totals}
    return {
        "run_dir": Path(f"/tmp/{agent}-{task_ref}"),
        "metadata": metadata,
        "result": result,
    }


def test_aggregate_runs_uses_ran_benchmarks_for_pass_rate_and_completeness() -> None:
    runs = [
        _run("agent-a", "task-1", "PASS", 1.25, "2026-01-01T00:00:00", elapsed_seconds=61.4),
        _run("agent-a", "task-2", "FAIL", 2.75, "2026-01-01T00:01:00", elapsed_seconds=118.6),
        _run("agent-b", "task-1", "PASS", 3.0, "2026-01-01T00:02:00", elapsed_seconds=3600.1),
    ]

    aggregated = aggregate_runs(runs)

    agent_a = aggregated["agents"]["agent-a"]
    agent_b = aggregated["agents"]["agent-b"]

    assert agent_a["pass_rate"] == 50.0
    # Wilson 95% interval for 1/2 (not the degenerate Wald/bootstrap [0,100]).
    lo_a, hi_a = agent_a["pass_rate_ci"]
    assert abs(lo_a - 9.453120573423075) < 1e-6
    assert abs(hi_a - 90.54687942657694) < 1e-6
    assert agent_a["completeness"] == 100.0
    assert agent_a["average_cost"] == 2.0
    assert agent_a["average_walltime"] == 90.0

    assert agent_b["pass_rate"] == 100.0
    # Wilson interval for 1/1 has a nonzero lower bound (Wald would give 100/100).
    lo_b, hi_b = agent_b["pass_rate_ci"]
    assert abs(lo_b - 20.654931437723747) < 1e-6
    assert hi_b == 100.0
    assert agent_b["completeness"] == 50.0
    assert agent_b["average_cost"] == 3.0
    assert agent_b["average_walltime"] == 3600.1


def test_aggregate_runs_pools_all_attempts_flat() -> None:
    # Two tasks, multiple attempts each, for one agent. Pass rate and CI pool
    # every attempt (not one latest run per task).
    runs = [
        _run("agent-a", "task-1", "PASS", 1.0, "2026-01-01T00:00:00", elapsed_seconds=10.0),
        _run("agent-a", "task-1", "FAIL", 1.0, "2026-01-01T00:01:00", elapsed_seconds=10.0),
        _run("agent-a", "task-1", "PASS", 1.0, "2026-01-01T00:02:00", elapsed_seconds=10.0),
        _run("agent-a", "task-2", "PASS", 1.0, "2026-01-01T00:03:00", elapsed_seconds=10.0),
        _run("agent-a", "task-2", "FAIL", 1.0, "2026-01-01T00:04:00", elapsed_seconds=10.0),
    ]

    aggregated = aggregate_runs(runs)
    agent_a = aggregated["agents"]["agent-a"]

    # 3 of 5 attempts passed -> 60%, pooled flat over all attempts.
    assert agent_a["attempt_count"] == 5
    assert agent_a["passed_count"] == 3
    assert agent_a["pass_rate"] == 60.0
    # Two distinct tasks were run.
    assert agent_a["ran_count"] == 2
    # Per-task rollup carries attempt counts + pass rate.
    assert agent_a["tasks"]["task-1"]["attempts"] == 3
    assert agent_a["tasks"]["task-1"]["passed_count"] == 2
    assert abs(agent_a["tasks"]["task-1"]["pass_rate"] - (2 / 3 * 100.0)) < 1e-6
    assert agent_a["tasks"]["task-2"]["attempts"] == 2
    # A Wilson interval is computed from the 3/5 proportion and brackets it.
    assert agent_a["pass_rate_ci"] is not None
    low, high = agent_a["pass_rate_ci"]
    assert 0.0 <= low <= 60.0 <= high <= 100.0


def test_generate_leaderboard_html_shows_new_columns_and_cost_formatting(tmp_path: Path) -> None:
    runs = [
        _run("agent-a", "task-1", "PASS", 0.1234, "2026-01-01T00:00:00", elapsed_seconds=61.4),
        _run("agent-a", "task-2", "FAIL", 0.2468, "2026-01-01T00:01:00", elapsed_seconds=118.6),
        _run("agent-b", "task-1", "PASS", 1.0, "2026-01-01T00:02:00", elapsed_seconds=3600.1),
    ]
    aggregated = aggregate_runs(runs)

    output_file = tmp_path / "index.html"
    generate_leaderboard_html(aggregated, {}, output_file)
    html = output_file.read_text(encoding="utf-8")

    assert "Pass<br>Rate" in html
    assert ">Harness<" in html
    assert ">Models<" in html
    assert ">Agent<" not in html
    assert ">Harness | Models<" not in html
    assert ">95% CI<" not in html
    assert ">Completeness<" in html
    assert "Avg<br>Cost" in html
    assert "Avg<br>Walltime" in html
    assert "Avg<br>Tokens" in html
    assert "Avg<br>Tok/s" in html
    assert 'class="stat"' in html
    assert "data-tip=" in html
    assert ">opencode<" in html
    assert ">model-a<" in html
    assert "[9%, 91%]" in html  # Wilson interval for agent-a (1/2)
    assert "[21%, 100%]" in html  # Wilson interval for agent-b (1/1)
    assert "$0.19" in html
    assert "$1.00" in html
    assert "0:01:30" in html
    assert "1:00:00" in html
    assert ">100%<" in html
    assert ">50%<" in html

    assert 'data-sort-type="text"' in html
    assert 'data-sort-type="number"' in html
    assert 'data-sort-value=' in html
    assert 'sort.js' in html
    assert 'groups.js' in html
    assert 'class="sort-button ' in html
    assert 'aria-sort="descending"' in html
    assert 'data-sort-label="Sort by pass rate"' in html
    assert 'data-sort-default="descending"' in html
    assert 'group-cell' in html
    assert 'task-col hidden' in html
    assert 'data-group-name=' in html
    assert 'group-sort-btn' in html
    assert 'class="numeric group-header"' in html
    assert 'task-header hidden' in html


def test_generate_leaderboard_html_has_visualization_selector_and_views(tmp_path: Path) -> None:
    runs = [
        _run("agent-a", "task-1", "PASS", 0.1234, "2026-01-01T00:00:00", elapsed_seconds=61.4),
        _run("agent-a", "task-2", "FAIL", 0.2468, "2026-01-01T00:01:00", elapsed_seconds=118.6),
        _run("agent-b", "task-1", "PASS", 1.0, "2026-01-01T00:02:00", elapsed_seconds=3600.1),
    ]
    aggregated = aggregate_runs(runs)

    output_file = tmp_path / "index.html"
    generate_leaderboard_html(aggregated, {}, output_file)
    html = output_file.read_text(encoding="utf-8")

    # Radio selector with table + plot modes.
    assert 'id="viz-selector"' in html
    assert 'name="viz" value="table"' in html
    assert 'name="viz" value="plot"' in html
    assert 'type="radio"' in html

    # View containers.
    assert 'id="view-table"' in html
    assert 'id="view-plot"' in html
    assert 'id="plot-container"' in html

    # Embedded plot data and its scripts.
    assert 'id="agent-data"' in html
    assert 'type="application/json"' in html
    assert '"pass_rate"' in html
    assert '"ci_low"' in html
    assert '"ci_high"' in html
    assert 'plot.js' in html
    assert 'viz.js' in html

    # Removed filter fields.
    assert 'filter-min-pass' not in html
    assert 'filter-min-completeness' not in html


def test_leaderboard_has_group_selector_and_recompute_data(tmp_path: Path) -> None:
    # Two distinct groups via task_definitions so the selector has >1 checkbox.
    task_defs = {
        "alpha-1": {"group": "Alpha"},
        "beta-1": {"group": "Beta"},
    }
    runs = [
        _run("agent-a", "alpha-1", "PASS", 1.0, "2026-01-01T00:00:00", elapsed_seconds=10.0, total_tokens=100, tokens_per_second=5.0),
        _run("agent-a", "beta-1", "FAIL", 2.0, "2026-01-01T00:01:00", elapsed_seconds=20.0, total_tokens=200, tokens_per_second=6.0),
    ]
    aggregated = aggregate_runs(runs, task_defs)

    # Per-group raw breakdown is retained for client recomputation.
    gb = aggregated["agents"]["agent-a"]["group_breakdown"]
    assert set(gb.keys()) == {"Alpha", "Beta"}
    assert gb["Alpha"]["attempt_count"] == 1
    assert gb["Alpha"]["passed_count"] == 1
    assert gb["Beta"]["passed_count"] == 0

    output_file = tmp_path / "index.html"
    generate_leaderboard_html(aggregated, task_defs, output_file)
    html = output_file.read_text(encoding="utf-8")

    # Dedicated group selector panel with a checkbox per group.
    assert 'id="group-selector"' in html
    assert 'class="group-checkbox"' in html
    assert 'value="Alpha"' in html
    assert 'value="Beta"' in html
    assert 'id="group-select-all"' in html
    assert 'id="group-select-none"' in html

    # Client recompute script is loaded.
    assert 'recompute.js' in html

    # Cells carry data-col markers so recompute.js can target them.
    assert 'data-col="pass_rate"' in html
    assert 'data-col="completeness"' in html
    assert 'data-col="cost"' in html
    assert 'data-col="group"' in html

    # Rows carry the agent key and embedded per-group raw data.
    assert 'data-agent="agent-a"' in html
    assert '"groups":' in html
    assert '"passed_count"' in html

    # Updated footnote reflects Wilson + normal-approximation, no bootstrap.
    assert 'Wilson score' in html
    assert 'bootstrap' not in html.lower()


_MODEL_DEFS = {
    "gpt": {"name": "GPT-5.4", "provider": "OpenAI", "logo": "logos/openai.svg", "description": "Frontier model."},
    "gemma": {"name": "Gemma 4 31B", "provider": "Google", "logo": "logos/gemma.png", "description": "Local model."},
}
_HARNESS_DEFS = {
    "opencode": {"name": "OpenCode", "description": "Single-model harness."},
    "opencode-hybrid": {"name": "OpenCode (Hybrid)", "description": "Two-model harness."},
}


def _mixed_runs() -> list[dict]:
    return [
        _run("agent-gpt", "task-1", "PASS", 1.0, "2026-01-01T00:00:00", harness="opencode", models=["gpt"]),
        _run("agent-gpt", "task-2", "FAIL", 1.0, "2026-01-01T00:01:00", harness="opencode", models=["gpt"]),
        _run("agent-hybrid", "task-1", "PASS", 1.0, "2026-01-01T00:02:00", harness="opencode-hybrid", models=["gpt", "gemma"]),
    ]


def test_display_helpers_fall_back_to_id() -> None:
    assert model_display_name("gpt", _MODEL_DEFS) == "GPT-5.4"
    assert model_display_name("unknown", _MODEL_DEFS) == "unknown"
    assert harness_display_name("opencode", _HARNESS_DEFS) == "OpenCode"
    assert harness_display_name("mystery", _HARNESS_DEFS) == "mystery"


def test_agent_label_format() -> None:
    # single model
    assert agent_label("opencode", ["gpt"], _HARNESS_DEFS, _MODEL_DEFS) == "OpenCode with GPT-5.4"
    # two models joined with "and"
    assert (
        agent_label("opencode-hybrid", ["gpt", "gemma"], _HARNESS_DEFS, _MODEL_DEFS)
        == "OpenCode (Hybrid) with GPT-5.4 and Gemma 4 31B"
    )
    # three models use oxford-style comma list
    assert (
        agent_label("opencode", ["gpt", "gemma", "gpt"], _HARNESS_DEFS, _MODEL_DEFS)
        == "OpenCode with GPT-5.4, Gemma 4 31B, and GPT-5.4"
    )
    # no models falls back to harness name only
    assert agent_label("opencode", [], _HARNESS_DEFS, _MODEL_DEFS) == "OpenCode"


def test_dimension_views_group_by_model_and_harness() -> None:
    aggregated = aggregate_runs(_mixed_runs())
    dims = _dimension_views(aggregated)

    # gpt is used by both agents; gemma only by the hybrid agent.
    assert set(dims["models"].keys()) == {"gpt", "gemma"}
    assert dims["models"]["gpt"]["agent_count"] == 2
    assert dims["models"]["gemma"]["agent_count"] == 1
    assert dims["models"]["gpt"]["harnesses"] == {"opencode", "opencode-hybrid"}

    # two distinct harnesses
    assert set(dims["harnesses"].keys()) == {"opencode", "opencode-hybrid"}
    assert dims["harnesses"]["opencode-hybrid"]["models"] == {"gpt", "gemma"}


def test_generate_model_detail_page_uses_metadata(tmp_path: Path) -> None:
    aggregated = aggregate_runs(_mixed_runs())
    dims = _dimension_views(aggregated)
    generate_model_detail_page("gpt", dims["models"]["gpt"], tmp_path, _MODEL_DEFS, _HARNESS_DEFS)

    html = (tmp_path / "models" / "gpt.html").read_text(encoding="utf-8")
    assert "<title>GPT-5.4 - abc-ci</title>" in html
    assert "Frontier model." in html
    assert "OpenAI" in html
    assert 'src="../logos/openai.svg"' in html
    # links back to harness pages
    assert 'href="../harnesses/opencode.html"' in html
    assert 'href="../harnesses/opencode-hybrid.html"' in html
    # raw id preserved as provenance
    assert ">gpt<" in html


def test_generate_harness_detail_page_lists_models(tmp_path: Path) -> None:
    aggregated = aggregate_runs(_mixed_runs())
    dims = _dimension_views(aggregated)
    generate_harness_detail_page("opencode-hybrid", dims["harnesses"]["opencode-hybrid"], tmp_path, _HARNESS_DEFS, _MODEL_DEFS)

    html = (tmp_path / "harnesses" / "opencode-hybrid.html").read_text(encoding="utf-8")
    assert "<title>OpenCode (Hybrid) - abc-ci</title>" in html
    assert "Two-model harness." in html
    assert 'href="../models/gpt.html"' in html
    assert 'href="../models/gemma.html"' in html


def test_generate_models_index_page_links_and_logos(tmp_path: Path) -> None:
    aggregated = aggregate_runs(_mixed_runs())
    dims = _dimension_views(aggregated)
    generate_models_index_page(dims["models"], tmp_path, _MODEL_DEFS)

    html = (tmp_path / "models.html").read_text(encoding="utf-8")
    assert 'href="models/gpt.html"' in html
    assert 'href="models/gemma.html"' in html
    assert 'src="logos/openai.svg"' in html
    assert "GPT-5.4" in html


def test_leaderboard_links_models_and_harnesses(tmp_path: Path) -> None:
    aggregated = aggregate_runs(_mixed_runs())
    output_file = tmp_path / "index.html"
    generate_leaderboard_html(aggregated, {}, output_file, _MODEL_DEFS, _HARNESS_DEFS)
    html = output_file.read_text(encoding="utf-8")

    assert 'href="models.html"' in html  # nav link
    assert 'href="harnesses/opencode.html"' in html
    assert 'href="models/gpt.html"' in html
    assert 'class="model-chip"' in html
    assert 'src="logos/openai.svg"' in html
    # display name shown, raw id retained for filtering
    assert "GPT-5.4" in html
    assert 'data-models="gpt"' in html or 'data-models="gpt, gemma"' in html
    # plot data carries the natural agent label
    assert '"label": "OpenCode with GPT-5.4"' in html
    assert '"label": "OpenCode (Hybrid) with GPT-5.4 and Gemma 4 31B"' in html


def test_wilson_proportion_ci_edge_cases() -> None:
    # No trials -> no interval.
    assert _wilson_proportion_ci(0, 0) is None

    # All-pass and all-fail single trial: bounded away from the degenerate
    # zero-width Wald interval, and clamped to [0, 1].
    lo1, hi1 = _wilson_proportion_ci(1, 1)
    assert 0.0 < lo1 < 1.0
    assert hi1 == 1.0
    lo0, hi0 = _wilson_proportion_ci(0, 1)
    assert lo0 == 0.0
    assert 0.0 < hi0 < 1.0

    # Interior proportion brackets the point estimate and stays in range.
    lo, hi = _wilson_proportion_ci(1, 2)
    assert 0.0 <= lo <= 0.5 <= hi <= 1.0


def test_mean_ci_edge_cases() -> None:
    assert _mean_ci([]) is None
    assert _mean_ci([4.2]) == (4.2, 4.2)
    lo, hi = _mean_ci([1000.0, 3000.0])
    assert lo < 2000.0 < hi


def test_token_extraction_from_totals_and_result_fallback() -> None:
    run = {
        "result": {},
        "metadata": {"usage": {"totals": {"total_tokens": 12345, "tokens_per_second": 42.5}}},
    }
    assert _extract_total_tokens(run) == 12345.0
    assert _extract_tokens_per_second(run) == 42.5

    # result takes precedence over metadata (checked first).
    run_result = {
        "result": {"usage": {"totals": {"total_tokens": 999, "tokens_per_second": 7.0}}},
        "metadata": {"usage": {"totals": {"total_tokens": 1, "tokens_per_second": 1.0}}},
    }
    assert _extract_total_tokens(run_result) == 999.0
    assert _extract_tokens_per_second(run_result) == 7.0

    # missing everywhere -> None
    empty = {"result": {}, "metadata": {}}
    assert _extract_total_tokens(empty) is None
    assert _extract_tokens_per_second(empty) is None


def test_aggregate_runs_records_token_stats_and_cis() -> None:
    runs = [
        _run("agent-a", "task-1", "PASS", 1.0, "2026-01-01T00:00:00", elapsed_seconds=60.0, total_tokens=1000, tokens_per_second=10.0),
        _run("agent-a", "task-2", "FAIL", 3.0, "2026-01-01T00:01:00", elapsed_seconds=120.0, total_tokens=3000, tokens_per_second=30.0),
    ]

    aggregated = aggregate_runs(runs)
    agent_a = aggregated["agents"]["agent-a"]

    assert agent_a["average_tokens"] == 2000.0
    assert agent_a["average_tokens_per_second"] == 20.0
    assert agent_a["tokens_ci"] is not None
    assert agent_a["tokens_per_second_ci"] is not None
    assert agent_a["cost_ci"] is not None
    assert agent_a["walltime_ci"] is not None

    lo, hi = agent_a["tokens_ci"]
    # Normal-approximation mean CI brackets the mean (2000); with n=2 it is
    # wider than the sample range, which is expected.
    assert lo < 2000.0 < hi
