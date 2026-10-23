"""Tests for :mod:`abc_mine.prompts`.

Two safety properties dominate this file:

* **Override gating.** An override that is silently ignored is worse than one
  that is silently active: the user believes the run used their prompt when it
  did not. So an override present without ``allow_template_overrides`` must be a
  hard error, and an unknown override filename must be rejected rather than
  no-op'd.
* **Slot all-or-nothing rendering.** An unfilled slot must leave *no* trace —
  no heading, no lead-in, no literal ``@@slot:`` token — or a freshly scaffolded
  pack would inject dangling headings (or instructional comments) straight into
  a live agent prompt.
"""

from __future__ import annotations

import hashlib

import pytest

from abc_mine import prompts as P


# -- shipped templates -------------------------------------------------------


@pytest.mark.parametrize("name", P.TEMPLATE_NAMES)
def test_every_shipped_template_loads_and_is_nonempty(name):
    """The templates are package data; a packaging mistake that dropped one must
    fail here rather than at agent-launch time."""
    body = P.load_template(name)
    assert body.strip()


@pytest.mark.parametrize("name", P.TEMPLATE_NAMES)
def test_template_contains_all_its_required_vars(name):
    """A shipped template missing a mechanically-required placeholder would
    produce an agent that writes its output nowhere."""
    body = P.load_template(name)
    for var in P.REQUIRED_VARS[name]:
        assert f"@@{var}@@" in body, f"{name} is missing required @@{var}@@"


@pytest.mark.parametrize("name", P.TEMPLATE_NAMES)
def test_template_contains_all_its_advisory_vars(name):
    """Advisory placeholders carry single-sourced content (rubric tables). If a
    shipped template dropped one, the prompt would silently stop agreeing with
    the config."""
    body = P.load_template(name)
    for var in P.ADVISORY_VARS[name]:
        assert f"@@{var}@@" in body, f"{name} is missing advisory @@{var}@@"


def test_every_slot_appears_in_each_template_it_claims():
    """The SLOTS table declares where each slot renders. If the declaration and
    the templates disagreed, a user's slot content would vanish without warning."""
    for slot in P.SLOTS:
        for name in slot.templates:
            body = P.load_template(name)
            assert f"@@slot:{slot.key}@@" in body, f"{name} lacks slot {slot.key}"


def test_no_template_references_an_undeclared_slot():
    """The reverse direction: a template must not reference a slot key that is
    not in SLOTS, which would render as an unresolvable token."""
    known = {s.key for s in P.SLOTS}
    for name in P.TEMPLATE_NAMES:
        body = P.load_template(name)
        for match in P._VAR_RE.finditer(body):
            if match.group(1):
                assert match.group(2) in known, f"{name}: unknown slot {match.group(2)}"


def test_load_template_rejects_unknown_name():
    with pytest.raises(P.PromptError, match="unknown template"):
        P.load_template("not_a_template")


def test_slot_by_key_raises_for_unknown():
    with pytest.raises(KeyError):
        P.slot_by_key("nope")


# -- empty pack --------------------------------------------------------------


def test_load_pack_none_is_an_empty_pack():
    """The tool must be usable with no pack at all."""
    pack = P.load_pack(None)
    assert pack.root is None
    assert pack.slots == {}
    assert pack.overrides == {}
    assert pack.has_overrides is False
    assert pack.banner() is None
    assert pack.warnings == []


def test_load_pack_missing_directory_raises(tmp_path):
    with pytest.raises(P.PromptError, match="prompt pack directory not found"):
        P.load_pack(tmp_path / "does-not-exist")


def test_load_pack_empty_directory_is_legal(tmp_path):
    pack = P.load_pack(tmp_path)
    assert pack.slots == {} and pack.overrides == {}


# -- scaffolding -------------------------------------------------------------


def test_scaffold_creates_one_file_per_slot_and_no_overrides_dir(tmp_path):
    """``overrides/`` must stay out of sight for users who do not need it: the
    advanced full-replacement mechanism should be opt-in at every level."""
    dest = tmp_path / "p"
    created = P.scaffold(dest)
    assert len(created) == len(P.SLOTS)
    for slot in P.SLOTS:
        assert (dest / P.SLOTS_SUBDIR / f"{slot.key}.md").is_file()
    assert not (dest / P.OVERRIDES_SUBDIR).exists()


def test_scaffold_with_overrides_creates_the_directory(tmp_path):
    dest = tmp_path / "p"
    created = P.scaffold(dest, with_overrides=True)
    ov = dest / P.OVERRIDES_SUBDIR
    assert ov.is_dir()
    assert (ov / "README.md").is_file()
    assert any(p.name == "README.md" for p in created)


def test_scaffold_is_idempotent(tmp_path):
    dest = tmp_path / "p"
    P.scaffold(dest)
    (dest / P.SLOTS_SUBDIR / "exclusions.md").write_text("real content\n")
    again = P.scaffold(dest)
    assert again == []
    assert (dest / P.SLOTS_SUBDIR / "exclusions.md").read_text() == "real content\n"


def test_scaffolded_pack_loads_with_no_slots(tmp_path):
    """CRITICAL: scaffolded files are comment-only. They must count as EMPTY, or
    a user who scaffolded a pack and filled two slots would ship "this file is
    OPTIONAL, delete it" into every agent's instructions under a real heading."""
    dest = tmp_path / "p"
    P.scaffold(dest)
    pack = P.load_pack(dest)
    assert pack.slots == {}
    assert pack.overrides == {}
    assert pack.has_overrides is False


def test_scaffolded_pack_renders_every_template_with_no_slot_headings(tmp_path):
    """End-to-end form of the same invariant: a freshly scaffolded pack must
    render byte-identically to no pack at all."""
    dest = tmp_path / "p"
    P.scaffold(dest)
    pack = P.load_pack(dest)
    variables = _screen_system_vars()
    assert P.render("screen_system", pack=pack, variables=variables) == P.render(
        "screen_system", pack=None, variables=variables
    )


def test_scaffolded_overrides_readme_is_not_mistaken_for_an_override(tmp_path):
    """The scaffolded overrides/README.md is not named after a base template, so
    it must not be treated as an override — but it also must not trip the
    unknown-filename error, or scaffolding would produce an unloadable pack.

    Regression guard: scaffold(with_overrides=True) previously wrote a README that
    _load_overrides then rejected, so the tool emitted a pack it refused to read.
    Documentation filenames are now skipped (P.IGNORED_PACK_FILES) while any
    plausibly-misnamed override is still an error -- see
    test_unknown_filename_in_overrides_is_rejected.
    """
    dest = tmp_path / "p"
    P.scaffold(dest, with_overrides=True)
    pack = P.load_pack(dest, allow_template_overrides=True)
    assert pack.overrides == {}
    # The same pack must also load when overrides are not permitted: a README is
    # not an override, so its mere presence cannot trip the opt-in gate.
    assert P.load_pack(dest, allow_template_overrides=False).overrides == {}


# -- strip_comments() --------------------------------------------------------


def test_strip_comments_removes_single_line_comment():
    assert P.strip_comments("<!-- gone -->\nkept\n") == "kept"


def test_strip_comments_removes_multiline_comment():
    text = "<!-- line one\nline two\nline three -->\n\nreal prose here\n"
    assert P.strip_comments(text) == "real prose here"


def test_strip_comments_keeps_prose_around_comments():
    text = "before\n\n<!-- note -->\n\nafter\n"
    out = P.strip_comments(text)
    assert "before" in out and "after" in out and "note" not in out


def test_strip_comments_on_comment_only_text_is_empty():
    assert P.strip_comments("<!-- only a comment -->\n") == ""
    assert P.strip_comments("   \n\n  ") == ""


def test_strip_comments_removes_multiple_comments():
    assert P.strip_comments("<!--a-->x<!--b-->y<!--c-->").replace(" ", "") == "xy"


# -- slot loading and rendering ----------------------------------------------


def _write_slot(root, key, text):
    d = root / P.SLOTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.md").write_text(text)


def test_filled_slot_renders_under_its_heading(tmp_path):
    _write_slot(tmp_path, "codebase_overview", "A Fortran solver for physics.\n")
    pack = P.load_pack(tmp_path)
    assert pack.slots["codebase_overview"] == "A Fortran solver for physics."

    out = P.render("screen_system", pack=pack, variables=_screen_system_vars())
    slot = P.slot_by_key("codebase_overview")
    assert slot.heading in out
    assert "A Fortran solver for physics." in out


def test_unfilled_slot_leaves_no_trace(tmp_path):
    """ALL-OR-NOTHING: an absent slot must leave no heading and no token, so the
    surrounding prose reads correctly in both states."""
    pack = P.load_pack(tmp_path)
    out = P.render("screen_system", pack=pack, variables=_screen_system_vars())
    for slot in P.SLOTS:
        if "screen_system" in slot.templates:
            assert slot.heading not in out, f"empty slot {slot.key} left its heading"
    assert "@@slot:" not in out


def test_empty_slot_file_behaves_exactly_like_a_missing_one(tmp_path):
    _write_slot(tmp_path, "codebase_overview", "   \n\n")
    pack = P.load_pack(tmp_path)
    assert pack.slots == {}
    out = P.render("screen_system", pack=pack, variables=_screen_system_vars())
    assert P.slot_by_key("codebase_overview").heading not in out


def test_comment_only_slot_file_behaves_like_a_missing_one(tmp_path):
    _write_slot(tmp_path, "exclusions", "<!-- describe exclusions here -->\n")
    pack = P.load_pack(tmp_path)
    assert pack.slots == {}


def test_render_slot_without_a_pack_is_empty():
    assert P.render_slot("codebase_overview", None) == ""


def test_unknown_filename_in_slots_raises(tmp_path):
    """A typo must be rejected, never ignored: silently dropping a user's prompt
    content is the failure mode this check exists to prevent."""
    d = tmp_path / P.SLOTS_SUBDIR
    d.mkdir()
    (d / "codebase_overvew.md").write_text("typo'd name\n")
    with pytest.raises(P.PromptError, match="unrecognized file"):
        P.load_pack(tmp_path)


def test_wrong_suffix_in_slots_raises(tmp_path):
    d = tmp_path / P.SLOTS_SUBDIR
    d.mkdir()
    (d / "codebase_overview.txt").write_text("wrong suffix\n")
    with pytest.raises(P.PromptError, match="unrecognized file"):
        P.load_pack(tmp_path)


# -- override gating ---------------------------------------------------------


def _write_override(root, name, text):
    d = root / P.OVERRIDES_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)
    return d / f"{name}.md"


def _valid_screen_task_body() -> str:
    return (
        "Custom screen task.\n"
        "sha=@@sha@@ diff=@@diff_path@@ summary=@@summary_path@@\n"
        "schema:\n@@rubric_axes_schema@@\n"
    )


def test_unknown_filename_in_overrides_raises(tmp_path):
    """A misnamed override must never silently do nothing."""
    _write_override(tmp_path, "screen_sistem", "whatever @@sha@@")
    with pytest.raises(P.PromptError, match="unrecognized file"):
        P.load_pack(tmp_path, allow_template_overrides=True)


def test_override_without_the_flag_is_a_hard_error(tmp_path):
    """GATING INVARIANT: an override present while
    ``allow_template_overrides=False`` must FAIL LOUDLY, not be ignored. An
    override the user believes is active but silently is not would be worse than
    either alternative."""
    _write_override(tmp_path, "screen_task", _valid_screen_task_body())
    with pytest.raises(P.PromptError, match="allow_template_overrides"):
        P.load_pack(tmp_path, allow_template_overrides=False)
    # And the default must be the refusing behaviour.
    with pytest.raises(P.PromptError, match="allow_template_overrides"):
        P.load_pack(tmp_path)


def test_override_with_the_flag_loads_and_is_auditable(tmp_path):
    """An overridden run must carry permanent evidence: a digest in the pack and
    a loud banner, so it can never be mistaken for a stock run."""
    body = _valid_screen_task_body()
    _write_override(tmp_path, "screen_task", body)
    pack = P.load_pack(tmp_path, allow_template_overrides=True)

    assert pack.has_overrides is True
    assert pack.overrides["screen_task"] == body
    assert pack.override_digests["screen_task"] == hashlib.sha256(
        body.encode("utf-8")
    ).hexdigest()
    assert len(pack.override_digests["screen_task"]) == 64

    banner = pack.banner()
    assert isinstance(banner, str) and banner.strip()
    assert "screen_task" in banner
    assert "OVERRIDE" in banner.upper()


def test_active_override_is_what_load_template_returns(tmp_path):
    body = _valid_screen_task_body()
    _write_override(tmp_path, "screen_task", body)
    pack = P.load_pack(tmp_path, allow_template_overrides=True)
    assert P.load_template("screen_task", pack) == body
    # Non-overridden templates still come from the shipped set.
    assert P.load_template("screen_system", pack) == P.load_template("screen_system")


def test_override_missing_a_required_var_raises(tmp_path):
    """A mechanically-required placeholder carries a path the stage substitutes.
    Dropping @@summary_path@@ would produce an agent that writes nowhere, so this
    is an error, not a warning."""
    _write_override(
        tmp_path, "screen_task", "sha=@@sha@@ diff=@@diff_path@@\n(no summary path)\n"
    )
    with pytest.raises(P.PromptError, match="summary_path"):
        P.load_pack(tmp_path, allow_template_overrides=True)


def test_override_missing_only_an_advisory_var_warns(tmp_path):
    """SEVERITY SPLIT: dropping advisory content (the rubric schema) is the
    user's legitimate call, so it warns rather than fails."""
    body = "sha=@@sha@@ diff=@@diff_path@@ summary=@@summary_path@@\n"
    _write_override(tmp_path, "screen_task", body)
    pack = P.load_pack(tmp_path, allow_template_overrides=True)
    assert pack.has_overrides is True
    assert pack.warnings
    assert any("rubric_axes_schema" in w for w in pack.warnings)


def test_overrides_only_pack_without_slots_dir_is_legal(tmp_path):
    _write_override(tmp_path, "screen_task", _valid_screen_task_body())
    pack = P.load_pack(tmp_path, allow_template_overrides=True)
    assert pack.slots == {} and pack.has_overrides


# -- render() ----------------------------------------------------------------


def _screen_system_vars() -> dict[str, str]:
    return {
        "codebase_name": "Testbase",
        "toolchain_block": "| Step | Command |",
        "rubric_axis_table": "TABLE",
        "rubric_axis_guidance": "GUIDANCE",
    }


def test_render_strict_raises_on_an_unsupplied_variable():
    """STRICTNESS INVARIANT: leaving a placeholder unsubstituted would put a
    literal ``@@summary_path@@`` into the agent's instructions."""
    with pytest.raises(P.PromptError, match="unresolved placeholder"):
        P.render("screen_system", variables={"codebase_name": "X"}, strict=True)


def test_render_non_strict_leaves_the_token_alone():
    out = P.render("screen_system", variables={"codebase_name": "X"}, strict=False)
    assert "@@rubric_axis_table@@" in out


def test_render_strict_is_satisfied_by_a_full_variable_set():
    out = P.render("screen_system", variables=_screen_system_vars(), strict=True)
    assert "Testbase" in out
    assert "TABLE" in out and "GUIDANCE" in out
    assert "@@" not in out


def test_render_substitution_is_single_pass_for_nested_tokens(tmp_path):
    """SINGLE-PASS INVARIANT: an injected value containing ``@@other@@`` must
    not be re-scanned. Otherwise a diff or a rubric table that happens to
    contain the placeholder syntax would be mangled (the predecessor pipeline
    needed a regex hack to work around exactly this with ``{}``)."""
    payload = "literal @@codebase_name@@ token"
    variables = dict(_screen_system_vars())
    variables["rubric_axis_table"] = payload
    out = P.render("screen_system", variables=variables, strict=True)
    assert payload in out


def test_render_substitution_does_not_mangle_json_braces():
    """``@@name@@`` was chosen precisely so injected JSON is never touched."""
    payload = '{"axes": {"verifiability": {"score": 4}}, "list": [1, 2]}'
    variables = dict(_screen_system_vars())
    variables["toolchain_block"] = payload
    out = P.render("screen_system", variables=variables, strict=True)
    assert payload in out


def test_render_slot_content_is_not_rescanned(tmp_path):
    """Slot content is user prose; it must be inserted verbatim."""
    _write_slot(tmp_path, "codebase_overview", "see @@sha@@ and {braces}\n")
    pack = P.load_pack(tmp_path)
    out = P.render("screen_system", pack=pack, variables=_screen_system_vars())
    assert "see @@sha@@ and {braces}" in out


def test_render_collapses_blank_runs_left_by_vanished_slots(tmp_path):
    out = P.render("screen_system", variables=_screen_system_vars())
    assert "\n\n\n\n" not in out
    assert out.endswith("\n")


def test_render_honours_an_active_override(tmp_path):
    body = "OVERRIDDEN @@sha@@ @@diff_path@@ @@summary_path@@ @@rubric_axes_schema@@\n"
    _write_override(tmp_path, "screen_task", body)
    pack = P.load_pack(tmp_path, allow_template_overrides=True)
    out = P.render(
        "screen_task",
        pack=pack,
        variables={
            "sha": "abc",
            "diff_path": "/d",
            "summary_path": "/s",
            "rubric_axes_schema": "SCHEMA",
        },
    )
    assert out.startswith("OVERRIDDEN abc /d /s SCHEMA")


# -- compose() ---------------------------------------------------------------


def test_compose_joins_system_and_task_with_a_rule():
    """The harness takes one prompt, so system guidance is prepended with a
    horizontal rule between the halves."""
    variables = dict(_screen_system_vars())
    variables.update(
        {
            "sha": "deadbeef",
            "parent": "cafebabe",
            "subject": "Fix a thing",
            "author_date": "2020-01-01",
            "files_changed": 3,
            "diff_lines": 42,
            "touched_files": "a.f90",
            "diff_path": "/w/diff",
            "message_path": "/w/msg",
            "summary_path": "/o/summary.json",
            "source_path": "/workspace/src",
            "diff_truncation_note": "",
            "task_types": "bug_fix",
            "rubric_axes_schema": "SCHEMA",
        }
    )
    out = P.compose("screen_system", "screen_task", variables=variables)
    assert "\n\n---\n\n" in out
    system = P.render("screen_system", variables=variables)
    task = P.render("screen_task", variables=variables)
    assert out == f"{system}\n\n---\n\n{task}"
    assert out.index("---\n") > 0
