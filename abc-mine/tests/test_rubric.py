"""Tests for :mod:`abc_mine.rubric`.

The rubric is the single source of truth for scoring. Two classes of invariant
matter here:

* **Scoring arithmetic** must be stable across releases, otherwise scores from
  one run cannot be compared against another. The mixed-case regression value
  below is carried over from the predecessor pipeline and is asserted exactly.
* **Gates must fail closed.** A missing, unparseable, or zero gate axis has to
  disqualify; treating it as a pass would let unverifiable commits through, and
  no later stage would notice.
"""

from __future__ import annotations

import pytest

from abc_mine import rubric as R


ALL_KEYS = ("verifiability", "task_well_defined", "starting_state",
            "self_contained", "difficulty_fit", "verify_cost")


def _all(value: int) -> dict[str, int]:
    return {k: value for k in ALL_KEYS}


# -- score() -----------------------------------------------------------------


def test_default_axes_are_the_documented_six():
    """The axis vocabulary is a public contract: prompts and configs name these
    keys, so an accidental rename/removal must break loudly here."""
    assert R.DEFAULT_RUBRIC.keys == ALL_KEYS
    assert R.DEFAULT_RUBRIC.gate_keys == ("verifiability", "task_well_defined")


def test_score_all_fours_is_a_perfect_hundred():
    """Weights sum to 1.0 and normalization is by MAX_AXIS_SCORE, so a perfect
    card must land exactly on 100 — proving the 0-100 range really spans."""
    assert R.score(_all(4)) == (100, False)


def test_score_all_zeros_is_disqualified():
    """An all-zero card trips both gates."""
    assert R.score(_all(0)) == (0, True)


def test_zero_on_verifiability_gate_disqualifies():
    """POLARITY/SAFETY: verifiability is a gate. A 0 forces score 0 *and* the
    disqualified flag, regardless of how strong every other axis is. A commit
    whose correctness cannot be checked is worthless as a benchmark."""
    axes = _all(4)
    axes["verifiability"] = 0
    assert R.score(axes) == (0, True)


def test_zero_on_task_well_defined_gate_disqualifies():
    """POLARITY/SAFETY: the second gate behaves identically — no coherent task
    means no benchmark, whatever the rest of the card says."""
    axes = _all(4)
    axes["task_well_defined"] = 0
    assert R.score(axes) == (0, True)


def test_zero_on_a_non_gate_axis_does_not_disqualify():
    """Only gate axes disqualify; a 0 elsewhere merely lowers the total."""
    axes = _all(4)
    axes["verify_cost"] = 0
    total, dq = R.score(axes)
    assert dq is False
    assert 0 < total < 100


def test_score_mixed_case_regression_guard():
    """REGRESSION GUARD: this exact score (79) is carried over from the
    predecessor pipeline. Any change to weights or the rounding rule shifts it,
    which would silently make old and new scores incomparable."""
    axes = {
        "verifiability": 4,
        "task_well_defined": 3,
        "starting_state": 3,
        "self_contained": 2,
        "difficulty_fit": 3,
        "verify_cost": 3,
    }
    assert R.score(axes) == (79, False)


def test_bare_int_and_dict_forms_score_identically():
    """Models emit either ``{"key": 3}`` or ``{"key": {"score": 3, ...}}``.
    Both must be accepted, or a prompt-format change would silently zero every
    axis and disqualify the whole history."""
    bare = {
        "verifiability": 4,
        "task_well_defined": 3,
        "starting_state": 3,
        "self_contained": 2,
        "difficulty_fit": 3,
        "verify_cost": 3,
    }
    rich = {k: {"score": v, "why": "because"} for k, v in bare.items()}
    assert R.score(bare) == R.score(rich) == (79, False)


@pytest.mark.parametrize("raw,expected", [(9, 4), (100, 4), (-1, 0), (-99, 0)])
def test_out_of_range_scores_clamp(raw, expected):
    """Out-of-range model output is clamped rather than trusted: a hallucinated
    ``7`` must not inflate the total past 100."""
    axes = _all(4)
    axes["verify_cost"] = raw
    clamped = _all(4)
    clamped["verify_cost"] = expected
    assert R.score(axes) == R.score(clamped)


def test_scores_above_max_cannot_exceed_one_hundred():
    assert R.score(_all(99)) == (100, False)


def test_missing_axes_count_as_zero_and_so_disqualify():
    """FAIL-CLOSED: an agent that omits axes has not produced a usable verdict.
    Missing entries are 0, which trips a gate, so junk cannot slip through."""
    assert R.score({"starting_state": 4}) == (0, True)


def test_missing_only_non_gate_axes_lowers_but_does_not_disqualify():
    axes = {"verifiability": 4, "task_well_defined": 4}
    total, dq = R.score(axes)
    assert dq is False
    assert 0 < total < 100


@pytest.mark.parametrize("bad", [None, [], "nonsense", 7])
def test_non_mapping_input_does_not_raise(bad):
    """A malformed agent payload must degrade to a disqualification, never an
    exception that would abort an entire sweep."""
    assert R.score(bad) == (0, True)


def test_unparseable_axis_values_become_zero():
    axes = _all(4)
    axes["verifiability"] = "not a number"
    assert R.score(axes) == (0, True)


def test_bound_score_method_matches_module_function():
    axes = _all(3)
    assert R.DEFAULT_RUBRIC.score(axes) == R.score(axes)


# -- validate_axes() ---------------------------------------------------------


def test_validate_axes_accepts_defaults():
    assert R.validate_axes(R.DEFAULT_AXES) == R.DEFAULT_AXES


def test_validate_axes_rejects_empty():
    with pytest.raises(ValueError, match="at least one axis"):
        R.validate_axes([])


def test_validate_axes_rejects_duplicate_keys():
    """Duplicate keys would make the weight sum meaningless and axis lookup
    ambiguous."""
    axis = R.DEFAULT_AXES[0]
    dup = R.Axis(key=axis.key, title="Dup", weight=0.5, gate=True, guidance="x")
    other = R.Axis(key=axis.key, title="Dup2", weight=0.5, gate=False, guidance="x")
    with pytest.raises(ValueError, match="duplicate"):
        R.validate_axes([dup, other])


def test_validate_axes_rejects_weights_not_summing_to_one():
    """If weights do not sum to 1.0 the '0-100' score no longer spans 0-100 and
    scores are not comparable across runs."""
    axes = [
        R.Axis(key="a", title="A", weight=0.5, gate=True, guidance="x"),
        R.Axis(key="b", title="B", weight=0.2, gate=False, guidance="x"),
    ]
    with pytest.raises(ValueError, match="sum to 1.0"):
        R.validate_axes(axes)


def test_validate_axes_rejects_negative_weight():
    axes = [
        R.Axis(key="a", title="A", weight=1.5, gate=True, guidance="x"),
        R.Axis(key="b", title="B", weight=-0.5, gate=False, guidance="x"),
    ]
    with pytest.raises(ValueError, match="negative weight"):
        R.validate_axes(axes)


def test_validate_axes_requires_a_gate():
    """SAFETY: without a gate, a commit nobody can verify still scores nonzero
    and would be ranked and built."""
    axes = [
        R.Axis(key="a", title="A", weight=0.5, gate=False, guidance="x"),
        R.Axis(key="b", title="B", weight=0.5, gate=False, guidance="x"),
    ]
    with pytest.raises(ValueError, match="gate axis"):
        R.validate_axes(axes)


# -- rubric_from_config() ----------------------------------------------------


def test_rubric_from_config_none_returns_default():
    assert R.rubric_from_config(None) is R.DEFAULT_RUBRIC
    assert R.rubric_from_config({}) is R.DEFAULT_RUBRIC


def test_rubric_from_config_applies_a_weight_override():
    """Re-weighting is a single edit in one place; the scorer must honor it."""
    cfg = {"axes": {"verifiability": {"weight": 0.35}, "verify_cost": {"weight": 0.05}}}
    rub = R.rubric_from_config(cfg)
    assert rub.axis("verifiability").weight == pytest.approx(0.35)
    assert rub.axis("verify_cost").weight == pytest.approx(0.05)
    # All-4s must still be exactly 100: weights still sum to 1.0.
    assert R.score(_all(4), rubric=rub) == (100, False)
    # And re-weighting must actually change a mixed card.
    mixed = dict(_all(4))
    mixed["verify_cost"] = 0
    assert R.score(mixed, rubric=rub) != R.score(mixed)


def test_rubric_from_config_rejects_unknown_axis_key():
    """FAIL-CLOSED: a typo'd axis name would otherwise be scored as a *missing*
    axis (0) and — on a gate — quietly disqualify every commit in the run."""
    with pytest.raises(ValueError, match="unknown rubric axis"):
        R.rubric_from_config({"axes": {"verifiabilty": {"weight": 0.3}}})


def test_rubric_from_config_rejects_weights_that_no_longer_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        R.rubric_from_config({"axes": {"verifiability": {"weight": 0.9}}})


def test_rubric_from_config_rejects_removing_every_gate():
    with pytest.raises(ValueError, match="gate axis"):
        R.rubric_from_config(
            {"axes": {"verifiability": {"gate": False}, "task_well_defined": {"gate": False}}}
        )


def test_rubric_from_config_rejects_non_mapping_axes():
    with pytest.raises(ValueError, match="must be a mapping"):
        R.rubric_from_config({"axes": ["verifiability"]})
    with pytest.raises(ValueError, match="must be a mapping"):
        R.rubric_from_config({"axes": {"verifiability": 0.3}})


def test_rubric_from_config_replaces_task_types():
    rub = R.rubric_from_config({"task_types": ["bug_fix", "refactor"]})
    assert rub.task_types == ("bug_fix", "refactor")


def test_rubric_from_config_rejects_empty_task_types():
    with pytest.raises(ValueError, match="task_types"):
        R.rubric_from_config({"task_types": []})


def test_rubric_axis_lookup_raises_keyerror_for_unknown():
    with pytest.raises(KeyError):
        R.DEFAULT_RUBRIC.axis("nope")


# -- coerce_axes() -----------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        None,
        {},
        {"verifiability": 4},
        {"verifiability": {"score": 4, "why": "clear test"}},
        {"verifiability": "junk"},
        "not a mapping",
    ],
)
def test_coerce_axes_always_returns_every_axis_fully_typed(src):
    """Downstream writers (summaries, rank payloads) assume every axis is
    present with an int score, a bool gate, and a str why. Normalizing here is
    what keeps the on-disk summary schema stable regardless of model output."""
    out = R.DEFAULT_RUBRIC.coerce_axes(src)
    assert set(out) == set(ALL_KEYS)
    for key, entry in out.items():
        assert isinstance(entry["score"], int)
        assert 0 <= entry["score"] <= R.MAX_AXIS_SCORE
        assert isinstance(entry["gate"], bool)
        assert isinstance(entry["why"], str)
        assert entry["gate"] == R.DEFAULT_RUBRIC.axis(key).gate


def test_coerce_axes_preserves_why_and_clamps_score():
    out = R.DEFAULT_RUBRIC.coerce_axes(
        {"verifiability": {"score": 99, "why": "a reason"}, "verify_cost": -3}
    )
    assert out["verifiability"] == {"score": 4, "gate": True, "why": "a reason"}
    assert out["verify_cost"] == {"score": 0, "gate": False, "why": ""}


# -- renderers ---------------------------------------------------------------


def test_render_axis_table_mentions_every_axis_title():
    """The table embedded in the prompt is *derived* from the rubric, so the
    model can never be shown weights the Python scorer does not apply."""
    text = R.render_axis_table()
    assert text.strip()
    for ax in R.DEFAULT_AXES:
        assert ax.title in text
        assert f"{ax.weight:.2f}" in text
    assert "**yes**" in text  # at least one gate is advertised as such


def test_render_axis_guidance_mentions_every_axis():
    text = R.render_axis_guidance()
    assert text.strip()
    for ax in R.DEFAULT_AXES:
        assert ax.title in text


def test_render_axes_schema_lists_every_key_and_the_score_range():
    """The schema fragment tells the agent exactly what to emit; if it drifted
    from the axis keys the scorer would read zeros for every axis."""
    text = R.render_axes_schema()
    assert text.strip()
    for ax in R.DEFAULT_AXES:
        assert f'"{ax.key}":' in text
    assert '"score": 0-4' in text
    # Valid JSON-ish fragment: exactly one entry per axis, commas between.
    assert len(text.splitlines()) == len(R.DEFAULT_AXES)
    assert text.count(",") >= len(R.DEFAULT_AXES) - 1


def test_bound_renderers_match_module_functions():
    assert R.DEFAULT_RUBRIC.render_axis_table() == R.render_axis_table()
    assert R.DEFAULT_RUBRIC.render_axis_guidance() == R.render_axis_guidance()
    assert R.DEFAULT_RUBRIC.render_axes_schema() == R.render_axes_schema()


def test_renderers_track_a_custom_rubric():
    """Single-sourcing check: overriding a weight must change the rendered
    table, otherwise the prompt and the scorer could disagree."""
    rub = R.rubric_from_config(
        {"axes": {"verifiability": {"weight": 0.35, "title": "Checkability"},
                  "verify_cost": {"weight": 0.05}}}
    )
    table = rub.render_axis_table()
    assert "Checkability" in table
    assert "0.35" in table
    assert "0.05" in table
