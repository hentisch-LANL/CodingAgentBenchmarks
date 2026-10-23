# Codex Harness

The `codex` harness runs the [OpenAI Codex CLI](https://developers.openai.com/codex/cli)
non-interactively against a staged task workspace. Codex owns the agent loop and
executes its tools inside the same runtime container as the rest of the
benchmark; ABC Bench supplies the prompt, routes model traffic through the model
proxy, captures the event stream, and grades the resulting workspace.

## Verification status

Verified against a live endpoint (`azure-gov.gpt-5.6-sol` on
`api.example.com`, codex-cli 0.155.1) on a host with working user
namespaces, through the real `abc-bench build-container` / `abc-bench run` path:

- the layered build `base -> harness -> runtime` completes, and the resulting
  container runs under `ch-run` with `codex`, conda `python`, `pyyaml`, `numpy`,
  `scipy`, and `ripgrep` all present
- `smoke_test/hello-world-python` → `PASS`
- `smoke_test/eos-metropolis-1953` → `PASS`
- `smoke_test/hello-world-prank-python` → `FAIL`, which is the **correct**
  outcome: that task's prompt and hidden verifier disagree by design, so a pass
  would indicate a broken verification path
- timeout path → Codex killed mid-run, harness exits `124`, no terminal event
  recorded, runtime dir removed by the `trap`
- usage accounting → tokens, cache-read/cache-write buckets, and throughput all
  populated in `usage.json`
- every config key was checked against `codex --strict-config`, and
  `web_search="disabled"` was confirmed effective by inspecting the tool list in
  the captured outbound request body (`tools.web_search=false` was confirmed
  *ineffective* the same way)
- the proxy-side tool guard fires through the real sandboxed path: with the
  harness deliberately switched to the ineffective key, Codex declared
  `web_search`, the proxy returned `403` with zero upstream attempts logged, and
  the run was recorded as `HARNESS_ERROR` rather than graded

!!! note "Host prerequisites"
    Two things must be true of the host, and neither is something the container
    can provide:

    - **unprivileged user namespaces must work.** `ch-run` needs them to create
      its user and mount namespaces. Under Docker this means
      `--security-opt seccomp=unconfined` (the default profile blocks
      `unshare(CLONE_NEWUSER)`); on the host,
      `/proc/sys/user/max_user_namespaces` must be non-zero.
    - **`iproute2` must be installed.** The sandbox helper runs
      `ip link set lo up` to bring up loopback inside the network namespace, so
      with `sandbox_network: true` a missing `ip` fails the run.

    On a TLS-intercepting network the harness layer additionally needs the
    intercepting CA in its trust store: GitHub redirects release downloads to
    `release-assets.githubusercontent.com`, which may be intercepted even when
    `github.com` is not, producing
    `curl: (60) unable to get local issuer certificate` during the build.

## Files

```text
harnesses/codex/
  harness.yaml         # id + container_template
  run_benchmark.sh     # the standard harness entrypoint
  system_prompt.md     # task-agnostic system instructions
  lib/
    render_events.py   # Codex JSONL -> readable transcript + summary

containers/harnesses/codex.Dockerfile.jinja2
```

## Container layer

Codex publishes statically linked musl binaries per platform, so the harness
layer installs the release archive directly instead of `npm install -g
@openai/codex`. That avoids pulling an entire Node toolchain into the image
purely to run a launcher script. The binary is symlinked to `/usr/local/bin/codex`,
and a build-time `codex --version` smoke check makes a broken install fail the
build rather than a benchmark run.

The version is intentionally **not pinned**: the template fetches
`releases/latest/download/...` so runs exercise current Codex behavior. For a
frozen campaign, change the URL to a specific `download/rust-vX.Y.Z` tag.

`codex --version` and both `--help` texts are archived in
`/opt/codex/build-info/` inside the image for run provenance.

## Invocation

Global flags must precede the `exec` subcommand. The effective invocation is:

```bash
codex --ask-for-approval never \
  -c 'model_provider="abc-bench-proxy"' \
  -c 'model_providers.abc-bench-proxy.base_url="http://127.0.0.1:18080"' \
  -c 'model_providers.abc-bench-proxy.env_key="ABC_BENCH_MODEL_PROXY_API_KEY"' \
  -c 'model_providers.abc-bench-proxy.wire_api="responses"' \
  -c 'web_search="disabled"' \
  exec --sandbox danger-full-access --cd /workspace \
  --skip-git-repo-check --model <upstream-model-id> \
  --ephemeral --ignore-user-config --json --color never \
  --output-last-message .../final_message.txt - < combined_prompt.md
```

The prompt is fed on **stdin** (`-`), assembled in the standard three layers:
system prompt, task-group instructions (when the group ships
`group_prompt.md`), then the per-task prompt.

`--ephemeral` suppresses persisted session rollout files and
`--ignore-user-config` ignores any `config.toml` left in `CODEX_HOME`, so
attempts do not inherit state from each other.

## Model wiring and the `api` field

Codex talks to the ABC Bench model proxy as a custom provider. The provider's
`wire_api` is chosen from the model's opaque `api` field:

| `api` value | Codex `wire_api` |
| --- | --- |
| `responses` (default) | `responses` |
| `chat_completions` | `chat` |

Unlike the OpenCode harness, this harness defaults to `responses`, because Codex
is built around the Responses API and loses reasoning-item continuity between
turns on the Chat Completions path. Unknown `api` values are rejected rather
than silently accepted.

!!! note "History: the duplicate `Authorization` header"
    This harness originally had to leave the provider's `env_key` unset, because
    automaton's proxy assigned a capitalized `Authorization` header without
    removing a client-supplied lowercase `authorization`. Codex (via `reqwest`)
    always sends the lowercase form, so requests went upstream with two auth
    headers and the AWS load balancer rejected them with an opaque HTML
    `400 Bad Request` before the model saw anything.

    That proxy bug is fixed (it now replaces rather than appends the header), so
    the harness uses the straightforward `env_key` wiring again.

## Sandboxing

Codex has its own command sandbox built on bubblewrap plus seccomp. That
generally cannot start nested inside Charliecloud's user namespace, and
ABC Bench already confines the run with the container plus a network namespace.
The harness therefore defaults to `--sandbox danger-full-access` and treats the
container as the security boundary.

This is a deliberate configuration, not an automatic fallback: the harness never
silently downgrades its sandbox mode. Override it explicitly through
`harness_config` if a host does support nested bubblewrap.

## Per-run state

Codex stores config, credentials, logs, caches, and history under `CODEX_HOME`.
The harness creates a fresh `mktemp -d` runtime root per invocation and removes
it on exit, with `HOME` and `CODEX_HOME` pointed inside it.

Two constraints drive the location:

- the container `HOME` (`/var/tmp/automaton-home`) is shared and mode `0777`, so
  agent state there would collide across parallel attempts;
- Codex refuses to create its PATH helper aliases when `CODEX_HOME` is under
  `/tmp`, and requires the directory to already exist.

The runtime root is therefore created under `/var/tmp`.

## Request retries

Codex's own request retries (`request_max_retries` / `stream_max_retries`) are
left at their defaults.

These were previously pinned to `0`, on the reasoning that the model proxy
"already retries dropped upstream requests." That is only half true, and the
half that is missing matters: automaton's proxy retries **transport** failures (a
refused or dropped connection) but forwards an upstream **HTTP** error verbatim —
a `503 Overloaded` is handed straight to the client. With the CLI's retries
disabled, nothing rode out a transient `503`, so a single blip killed a run that
had already done real work.

The two layers cover disjoint failure classes and so do not compound. An endpoint
that genuinely will not answer still ends the run as a harness failure; retrying
never converts a dead endpoint into a graded result.

## Web search

The harness sets `web_search="disabled"`.

`web_search` is an **enum** (`disabled | cached | indexed | live`), not a boolean,
and `disabled` is the current, first-class way to turn the tool off.

!!! danger "Do not switch this to `tools.web_search=false`"
    `tools.web_search` is a *different* key. Setting it `false` parses, validates
    under `--strict-config`, emits no deprecation warning, and **does not remove
    the tool** on codex-cli 0.155.1 — verified by inspecting the outbound request
    body. Only `web_search="disabled"` actually drops it.

    (The setting Codex does report as deprecated is `[features].web_search`, a
    legacy *enable* toggle. Neither key documented here is deprecated.)

### Why this is a containment control, not a tidiness setting

Codex declares web search as a **native provider tool**:

```json
{"type": "web_search", "external_web_access": true}
```

Note `"type": "web_search"`, not `"type": "function"`. Codex's other tools
(`exec_command`, `write_stdin`, …) are `function` tools that Codex executes
*inside the container*. A native tool is executed by the **model provider's own
infrastructure**, within the single HTTP response the proxy is already
forwarding.

The consequence is that the sandbox cannot see it:

- no second connection is opened from inside the container, so the run's network
  namespace has nothing to filter;
- `model_proxy.log` records one ordinary `200`;
- the harness transcript shows no tool call.

This was confirmed empirically against `api.example.com`: a request
carrying that tool entry came back with `web_search_call` output items,
`tool_usage.web_search: {"num_requests": 3}`, and — when asked to fetch
`https://example.com` — the live page's actual `<h1>` text.

For a benchmark that may eventually run export-controlled code, that is an
untraceable egress path in both directions: remote content can come in, and
prompt content can leave inside a provider-side search query.

Disabling the tool in the harness is therefore the primary control, and
automaton's model proxy independently enforces a second one.

## Provider-executed tool policy (proxy-side)

The model proxy **rejects any request declaring a provider-executed tool unless
that tool is explicitly allowed**, and it does so *before* opening the upstream
connection, so a blocked request never reaches the provider.

This is deliberately defense in depth: it does not rely on the harness being
configured correctly, or on a given agent's config keys continuing to behave
across version bumps. Since the Codex CLI is intentionally unpinned, that second
property matters.

Default behavior:

```yaml
defaults:
  # Absent/empty (the default) blocks every provider-executed tool.
  allowed_provider_executed_tools: []
```

To permit one, name it explicitly:

```yaml
defaults:
  allowed_provider_executed_tools:
    - web_search
```

Recognized types include the OpenAI Responses native tools (`web_search`,
`code_interpreter`, `file_search`, `computer_use`, `image_generation`,
`local_shell`, `mcp`) and the Anthropic server-side tools
(`web_search_20250305`, `web_fetch_20250910`, `code_execution_20250522`, …). See
`PROVIDER_EXECUTED_TOOL_TYPES` in `automaton.proxy`.

The check **fails closed**: a tool whose `type` is unrecognized *and* which
carries no local call signature (a `name` plus `parameters`/`input_schema`) is
treated as provider-executed. A new native tool in a future agent release is
blocked by default rather than passing silently. Locally executed `function` /
`custom` tools are always allowed, and Responses `namespace` tools are recursed
into so a native tool cannot hide inside one.

When a request is blocked:

- the sandbox receives an HTTP `403` with a `provider_executed_tool_blocked`
  error body (the broker answers rather than hanging the agent);
- `model_proxy.log` gets a `[tool-policy] BLOCKED` line explaining what to do;
- the run is recorded as `HARNESS_ERROR` with
  `failure_phase: "provider_executed_tool_blocked"`.

That last point is important: a blocked tool is **never** graded as a benchmark
`FAIL`, including when the agent retries without the tool and exits `0`. A
containment failure must not be able to produce a plausible-looking score.

## `harness_config` options

All optional:

```yaml
agents:
  codex-high-effort:
    harness: codex
    models:
      - azure-gov-gpt-5.6-sol
    harness_config:
      reasoning_effort: high        # minimal|low|medium|high|xhigh
      sandbox_mode: workspace-write # read-only|workspace-write|danger-full-access
      context_window: 272000        # override when the model is unknown to Codex
```

`context_window` is useful because Codex emits
`Model metadata for <id> not found. Defaulting to fallback metadata` for models
it does not ship metadata for, which can mean a conservative context limit.

Invalid values are rejected with a clear error rather than being passed through.

## Output artifacts

In addition to the standard `harness.log`:

```text
harness_output/
  combined_prompt.md
  harness.log
  codex/
    events.jsonl          # raw Codex JSONL event stream, verbatim
    events_summary.json   # terminal event, thread id, usage, malformed count
    final_message.txt     # final assistant message
    stderr.log            # CLI diagnostics, kept out of the event stream
```

`lib/render_events.py` tees the raw stream to `events.jsonl` while printing a
readable transcript, so a long run is observable live in `harness.log` and
`ch-run.log`. It preserves unknown event types instead of dropping them, and
always exits 0 — the harness reads Codex's status from `PIPESTATUS[0]` so a
rendering problem cannot masquerade as an agent failure.

## Failure semantics

| Condition | Harness exit | Runner status |
| --- | --- | --- |
| `turn.completed`, exit 0 | 0 | graded by the verifier |
| wall-clock timeout | 124 | `FAIL` / timeout |
| `turn.failed` with process exit 0 | 1 | `HARNESS_ERROR` |
| non-zero Codex exit | that code | `HARNESS_ERROR` |

A `turn.failed` that still exits 0 is reported as a harness failure rather than
being graded as a wrong answer, so endpoint and transport problems are not
silently converted into semantic test failures.

## Testing

`tests/test_codex_harness.py` covers the harness contract, the container
template, and the event renderer without needing Charliecloud or a live model.

`tests/test_provider_executed_tools.py` covers the proxy-side tool guard:
detection (including namespace recursion, case-insensitivity, and the
fail-closed path for unknown tools), enforcement before the upstream connection
is opened, the opt-in allowlist, and the failure classification.

`tools_dev/harness_smoke.py` runs `run_benchmark.sh` against a real
`ModelProxy` and a real task, then runs the hidden verifier. It exists for
environments where `ch-run` cannot create namespaces (for example a Docker
sandbox whose seccomp profile blocks `unshare`), and covers everything except
`ch-run` itself, the bind mounts, and the clean container environment:

```bash
python3 tools_dev/harness_smoke.py --agent codex-default --task hello-world-python
```

`tools_dev/prove_tool_guard.py` needs a live endpoint and a `codex` binary. It
runs Codex twice through a real proxy — once configured correctly, once
deliberately leaking `web_search` — and asserts the second is blocked with no
upstream attempt:

```bash
python3 tools_dev/prove_tool_guard.py
```

The full path, once namespaces are available:

```bash
abc-bench build-container codex
abc-bench build-container codex/smoke_test
abc-bench run --task smoke_test/hello-world-python --agent codex-default
```

## See Also

- [Adding Harnesses](adding-harnesses.md)
- [Harness Interface](../reference/harness-interface.md)
- [Container System](container-system.md)
