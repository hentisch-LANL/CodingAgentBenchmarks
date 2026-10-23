"""Tests for the Codex harness assets.

These tests cover the parts of the Codex harness that can be checked without
Charliecloud or a live model endpoint: the harness metadata/contract, the
container template, and the pure-Python event renderer.

The `codex exec` invocation itself is exercised by
``tools_dev/codex_harness_smoke.py``, which needs a real model endpoint.
"""

from pathlib import Path
import json
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton import Harness  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HARNESS_DIR = REPO / "harnesses" / "codex"
RUN_SCRIPT = HARNESS_DIR / "run_benchmark.sh"
TEMPLATE = REPO / "containers" / "harnesses" / "codex.Dockerfile.jinja2"


def test_harness_loads_and_validates() -> None:
    harness = Harness("codex", HARNESS_DIR)
    assert harness.validate() == []
    # container_template drives containers/harnesses/<name>.Dockerfile.jinja2
    assert harness.container_template == "codex"
    assert harness.run_script == RUN_SCRIPT
    assert harness.system_prompt_file == HARNESS_DIR / "system_prompt.md"


def test_container_template_exists_for_declared_name() -> None:
    harness = Harness("codex", HARNESS_DIR)
    template = REPO / "containers" / "harnesses" / f"{harness.container_template}.Dockerfile.jinja2"
    assert template.is_file()


def test_container_template_substitutes_parent_image_and_installs_codex() -> None:
    text = TEMPLATE.read_text()
    # Rendering is a literal str.replace of exactly "{{ parent_image }}".
    assert "FROM {{ parent_image }}" in text
    # The binary must land on an absolute path the run script can rely on.
    assert "/usr/local/bin/codex" in text
    # A build-time smoke check keeps a broken install from surfacing mid-run.
    assert "codex --version" in text


def test_run_script_is_valid_bash() -> None:
    proc = subprocess.run(["bash", "-n", str(RUN_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_run_script_accepts_every_flag_automaton_passes() -> None:
    """automaton always passes eight flags and conditionally two more.

    A harness that does not parse one of them exits 1 with "Unknown argument".
    """
    text = RUN_SCRIPT.read_text()
    for flag in (
        "--workspace",
        "--task-prompt",
        "--system-prompt",
        "--group-prompt",
        "--models-config",
        "--harness-config",
        "--output-dir",
        "--timeout-seconds",
        "--task-env",
    ):
        assert f"        {flag})" in text, f"{flag} is not parsed"


def test_run_script_uses_pipestatus_not_dollar_question() -> None:
    """`$?` after a `| tee` pipeline captures tee's status, not codex's.

    Getting this wrong makes every timeout and agent failure look like success.
    """
    assert 'EXIT_CODE="${PIPESTATUS[0]}"' in RUN_SCRIPT.read_text()


def test_run_script_exits_124_on_timeout() -> None:
    text = RUN_SCRIPT.read_text()
    assert "timeout \"${TIMEOUT_SECONDS}\"" in text
    assert "exit 124" in text


def test_run_script_guards_optional_group_prompt() -> None:
    """--group-prompt is omitted when the group ships no group_prompt.md."""
    text = RUN_SCRIPT.read_text()
    assert '[[ -n "${GROUP_PROMPT}" && -f "${GROUP_PROMPT}" ]]' in text


def test_run_script_disables_web_search_with_the_effective_key() -> None:
    """`tools.web_search=false` alone does not drop the tool on 0.155.1."""
    text = RUN_SCRIPT.read_text()
    assert '-c \'web_search="disabled"\'' in text


def test_run_script_keeps_state_out_of_shared_container_home() -> None:
    """The container HOME is shared and 0777; per-run state must be private.

    Codex additionally refuses to create PATH aliases when CODEX_HOME is under
    /tmp, so the runtime dir is created under /var/tmp.
    """
    text = RUN_SCRIPT.read_text()
    assert "mktemp -d /var/tmp/abc-bench-codex.XXXXXX" in text
    assert 'export CODEX_HOME="${CODEX_RUNTIME_DIR}/codex"' in text
    assert 'export HOME="${CODEX_STATE_HOME}"' in text
    # The runtime dir must not leak between attempts.
    assert "trap 'rm -rf \"${CODEX_RUNTIME_DIR}\"' EXIT" in text


def test_run_script_reads_its_token_from_an_env_var_not_argv() -> None:
    """The credential must never appear in argv or in run metadata.

    Codex reads the bearer token from the variable named by the provider's
    env_key. (This harness previously had to leave env_key unset to dodge an
    automaton proxy bug that appended a second Authorization header; that bug is
    fixed, so the straightforward wiring is used again.)
    """
    text = RUN_SCRIPT.read_text()
    setting_lines = [
        ln for ln in text.splitlines() if "abc-bench-proxy.env_key" in ln and "-c " in ln
    ]
    assert len(setting_lines) == 1, setting_lines
    # The value is the *name* of the variable, not the token itself.
    assert '${API_KEY_ENV}' in setting_lines[0]


def test_run_script_defaults_to_responses_wire_api() -> None:
    text = RUN_SCRIPT.read_text()
    assert 'DEFAULT_API = "responses"' in text
    assert '"chat_completions": "chat"' in text


def test_run_script_rejects_unknown_api_style() -> None:
    """The harness owns the `api` vocabulary; core assigns it no meaning."""
    text = RUN_SCRIPT.read_text()
    assert "codex harness does not support api" in text


# --- argument and config validation (runs the real script) ----------------


def _run_script(tmp_path: Path, *args: str, models_yaml: str | None = None) -> subprocess.CompletedProcess:
    """Invoke run_benchmark.sh far enough to exercise its validation."""
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "sys.md").write_text("# system\n")
    (tmp_path / "task.md").write_text("# task\n")
    (tmp_path / "harness.yaml").write_text("{}\n")
    if models_yaml is not None:
        (tmp_path / "models.yaml").write_text(models_yaml)
    # The container provides `python` (conda) on PATH; mirror that here so the
    # script's inline config parser can run.
    python_dir = str(Path(sys.executable).parent)
    return subprocess.run(
        ["bash", str(RUN_SCRIPT), *args],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{python_dir}:/usr/bin:/bin",
            "ABC_BENCH_MODEL_PROXY_API_KEY": "token",
        },
    )


def _standard_args(tmp_path: Path) -> list[str]:
    return [
        "--workspace", str(tmp_path / "ws"),
        "--task-prompt", str(tmp_path / "task.md"),
        "--system-prompt", str(tmp_path / "sys.md"),
        "--models-config", str(tmp_path / "models.yaml"),
        "--harness-config", str(tmp_path / "harness.yaml"),
        "--output-dir", str(tmp_path / "out"),
    ]


def test_script_rejects_unknown_argument(tmp_path: Path) -> None:
    proc = _run_script(tmp_path, "--bogus-flag", "x")
    assert proc.returncode == 1
    assert "Unknown argument: --bogus-flag" in proc.stderr


def test_script_reports_missing_required_argument(tmp_path: Path) -> None:
    proc = _run_script(tmp_path, "--workspace", str(tmp_path / "ws"))
    assert proc.returncode == 1
    assert "missing required argument" in proc.stderr
    assert "Usage:" in proc.stderr


def test_script_fails_when_no_models_are_exposed(tmp_path: Path) -> None:
    """A subshell SystemExit must not silently degrade the invocation."""
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml="models: []\n")
    assert proc.returncode == 1
    assert "exposes no models" in proc.stderr
    assert "failed to parse model configuration" in proc.stderr


def test_script_rejects_unsupported_api_value(tmp_path: Path) -> None:
    models_yaml = (
        "proxy_base_url: http://127.0.0.1:18080\n"
        "api_key_env: ABC_BENCH_MODEL_PROXY_API_KEY\n"
        "models:\n"
        "- name: m\n"
        "  model: up/m\n"
        "  base_url: http://127.0.0.1:18080\n"
        "  api: bogus_style\n"
    )
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml=models_yaml)
    assert proc.returncode == 1
    assert "does not support api 'bogus_style'" in proc.stderr


def test_script_rejects_invalid_harness_config_values(tmp_path: Path) -> None:
    models_yaml = (
        "proxy_base_url: http://127.0.0.1:18080\n"
        "api_key_env: ABC_BENCH_MODEL_PROXY_API_KEY\n"
        "models:\n"
        "- name: m\n"
        "  model: up/m\n"
        "  base_url: http://127.0.0.1:18080\n"
    )
    (tmp_path / "harness.yaml").write_text("reasoning_effort: turbo\n")
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml=models_yaml)
    assert proc.returncode == 1
    # Config parsing happens before the codex binary is resolved, so an invalid
    # value is reported even on a host with no codex installed. Accept the
    # binary-missing error too so this passes outside the runtime container.
    assert (
        "invalid reasoning_effort" in proc.stderr
        or "codex binary not found" in proc.stderr
    ), proc.stderr


def test_script_validates_config_before_requiring_the_binary(tmp_path: Path) -> None:
    """An operator misconfiguration should not be masked by a missing binary."""
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml="models: []\n")
    assert "failed to parse model configuration" in proc.stderr
    assert "codex binary not found" not in proc.stderr


# --- event renderer -------------------------------------------------------


def _render(events: list[str], tmp_path: Path) -> tuple[str, dict]:
    events_file = tmp_path / "events.jsonl"
    summary_file = tmp_path / "summary.json"
    proc = subprocess.run(
        [sys.executable, str(HARNESS_DIR / "lib" / "render_events.py"), str(events_file), str(summary_file)],
        input="\n".join(events) + "\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, json.loads(summary_file.read_text())


def test_renderer_preserves_raw_stream_and_extracts_usage(tmp_path: Path) -> None:
    events = [
        json.dumps({"type": "thread.started", "thread_id": "t-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done thinking"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        ),
    ]
    stdout, summary = _render(events, tmp_path)

    # Raw stream is preserved byte-for-byte, line by line.
    raw_lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert raw_lines == events

    assert summary["thread_id"] == "t-1"
    assert summary["last_terminal_event"] == "turn.completed"
    assert summary["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert summary["malformed_lines"] == 0
    assert "done thinking" in stdout


def test_renderer_reports_command_output_once(tmp_path: Path) -> None:
    """item.started announces the command; item.completed carries the result."""
    events = [
        json.dumps(
            {
                "type": "item.started",
                "item": {"id": "i1", "type": "command_execution", "command": "ls -la"},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "ls -la",
                    "exit_code": 0,
                    "aggregated_output": "total 0",
                },
            }
        ),
    ]
    stdout, _ = _render(events, tmp_path)

    assert len(re.findall(r"^\[command\] ls -la$", stdout, re.M)) == 1
    assert "[command exit=0]" in stdout
    assert "total 0" in stdout


def test_renderer_counts_malformed_lines_without_failing(tmp_path: Path) -> None:
    """A truncated or non-JSON line must be surfaced, not crash the run."""
    events = [
        json.dumps({"type": "thread.started", "thread_id": "t-2"}),
        "this is not json",
        '{"type": "turn.completed", "usage": {"input_tokens": 1',
    ]
    stdout, summary = _render(events, tmp_path)

    assert summary["malformed_lines"] == 2
    assert summary["last_terminal_event"] is None
    assert "this is not json" in stdout


def test_renderer_records_turn_failed_and_errors(tmp_path: Path) -> None:
    events = [
        json.dumps({"type": "item.completed", "item": {"type": "error", "message": "boom"}}),
        json.dumps({"type": "turn.failed", "error": {"message": "upstream exploded"}}),
    ]
    _, summary = _render(events, tmp_path)

    assert summary["last_terminal_event"] == "turn.failed"
    assert any("boom" in e for e in summary["errors"])


def test_renderer_preserves_unknown_event_types(tmp_path: Path) -> None:
    """A future Codex version must not silently lose events here."""
    events = [json.dumps({"type": "some.future.event", "payload": {"a": 1}})]
    stdout, summary = _render(events, tmp_path)

    assert summary["event_counts"]["some.future.event"] == 1
    assert "some.future.event" in stdout
    assert (tmp_path / "events.jsonl").read_text().strip() == events[0]
