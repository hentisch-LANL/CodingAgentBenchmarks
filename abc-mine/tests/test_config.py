"""Tests for :mod:`abc_mine.config`.

The config is the one file that defines a run, so the invariants here are mostly
about **refusing bad input early**: a typo'd section, a ``test_one`` that cannot
select one test, a ``run.id`` that is not a slug. Every one of those would
otherwise surface much later as a confusing failure after real money had been
spent on agents.

Also covered: relative paths resolve against the *config file's* directory (so a
config plus its pack and container form a relocatable bundle), and the model
endpoint comes only from the environment (so a config is safe to commit and a
credential is never serialized).
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from abc_mine import config as C


# -- fixtures / helpers ------------------------------------------------------


def _write_config(tmp_path, extra: dict | None = None, *, name="mine.yaml"):
    """Write a minimal valid config into ``tmp_path`` and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    image = tmp_path / "image.sqfs"
    image.touch()

    data: dict = {
        "run": {"id": "test-run"},
        "codebase": {"name": "Test Base", "repo": "repo"},
        "container": {"image": "image.sqfs"},
    }
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value

    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


# -- loading -----------------------------------------------------------------


def test_minimal_config_loads(tmp_path):
    cfg = C.load(_write_config(tmp_path))
    assert cfg.run_id == "test-run"
    assert cfg.codebase.name == "Test Base"
    assert cfg.codebase.repo == tmp_path / "repo"
    assert cfg.container.image == tmp_path / "image.sqfs"
    assert cfg.codebase.source_mount == C.DEFAULT_SOURCE_MOUNT
    assert cfg.selection.mode == "all"
    assert cfg.execution.parallel == "serial"
    assert cfg.source_path == tmp_path / "mine.yaml"


def test_relative_paths_resolve_against_the_config_files_directory(tmp_path, monkeypatch):
    """RELOCATABILITY: a config plus its pack and image must form a bundle that
    works regardless of the process CWD."""
    sub = tmp_path / "bundle"
    sub.mkdir()
    path = _write_config(sub)
    (sub / "prompts").mkdir()
    data = yaml.safe_load(path.read_text())
    data["prompts"] = {"pack": "prompts"}
    data["run"]["data_root"] = "out"
    path.write_text(yaml.safe_dump(data))

    monkeypatch.chdir(tmp_path)  # deliberately NOT the config's directory
    cfg = C.load(path)
    assert cfg.codebase.repo == sub / "repo"
    assert cfg.container.image == sub / "image.sqfs"
    assert cfg.prompts.pack == sub / "prompts"
    assert cfg.data_root == sub / "out"


def test_absolute_paths_are_left_alone(tmp_path):
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["codebase"]["repo"] = str(tmp_path / "repo")
    path.write_text(yaml.safe_dump(data))
    cfg = C.load(path)
    assert cfg.codebase.repo == tmp_path / "repo"


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(C.ConfigError, match="config file not found"):
        C.load(tmp_path / "nope.yaml")


def test_malformed_yaml_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("run: [unclosed\n")
    with pytest.raises(C.ConfigError, match="invalid YAML"):
        C.load(path)


def test_non_mapping_yaml_raises(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(C.ConfigError, match="must be a YAML mapping"):
        C.load(path)


def test_missing_run_id_raises(tmp_path):
    """``run.id`` namespaces every output; without it, two runs would collide."""
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["run"] = {}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.ConfigError, match="run.id is required"):
        C.load(path)


@pytest.mark.parametrize("bad_id", ["Test Run", "TestRun", "test_run", "run/one", "-lead"])
def test_non_slug_run_id_raises(tmp_path, bad_id):
    """``run.id`` becomes a directory name; a non-slug would produce surprising
    paths, so it is rejected with a suggested slug rather than silently
    normalized."""
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["run"]["id"] = bad_id
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.ConfigError, match="must be a slug"):
        C.load(path)


def test_missing_codebase_section_raises(tmp_path):
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    del data["codebase"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.ConfigError, match="'codebase' is required"):
        C.load(path)


def test_missing_codebase_repo_raises(tmp_path):
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["codebase"] = {"name": "X"}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.ConfigError, match="codebase.repo is required"):
        C.load(path)


def test_missing_container_image_raises(tmp_path):
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["container"] = {}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.ConfigError, match="container.image is required"):
        C.load(path)


def test_missing_container_section_raises(tmp_path):
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    del data["container"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(C.ConfigError, match="'container' is required"):
        C.load(path)


def test_unknown_top_level_section_raises(tmp_path):
    """FAIL-CLOSED: a typo'd section (``toolchian:``) would otherwise be silently
    ignored and the run would proceed with no toolchain at all."""
    path = _write_config(tmp_path, {"toolchian": {"build": "make"}})
    with pytest.raises(C.ConfigError, match="unknown top-level config section"):
        C.load(path)


def test_non_mapping_section_raises(tmp_path):
    path = _write_config(tmp_path, {"execution": ["workers"]})
    with pytest.raises(C.ConfigError, match="must be a mapping"):
        C.load(path)


def test_non_absolute_source_mount_raises(tmp_path):
    """Prompts and toolchain commands all reference the mount point, which only
    makes sense as an absolute in-container path."""
    path = _write_config(tmp_path, {"codebase": {"source_mount": "relative/src"}})
    with pytest.raises(C.ConfigError, match="absolute in-container path"):
        C.load(path)


def test_non_integer_optional_int_raises(tmp_path):
    path = _write_config(tmp_path, {"execution": {"timeout_seconds": "soon"}})
    with pytest.raises(C.ConfigError, match="must be an integer"):
        C.load(path)


def test_non_numeric_optional_float_raises(tmp_path):
    path = _write_config(tmp_path, {"toolchain": {"full_build_seconds": "fast"}})
    with pytest.raises(C.ConfigError, match="must be a number"):
        C.load(path)


def test_non_mapping_toolchain_env_raises(tmp_path):
    path = _write_config(tmp_path, {"toolchain": {"env": ["A=1"]}})
    with pytest.raises(C.ConfigError, match="toolchain.env must be a mapping"):
        C.load(path)


# -- ToolchainConfig ---------------------------------------------------------


def test_test_one_without_selector_placeholder_raises():
    """DECISIVE CHECK: ``test_one`` is the command that runs ONE test. Without
    ``{selector}`` it would run the whole suite, which is the expensive regime
    the rubric's verify_cost axis exists to avoid."""
    with pytest.raises(C.ConfigError, match=r"\{selector\}"):
        C.ToolchainConfig(test_one="ctest --test-dir {build}")


def test_test_one_with_selector_is_accepted():
    tc = C.ToolchainConfig(test_one="ctest --test-dir {build} -R '{selector}'")
    assert "{selector}" in tc.test_one


def test_build_without_any_test_command_raises():
    """A build with no test means no emitted benchmark can be verified beyond
    'it compiles', which the rubric treats as near-worthless."""
    with pytest.raises(C.ConfigError, match="no way to test"):
        C.ToolchainConfig(build="make -j")


def test_build_with_test_only_is_accepted():
    assert C.ToolchainConfig(build="make", test="ctest").test == "ctest"


def test_build_with_test_one_only_is_accepted():
    tc = C.ToolchainConfig(build="make", test_one="ctest -R '{selector}'")
    assert tc.build == "make"


def test_test_without_build_is_accepted():
    """Interpreted languages need no build step."""
    assert C.ToolchainConfig(test="pytest").test == "pytest"


def test_toolchain_validation_runs_through_load(tmp_path):
    path = _write_config(tmp_path, {"toolchain": {"build": "make", "test_one": "ctest"}})
    with pytest.raises(C.ConfigError, match=r"\{selector\}"):
        C.load(path)


# -- ToolchainConfig.render() -----------------------------------------------


def test_render_interpolates_source_build_and_install():
    tc = C.ToolchainConfig(
        configure="cmake -S {source} -B {build} -DCMAKE_INSTALL_PREFIX={install}",
        build="cmake --build {build}",
        test_one="ctest --test-dir {build} -R '{selector}'",
        build_dir="/tmp/b",
        install_dir="/tmp/i",
    )
    assert tc.render(tc.configure, "/src") == (
        "cmake -S /src -B /tmp/b -DCMAKE_INSTALL_PREFIX=/tmp/i"
    )
    assert tc.render(tc.build, "/src") == "cmake --build /tmp/b"
    assert tc.render(tc.test_one, "/src", selector="mytest") == (
        "ctest --test-dir /tmp/b -R 'mytest'"
    )


def test_render_derives_install_dir_when_unset():
    tc = C.ToolchainConfig(build_dir="/tmp/b")
    assert tc.paths("/src")["install"] == "/tmp/b-install"


def test_render_unknown_placeholder_raises():
    """Failing here beats shipping a literal ``{foo}`` into a container command
    line, where it would fail cryptically inside the image."""
    tc = C.ToolchainConfig()
    with pytest.raises(C.ConfigError, match="unknown placeholder"):
        tc.render("make -C {srcdir}", "/src")


def test_render_selector_placeholder_without_a_selector_raises():
    """Rendering ``test_one`` without a selector must not silently emit a literal
    ``{selector}``."""
    tc = C.ToolchainConfig(test_one="ctest -R '{selector}'")
    with pytest.raises(C.ConfigError, match="unknown placeholder"):
        tc.render(tc.test_one, "/src")


def test_render_malformed_brace_syntax_raises():
    tc = C.ToolchainConfig()
    with pytest.raises(C.ConfigError, match="malformed placeholder"):
        tc.render("echo {", "/src")


def test_rendered_env_interpolates_each_value():
    tc = C.ToolchainConfig(
        env={"SRC": "{source}", "BLD": "{build}"}, build_dir="/tmp/b"
    )
    assert tc.rendered_env("/src") == {"SRC": "/src", "BLD": "/tmp/b"}


# -- render_prompt_block() ---------------------------------------------------


def test_prompt_block_includes_every_configured_command():
    """SINGLE-SOURCING: the commands an agent is told to use are generated from
    the same strings preflight verified and the verifiers will call."""
    tc = C.ToolchainConfig(
        configure="./configure --prefix={install}",
        build="make -j -C {build}",
        test="ctest --test-dir {build}",
        test_one="ctest --test-dir {build} -R '{selector}'",
        build_dir="/tmp/b",
        env={"SRC": "{source}"},
    )
    block = tc.render_prompt_block("/workspace/src")
    assert "./configure --prefix=/tmp/b-install" in block
    assert "make -j -C /tmp/b" in block
    assert "ctest --test-dir /tmp/b" in block
    assert "<selector>" in block  # test_one shown with a placeholder selector
    assert "SRC=/workspace/src" in block


def test_prompt_block_says_cost_is_unmeasured_when_none():
    """MEASURED-NOT-ASSERTED: when nothing has been measured, the prompt must say
    so explicitly rather than invent a figure (the predecessor pipeline hard-coded
    a wrong constant and had to correct it in a later stage's prompt)."""
    tc = C.ToolchainConfig(build="make", test="ctest", full_build_seconds=None)
    block = tc.render_prompt_block("/src")
    assert "has not been measured" in block
    assert "Measured cost" not in block


def test_prompt_block_reports_a_measured_figure_when_set():
    tc = C.ToolchainConfig(
        build="make", test="ctest", full_build_seconds=240.0, targeted_test_seconds=12.0
    )
    block = tc.render_prompt_block("/src")
    assert "has not been measured" not in block
    assert "Measured cost" in block
    assert "4 minutes" in block
    assert "12 seconds" in block


def test_prompt_block_with_no_commands_still_renders():
    block = C.ToolchainConfig().render_prompt_block("/src")
    assert "has not been measured" in block


# -- SelectionConfig ---------------------------------------------------------


def test_selection_defaults():
    sel = C.SelectionConfig()
    assert sel.mode == "all"
    assert sel.exclude_merges is True


def test_selection_invalid_mode_raises():
    with pytest.raises(C.ConfigError, match="selection.mode must be one of"):
        C.SelectionConfig(mode="newest")


@pytest.mark.parametrize("mode", ["recent", "sample"])
def test_selection_recent_and_sample_require_count_or_stride(mode):
    with pytest.raises(C.ConfigError, match="requires 'count'"):
        C.SelectionConfig(mode=mode)


def test_selection_recent_with_count_is_accepted():
    assert C.SelectionConfig(mode="recent", count=50).count == 50


def test_selection_sample_with_stride_only_is_accepted():
    assert C.SelectionConfig(mode="sample", stride=7).stride == 7


def test_selection_range_mode_requires_range():
    with pytest.raises(C.ConfigError, match="requires 'range"):
        C.SelectionConfig(mode="range")


def test_selection_range_mode_with_range_is_accepted():
    sel = C.SelectionConfig(mode="range", range=("aaa", "bbb"))
    assert sel.range == ("aaa", "bbb")


def test_selection_range_must_have_two_elements_via_load(tmp_path):
    path = _write_config(tmp_path, {"selection": {"mode": "range", "range": ["a"]}})
    with pytest.raises(C.ConfigError, match="two-element list"):
        C.load(path)


def test_selection_paths_as_a_bare_string_is_wrapped(tmp_path):
    path = _write_config(tmp_path, {"selection": {"paths": "src/physics"}})
    cfg = C.load(path)
    assert cfg.selection.paths == ("src/physics",)


# -- ExecutionConfig ---------------------------------------------------------


def test_execution_invalid_parallel_raises():
    with pytest.raises(C.ConfigError, match="execution.parallel must be"):
        C.ExecutionConfig(parallel="threads")


def test_execution_workers_below_one_raises():
    with pytest.raises(C.ConfigError, match="workers must be >= 1"):
        C.ExecutionConfig(workers=0)


def test_execution_max_attempts_below_one_raises():
    """``max_attempts`` bounds retries across resumes; 0 would mean no work ever
    happens, and a negative value is nonsense."""
    with pytest.raises(C.ConfigError, match="max_attempts must be >= 1"):
        C.ExecutionConfig(max_attempts=0)


def test_execution_invalid_keep_logs_raises():
    with pytest.raises(C.ConfigError, match="keep_logs must be"):
        C.ExecutionConfig(keep_logs="some")


@pytest.mark.parametrize("value", ["none", "failed", "all"])
def test_execution_valid_keep_logs(value):
    assert C.ExecutionConfig(keep_logs=value).keep_logs == value


def test_execution_defaults_need_nothing_beyond_the_stdlib():
    ex = C.ExecutionConfig()
    assert ex.parallel == "serial"
    assert ex.keep_logs == "failed"
    assert ex.max_attempts == 2


# -- dotted overrides --------------------------------------------------------


def test_dotted_overrides_are_applied(tmp_path):
    """CLI flags arrive as dotted overrides; they must reach the resolved config
    (and get fingerprinted, so a resumed run can detect the drift)."""
    path = _write_config(tmp_path)
    cfg = C.load(
        path,
        overrides={"execution.workers": 8, "execution.parallel": "local"},
    )
    assert cfg.execution.workers == 8
    assert cfg.execution.parallel == "local"


def test_dotted_override_creates_a_missing_section(tmp_path):
    path = _write_config(tmp_path)
    cfg = C.load(path, overrides={"rank.max_candidates": 25})
    assert cfg.rank.max_candidates == 25


def test_dotted_override_is_validated_like_file_content(tmp_path):
    path = _write_config(tmp_path)
    with pytest.raises(C.ConfigError, match="workers must be >= 1"):
        C.load(path, overrides={"execution.workers": 0})


def test_dotted_override_through_a_non_section_raises(tmp_path):
    path = _write_config(tmp_path)
    with pytest.raises(C.ConfigError, match="is not a section"):
        C.load(path, overrides={"run.id.deeper": "x"})


# -- slugify() ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Truchas", "truchas"),
        ("Truchas v1", "truchas-v1"),
        ("  Leading and trailing  ", "leading-and-trailing"),
        ("Under_scores and.dots", "under-scores-and-dots"),
        ("MiXeD CaSe", "mixed-case"),
        ("a//b__c", "a-b-c"),
        ("---", ""),
        ("", ""),
        ("123", "123"),
    ],
)
def test_slugify(text, expected):
    assert C.slugify(text) == expected


def test_slugify_caps_length_and_does_not_end_in_a_hyphen():
    """The slug becomes a directory name, so it is capped; capping must not leave
    a trailing hyphen."""
    assert len(C.slugify("x" * 200)) == 64
    assert C.slugify("ab cd", max_length=3) == "ab"
    assert not C.slugify("ab cd", max_length=3).endswith("-")


def test_slugify_respects_custom_max_length():
    assert C.slugify("abcdefghij", max_length=4) == "abcd"


# -- llm_env() ---------------------------------------------------------------


def test_llm_env_raises_when_everything_is_missing(monkeypatch):
    """CREDENTIAL HYGIENE: the endpoint comes only from the environment, so a
    config file is safe to commit. Missing values must fail in seconds, before
    any container is provisioned."""
    for var in C.LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(C.ConfigError) as exc:
        C.llm_env()
    message = str(exc.value)
    assert "LLM_BASE_URL" in message
    assert "LLM_MODEL" in message
    assert "LLM_API_KEY" in message


def test_llm_env_raises_when_only_the_key_is_missing(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.delenv("LLM_API", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(C.ConfigError, match="LLM_API_KEY"):
        C.llm_env()


def test_llm_env_returns_settings_and_never_the_key(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("LLM_API", "responses")
    monkeypatch.setenv("LLM_API_KEY", "super-secret")
    env = C.llm_env()
    assert env == {
        "base_url": "http://localhost:8000/v1",
        "model": "some-model",
        "api": "responses",
    }
    assert "super-secret" not in repr(env)


def test_llm_env_api_defaults_to_chat_completions(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_API", raising=False)
    assert C.llm_env()["api"] == "chat_completions"


def test_llm_env_invalid_api_raises(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_API", "grpc")
    with pytest.raises(C.ConfigError, match="LLM_API must be"):
        C.llm_env()


def test_llm_env_accepts_anthropic_messages(monkeypatch):
    """The Anthropic Messages style is the only one that enables prompt caching.

    Reaching an Anthropic-backed endpoint over ``chat_completions`` works but
    silently forgoes cache reads, so this style must be selectable explicitly.
    """
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_API", "anthropic_messages")
    assert C.llm_env()["api"] == "anthropic_messages"


def test_llm_api_styles_are_the_validated_set(monkeypatch):
    """Every advertised style validates, so the constant cannot drift from the check."""
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    for style in C.LLM_API_STYLES:
        monkeypatch.setenv("LLM_API", style)
        assert C.llm_env()["api"] == style


def test_llm_env_whitespace_only_values_count_as_missing(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "   ")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    with pytest.raises(C.ConfigError, match="LLM_BASE_URL"):
        C.llm_env()


# -- derived properties -----------------------------------------------------


def test_derived_paths(tmp_path):
    path = _write_config(tmp_path, {"run": {"data_root": "out"}})
    cfg = C.load(path)
    assert cfg.run_dir == tmp_path / "out" / "test-run"
    assert cfg.screen_dir == cfg.run_dir / "screen"
    assert cfg.summaries_dir == cfg.run_dir / "screen" / "summaries"
    assert cfg.rank_dir == cfg.run_dir / "rank"
    assert cfg.build_dir == cfg.run_dir / "build"
    assert cfg.tasks_dir == cfg.run_dir / "build" / "tasks"


def test_group_id_defaults_to_a_slug_of_the_codebase_name(tmp_path):
    cfg = C.load(_write_config(tmp_path))
    assert cfg.codebase.name == "Test Base"
    assert cfg.group_id == "test-base"


def test_explicit_group_id_wins(tmp_path):
    path = _write_config(tmp_path, {"codebase": {"group_id": "custom-group"}})
    assert C.load(path).group_id == "custom-group"


def test_with_toolchain_returns_a_copy_with_measurements(tmp_path):
    """``preflight --write-config`` uses this; the original must be untouched."""
    cfg = C.load(_write_config(tmp_path))
    assert cfg.toolchain.full_build_seconds is None
    updated = cfg.with_toolchain(full_build_seconds=123.0)
    assert updated.toolchain.full_build_seconds == 123.0
    assert cfg.toolchain.full_build_seconds is None
    assert updated.run_id == cfg.run_id


def test_default_rubric_is_installed(tmp_path):
    cfg = C.load(_write_config(tmp_path))
    assert cfg.rubric.keys[0] == "verifiability"


def test_rubric_section_is_applied_from_the_config(tmp_path):
    path = _write_config(
        tmp_path,
        {"rubric": {"axes": {"verifiability": {"weight": 0.35},
                             "verify_cost": {"weight": 0.05}}}},
    )
    cfg = C.load(path)
    assert cfg.rubric.axis("verifiability").weight == pytest.approx(0.35)


def test_bad_rubric_section_propagates_as_a_valueerror(tmp_path):
    path = _write_config(tmp_path, {"rubric": {"axes": {"verifiabilty": {"weight": 0.3}}}})
    with pytest.raises(ValueError, match="unknown rubric axis"):
        C.load(path)


# -- validate_paths() -------------------------------------------------------


def test_validate_paths_reports_a_non_git_repo(tmp_path):
    cfg = C.load(_write_config(tmp_path))
    problems = C.validate_paths(cfg)
    assert any("not a git repository" in p for p in problems)
    assert not any("container.image does not exist" in p for p in problems)


def test_validate_paths_reports_a_missing_repo_and_image(tmp_path):
    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["codebase"]["repo"] = "no-such-repo"
    data["container"]["image"] = "no-such-image.sqfs"
    path.write_text(yaml.safe_dump(data))
    problems = C.validate_paths(C.load(path))
    assert any("codebase.repo does not exist" in p for p in problems)
    assert any("container.image does not exist" in p for p in problems)


def test_humanize_seconds_reads_naturally():
    assert C._humanize_seconds(30) == "30 seconds"
    assert C._humanize_seconds(240) == "4 minutes"
    assert "hours" in C._humanize_seconds(3600 * 3)


def test_harness_wires_every_api_style():
    """The harness must wire exactly the styles ``llm_env`` accepts.

    The accepted set lives in Python and the OpenCode provider mapping lives in
    the harness shell script, so nothing but this test stops one from gaining a
    style the other rejects. That drift would surface as a mid-run harness error
    after an agent had already been provisioned.
    """
    import re
    from pathlib import Path

    script = (
        Path(C.__file__).parent / "harness" / "run_benchmark.sh"
    ).read_text()
    block = re.search(r"API_NPM = \{(.*?)\n\}", script, re.DOTALL)
    assert block, "API_NPM mapping not found in the harness script"
    wired = set(re.findall(r'^\s*"([a-z_]+)":', block.group(1), re.MULTILINE))
    assert wired == set(C.LLM_API_STYLES)
    # The Anthropic adapter is what enables OpenCode's prompt caching.
    assert '"anthropic_messages": "@ai-sdk/anthropic"' in block.group(1)
