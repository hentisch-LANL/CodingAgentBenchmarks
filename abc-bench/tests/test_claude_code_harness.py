"""Tests for the Claude Code harness assets.

These tests cover the parts of the Claude Code harness that can be checked
without Charliecloud or a live model endpoint: the harness metadata/contract,
the container template, and the pure-Python event renderer.

The `claude -p` invocation itself is exercised by
``tools_dev/claude_code_harness_smoke.py``, which needs a real model endpoint.

Several assertions here pin behavior that was established empirically against
claude-code 2.1.278/2.1.280 and is easy to "simplify" back into a bug; each one
says what breaks if it is removed.
"""

from pathlib import Path
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automaton import Harness  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HARNESS_DIR = REPO / "harnesses" / "claude-code"
RUN_SCRIPT = HARNESS_DIR / "run_benchmark.sh"
RENDERER = HARNESS_DIR / "lib" / "render_events.py"
TEMPLATE = REPO / "containers" / "harnesses" / "claude-code.Dockerfile.jinja2"


def test_harness_loads_and_validates() -> None:
    harness = Harness("claude-code", HARNESS_DIR)
    assert harness.validate() == []
    # container_template drives containers/harnesses/<name>.Dockerfile.jinja2
    assert harness.container_template == "claude-code"
    assert harness.run_script == RUN_SCRIPT
    assert harness.system_prompt_file == HARNESS_DIR / "system_prompt.md"


def test_container_template_exists_for_declared_name() -> None:
    harness = Harness("claude-code", HARNESS_DIR)
    template = REPO / "containers" / "harnesses" / f"{harness.container_template}.Dockerfile.jinja2"
    assert template.is_file()


def test_container_template_substitutes_parent_image_and_installs_claude() -> None:
    text = TEMPLATE.read_text()
    # Rendering is a literal str.replace of exactly "{{ parent_image }}".
    assert "FROM {{ parent_image }}" in text
    # The binary must land on an absolute path the run script can rely on.
    assert "/usr/local/bin/claude" in text
    # A build-time smoke check keeps a broken install from surfacing mid-run.
    assert "claude --version" in text


def test_container_template_verifies_the_published_checksum() -> None:
    """A truncated or substituted download must fail the build, not a run."""
    text = TEMPLATE.read_text()
    assert "manifest.json" in text
    assert "sha256sum -c -" in text


def test_container_template_does_not_pipe_the_installer_into_bash() -> None:
    """`claude install` opens a TUI and hangs with no terminal attached.

    The template fetches the release binary directly instead. Reintroducing the
    installer would make the image build hang rather than fail. Comment lines are
    ignored so the rationale can still mention the installer by name.
    """
    code = "\n".join(
        line for line in TEMPLATE.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "install.sh" not in code
    assert "claude install" not in code


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
    """`$?` after a `| tee` pipeline captures tee's status, not claude's.

    Getting this wrong makes every timeout and agent failure look like success.
    """
    assert 'EXIT_CODE="${PIPESTATUS[0]}"' in RUN_SCRIPT.read_text()


def test_run_script_exits_124_on_timeout() -> None:
    text = RUN_SCRIPT.read_text()
    assert 'timeout "${TIMEOUT_SECONDS}"' in text
    assert "exit 124" in text


def test_run_script_guards_optional_group_prompt() -> None:
    """--group-prompt is omitted when the group ships no group_prompt.md."""
    text = RUN_SCRIPT.read_text()
    assert '[[ -n "${GROUP_PROMPT}" && -f "${GROUP_PROMPT}" ]]' in text


def test_run_script_grants_permission_for_the_tools_it_allows() -> None:
    """`--tools` controls availability; `--allowedTools` grants permission.

    With `--permission-mode dontAsk`, passing only `--tools` leaves the agent
    able to see Write/Edit/Bash but denied when it calls them. It then reports
    that it cannot edit the file and exits 0, which grades as a plausible FAIL
    while actually being a harness misconfiguration. Verified: this exact bug
    produced a FAIL on hello-world-python.

    The two lists are deliberately different: the offered set is a superset that
    additionally contains the web tools, which are refused on call.
    """
    text = RUN_SCRIPT.read_text()
    assert "--tools ${OFFERED_TOOLS//,/ }" in text
    assert "--allowedTools ${ALLOWED_TOOLS//,/ }" in text


def test_run_script_does_not_treat_a_denial_as_a_failure() -> None:
    """Denials are an intended outcome, so they must not fail the run.

    The harness refuses the web tools on purpose, and an intended policy refusal
    is indistinguishable in this event from a misconfiguration, so there is no
    sound way to branch on it. The count is recorded for observability only.
    """
    text = RUN_SCRIPT.read_text()
    # Reported...
    assert "Permission denials: ${PERMISSION_DENIALS:-0}" in text
    # ...but never used to fail the run.
    assert "harness permission misconfiguration" not in text
    assert 'PERMISSION_DENIALS}" -gt 0' not in text


def test_run_script_offers_web_tools_but_refuses_them() -> None:
    """Web tools are offered and denied, not withheld.

    A transparent refusal lets the agent report honestly that it could not
    consult the web, instead of silently reasoning without a capability it was
    never told about. The refusal is NOT the containment boundary -- the agent
    will try `curl` instead (observed) -- `sandbox_network`'s loopback-only
    namespace is what actually blocks egress.
    """
    text = RUN_SCRIPT.read_text()
    allow_list = text.split("DEFAULT_TOOLS")[1].split("]")[0]
    assert "WebSearch" not in allow_list
    assert "WebFetch" not in allow_list
    refused = text.split("REFUSED_TOOLS")[1].split("]")[0]
    assert "WebSearch" in refused
    assert "WebFetch" in refused


def test_run_script_does_not_suppress_the_cli_request_retries() -> None:
    """The proxy retries transport failures only; HTTP 5xx reaches the client.

    automaton's proxy forwards an upstream 503 verbatim, so the CLI is the only
    layer that can ride out a transient one. Pinning CLAUDE_CODE_MAX_RETRIES=0
    made a single 503 kill a run that had already done real work.
    """
    text = RUN_SCRIPT.read_text()
    assert "CLAUDE_CODE_MAX_RETRIES=0" not in text


def test_run_script_keeps_state_out_of_shared_container_home() -> None:
    """The container HOME is shared and 0777; per-run state must be private.

    Claude Code writes config, cache, and a projects/ tree under HOME, so
    concurrent attempts on one node would collide.
    """
    text = RUN_SCRIPT.read_text()
    assert "mktemp -d /var/tmp/abc-bench-claude-code.XXXXXX" in text
    assert 'export HOME="${CLAUDE_STATE_HOME}"' in text
    assert 'export CLAUDE_CONFIG_DIR="${CLAUDE_STATE_HOME}/.claude"' in text
    # The runtime dir must not leak between attempts.
    assert "trap 'rm -rf \"${CLAUDE_RUNTIME_DIR}\"' EXIT" in text


def test_run_script_reads_its_token_from_an_env_var_not_argv() -> None:
    """The credential must never appear in argv or in run metadata."""
    text = RUN_SCRIPT.read_text()
    assert 'export ANTHROPIC_API_KEY="${!API_KEY_ENV}"' in text
    # An inherited gateway token would change which credential is sent.
    assert "unset ANTHROPIC_AUTH_TOKEN" in text


def test_run_script_ignores_workspace_supplied_settings_and_mcp() -> None:
    """A task workspace must not be able to reconfigure the harness."""
    text = RUN_SCRIPT.read_text()
    assert "--setting-sources user" in text
    assert "--strict-mcp-config" in text
    assert '--mcp-config \'{"mcpServers":{}}\'' in text


def test_run_script_does_not_persist_sessions_between_attempts() -> None:
    assert "--no-session-persistence" in RUN_SCRIPT.read_text()


def test_run_script_defaults_to_the_anthropic_messages_api() -> None:
    text = RUN_SCRIPT.read_text()
    assert 'DEFAULT_API = "anthropic_messages"' in text


def test_run_script_rejects_unknown_api_style() -> None:
    """The harness owns the `api` vocabulary; core assigns it no meaning."""
    text = RUN_SCRIPT.read_text()
    assert "claude-code harness does not support api" in text


# --- argument and config validation (runs the real script) ----------------


def _run_script(tmp_path: Path, *args: str, models_yaml: str | None = None) -> subprocess.CompletedProcess:
    """Invoke run_benchmark.sh far enough to exercise its validation."""
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "sys.md").write_text("# system\n")
    (tmp_path / "task.md").write_text("# task\n")
    if not (tmp_path / "harness.yaml").exists():
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


def _models_yaml(extra: str = "") -> str:
    return (
        "proxy_base_url: http://127.0.0.1:18080\n"
        "api_key_env: ABC_BENCH_MODEL_PROXY_API_KEY\n"
        "models:\n"
        "- name: m\n"
        "  model: up/m\n"
        "  base_url: http://127.0.0.1:18080\n"
        + extra
    )


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
    """Claude Code cannot speak an OpenAI-compatible API at all."""
    proc = _run_script(
        tmp_path,
        *_standard_args(tmp_path),
        models_yaml=_models_yaml("  api: chat_completions\n"),
    )
    assert proc.returncode == 1
    assert "does not support api 'chat_completions'" in proc.stderr


def test_script_rejects_invalid_effort(tmp_path: Path) -> None:
    (tmp_path / "harness.yaml").write_text("effort: turbo\n")
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml=_models_yaml())
    assert proc.returncode == 1
    # Config parsing happens before the claude binary is resolved, so an invalid
    # value is reported even on a host with no claude installed. Accept the
    # binary-missing error too so this passes outside the runtime container.
    assert (
        "invalid effort" in proc.stderr
        or "claude binary not found" in proc.stderr
    ), proc.stderr


def test_script_rejects_non_numeric_max_turns(tmp_path: Path) -> None:
    (tmp_path / "harness.yaml").write_text("max_turns: lots\n")
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml=_models_yaml())
    assert proc.returncode == 1
    assert (
        "max_turns must be an integer" in proc.stderr
        or "claude binary not found" in proc.stderr
    ), proc.stderr


def test_script_rejects_non_positive_max_budget(tmp_path: Path) -> None:
    (tmp_path / "harness.yaml").write_text("max_budget_usd: 0\n")
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml=_models_yaml())
    assert proc.returncode == 1
    assert (
        "max_budget_usd must be a positive number" in proc.stderr
        or "claude binary not found" in proc.stderr
    ), proc.stderr


def test_script_rejects_malformed_tool_names(tmp_path: Path) -> None:
    """A tool name with whitespace would silently become two CLI arguments."""
    (tmp_path / "harness.yaml").write_text('tools: ["Bash Read"]\n')
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml=_models_yaml())
    assert proc.returncode == 1
    assert (
        "invalid tool name" in proc.stderr
        or "claude binary not found" in proc.stderr
    ), proc.stderr


def test_script_validates_config_before_requiring_the_binary(tmp_path: Path) -> None:
    """An operator misconfiguration should not be masked by a missing binary."""
    proc = _run_script(tmp_path, *_standard_args(tmp_path), models_yaml="models: []\n")
    assert "failed to parse model configuration" in proc.stderr
    assert "claude binary not found" not in proc.stderr


# --- event renderer -------------------------------------------------------


def _render(events: list[str], tmp_path: Path) -> tuple[str, dict]:
    events_file = tmp_path / "events.jsonl"
    summary_file = tmp_path / "summary.json"
    proc = subprocess.run(
        [sys.executable, str(RENDERER), str(events_file), str(summary_file)],
        input="\n".join(events) + "\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, json.loads(summary_file.read_text())


def _init_event() -> str:
    return json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "s-1",
            "claude_code_version": "2.1.280",
            "model": "aws-gov.claude-opus-5",
            "permissionMode": "dontAsk",
            "tools": ["Bash", "Read", "Write"],
        }
    )


def _result_event(**overrides) -> str:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "session_id": "s-1",
        "num_turns": 4,
        "total_cost_usd": 0.25,
        "result": "done",
        "usage": {
            "input_tokens": 8,
            "cache_creation_input_tokens": 31060,
            "cache_read_input_tokens": 92264,
            "output_tokens": 476,
            "output_tokens_details": {"thinking_tokens": 11},
        },
        "modelUsage": {"aws-gov.claude-opus-5": {"costUSD": 0.25}},
        "permission_denials": [],
    }
    event.update(overrides)
    return json.dumps(event)


def test_renderer_preserves_raw_stream_and_extracts_usage(tmp_path: Path) -> None:
    events = [_init_event(), _result_event()]
    stdout, summary = _render(events, tmp_path)

    # The raw stream must be byte-preserved for later analysis.
    assert (tmp_path / "events.jsonl").read_text().splitlines() == events
    assert summary["session_id"] == "s-1"
    assert summary["claude_code_version"] == "2.1.280"
    assert summary["usage"]["cache_read_input_tokens"] == 92264
    assert summary["num_turns"] == 4
    assert summary["malformed_lines"] == 0
    assert "[init] session=s-1" in stdout


def test_renderer_separates_is_error_from_result_subtype(tmp_path: Path) -> None:
    """An API error reports subtype "success" while setting is_error true.

    Reading the subtype as a verdict would let a run that never reached the model
    be graded as a normal completion.
    """
    events = [
        _init_event(),
        _result_event(
            is_error=True,
            terminal_reason="api_error",
            api_error_status=503,
            result="API Error: Overloaded",
        ),
    ]
    _, summary = _render(events, tmp_path)

    assert summary["result_subtype"] == "success"
    assert summary["is_error"] is True
    assert summary["terminal_reason"] == "api_error"
    assert summary["api_error_status"] == 503
    assert summary["errors"]


def test_renderer_records_permission_denials(tmp_path: Path) -> None:
    events = [
        _init_event(),
        json.dumps(
            {
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Edit",
                "message": "denied by dontAsk",
                "decision_reason_type": "mode",
            }
        ),
        _result_event(),
    ]
    stdout, summary = _render(events, tmp_path)

    assert len(summary["permission_denials"]) == 1
    assert summary["permission_denials"][0]["tool_name"] == "Edit"
    assert "[permission_denied] Edit" in stdout


def test_renderer_counts_api_retries(tmp_path: Path) -> None:
    """A run that only succeeded after fighting a flaky endpoint stays visible."""
    events = [
        _init_event(),
        json.dumps(
            {
                "type": "system",
                "subtype": "api_retry",
                "attempt": 1,
                "error_status": 503,
                "error": "overloaded",
            }
        ),
        _result_event(),
    ]
    stdout, summary = _render(events, tmp_path)

    assert summary["api_retries"] == 1
    assert "[api_retry] attempt=1 status=503" in stdout


def test_renderer_describes_tool_calls_and_results(tmp_path: Path) -> None:
    events = [
        _init_event(),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Reading the file."},
                        {"type": "thinking", "thinking": "consider the stub", "signature": "x"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "python3 t.py", "description": "run tests"},
                        },
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}
                    ]
                },
            }
        ),
        _result_event(),
    ]
    stdout, _ = _render(events, tmp_path)

    assert "[assistant] Reading the file." in stdout
    assert "[thinking] consider the stub" in stdout
    assert "[tool:Bash] python3 t.py (run tests)" in stdout
    assert "[tool_result] ok" in stdout


def test_renderer_does_not_dump_whole_file_contents_for_writes(tmp_path: Path) -> None:
    """A Write call's `content` can be enormous; the path identifies the call."""
    events = [
        _init_event(),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/workspace/s.py", "content": "X" * 5000},
                        }
                    ]
                },
            }
        ),
        _result_event(),
    ]
    stdout, _ = _render(events, tmp_path)

    assert "[tool:Write] /workspace/s.py" in stdout
    assert "X" * 3000 not in stdout


def test_renderer_flags_errored_tool_results(tmp_path: Path) -> None:
    events = [
        _init_event(),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "boom",
                            "is_error": True,
                        }
                    ]
                },
            }
        ),
        _result_event(),
    ]
    stdout, _ = _render(events, tmp_path)

    assert "[tool_result error] boom" in stdout


def test_renderer_counts_malformed_lines_without_failing(tmp_path: Path) -> None:
    """A rendering problem must not masquerade as an agent failure."""
    events = [_init_event(), "not json at all", _result_event()]
    stdout, summary = _render(events, tmp_path)

    assert summary["malformed_lines"] == 1
    assert "[raw] not json at all" in stdout
    # The raw file still holds every line verbatim.
    assert "not json at all" in (tmp_path / "events.jsonl").read_text()


def test_renderer_preserves_unknown_event_types(tmp_path: Path) -> None:
    """A future Claude Code release must not silently lose information."""
    events = [
        _init_event(),
        json.dumps({"type": "some_future_event", "detail": "important"}),
        _result_event(),
    ]
    stdout, summary = _render(events, tmp_path)

    assert "[event:some_future_event]" in stdout
    assert summary["event_counts"]["some_future_event"] == 1


def test_renderer_handles_structured_tool_result_content(tmp_path: Path) -> None:
    """Tool results may be a list of content blocks rather than a string."""
    events = [
        _init_event(),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "line one"}],
                        }
                    ]
                },
            }
        ),
        _result_event(),
    ]
    stdout, _ = _render(events, tmp_path)

    assert "[tool_result] line one" in stdout
