# Claude Code Harness

The `claude-code` harness runs the [Anthropic Claude Code CLI](https://code.claude.com/docs)
non-interactively (`claude -p`) against a staged task workspace. Claude Code owns
the agent loop and executes its tools inside the same runtime container as the
rest of the benchmark; ABC Bench supplies the prompt, routes model traffic
through the model proxy, captures the event stream, and grades the resulting
workspace.

This is the build-only harness: one prompt, one agent, no planner/builder split.

## Verification status

The original development checkout was verified against a live compatible
endpoint (claude-code 2.1.278/2.1.280) on a host with working
user namespaces, through the real `abc-bench build-container` / `abc-bench run`
path. These are historical results, not a validation of your deployment:

- the layered build `base -> harness -> runtime` completes, and the resulting
  container runs under `ch-run` with `claude`, conda `python`, `pyyaml`, `numpy`,
  `scipy`, and `ripgrep` all present
- `smoke_test/hello-world-python` → `PASS`
- `smoke_test/eos-metropolis-1953` → `PASS`
- `smoke_test/hello-world-prank-python` → `FAIL`, which is the **correct**
  outcome: that task's prompt and hidden verifier disagree by design, so a pass
  would indicate a broken verification path
- timeout path (`--timeout 45`) → the CLI is killed mid-run, the harness exits
  `124`, the run is recorded `HARNESS_ERROR`, partial logs and usage are kept, and
  the runtime dir is removed by the `trap`
- usage accounting → tokens, cache-read/cache-write buckets, cost, and throughput
  all populated in `usage.json`. The per-request buckets summed over a run match
  the CLI's own self-reported totals exactly (input, output, cache-read, and
  cache-creation)
- an upstream `503 Overloaded` → recorded as `HARNESS_ERROR`, never graded
- **concurrent attempts are isolated**, verified rather than assumed: a 4-job
  batch across 2 MPI ranks (`--attempts 2` over two tasks) put two attempts of
  the *same* task into flight 0.9 ms apart with fully overlapping runtimes. Each
  got a distinct `/var/tmp` state root and a distinct CLI session id, each usage
  record set was accounted separately with no double counting, workspaces stayed
  separate trees, and every runtime dir was removed by the `trap`

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
    intercepting CA in its trust store: the binary is fetched from
    `downloads.claude.ai`, which may be intercepted even when other hosts are
    not, producing `curl: (60) unable to get local issuer certificate` during the
    build.

## Files

```text
harnesses/claude-code/
  harness.yaml         # id + container_template
  run_benchmark.sh     # the standard harness entrypoint
  system_prompt.md     # task-agnostic system instructions
  settings.json        # per-run CLI settings, copied into the run dir
  lib/
    render_events.py   # Claude Code JSONL -> readable transcript + summary

containers/harnesses/claude-code.Dockerfile.jinja2
```

## Container layer

Claude Code ships a self-contained native binary per platform that embeds its own
Node runtime, so the harness layer needs no Node/npm toolchain. The template
fetches that binary directly from the release channel and symlinks it to
`/usr/local/bin/claude`.

!!! warning "Do not pipe `https://claude.ai/install.sh` into bash"
    The official installer's final step runs `claude install`, which opens a TUI
    and **hangs** with no terminal attached — the image build never finishes
    rather than failing cleanly. The installer also installs into `$HOME`, which
    in this image is shared and shadowed by a bind mount at run time. A test
    (`test_container_template_does_not_pipe_the_installer_into_bash`) pins this.

The published SHA-256 from the release manifest is verified before the binary is
made executable, so a truncated or substituted download fails the build instead of
a benchmark run. A build-time `claude --version` smoke check makes a broken
install fail the build too; it runs with `HOME` redirected to a scratch dir
because the CLI writes user state on first run.

The version is intentionally **not pinned**: the template resolves the `latest`
channel so runs exercise current Claude Code behavior, matching the codex layer.
For a frozen campaign, replace the `latest` lookup with a literal version (the
manifest URL layout is identical).

Provenance is archived in `/opt/claude-code/build-info/`:
`version.txt`, `release-channel-version.txt`, `platform.txt`, `binary.sha256`,
`manifest.json`, and `help.txt`.

## Model wiring and the `api` field

Claude Code speaks **only** the Anthropic Messages API (`/v1/messages`). It cannot
be pointed at an OpenAI-compatible endpoint, so the harness accepts exactly one
value for the opaque `api` hint:

| `api` value | Meaning |
| --- | --- |
| `anthropic_messages` (default) | Anthropic Messages API |

Anything else is rejected up front with a clear error rather than failing later
with an opaque HTTP error.

The CLI is pointed at the proxy with `ANTHROPIC_BASE_URL`, and reads its
credential from `ANTHROPIC_API_KEY`, which the harness sets from the variable
named by `api_key_env`. The token therefore never appears in argv or in run
metadata. `ANTHROPIC_AUTH_TOKEN` is explicitly unset, since an inherited gateway
token would change which credential is sent.

The proxy strips client-sent authentication and injects the configured upstream
credential. For `api: anthropic_messages` it uses `x-api-key`; other API styles
use `Authorization: Bearer <token>`. Configure the provider API style to match
your endpoint and harness.

!!! note "Usage accounting required an automaton change"
    Anthropic's usage shape is not OpenAI's: `input_tokens` counts only the
    *uncached* prompt, with cache reads and writes as separate buckets that must
    be **added** rather than subtracted. Streaming also splits one request's usage
    across events — `message_start` carries the prompt/cache counts while the
    final output count only arrives on `message_delta`. Support for both, plus
    query-string-tolerant endpoint matching (the CLI requests
    `/v1/messages?beta=true`), lives in `automaton.usage` /
    `automaton.proxy`. Without it every run reported zero tokens.

## Permissions and the tool set

The harness passes **both** `--tools` and `--allowedTools`, with deliberately
*different* lists.

!!! danger "Both flags are required"
    `--tools` selects which built-in tools *exist*; `--allowedTools` *grants
    permission* to call them. With `--permission-mode dontAsk`, passing only
    `--tools` leaves the agent able to see `Write`/`Edit`/`Bash` and then denied
    when it calls them. It responds by politely explaining that it cannot edit
    the file and exits `0` — which grades as a perfectly plausible `FAIL` while
    actually being a harness misconfiguration. This was observed on
    `hello-world-python` before the fix.

Non-interactive runs also set `--permission-prompts none`, so anything that would
prompt is denied rather than left hanging forever.

The **allowed** set is the built-in coding tools:

```text
Bash, Read, Write, Edit, Glob, Grep, NotebookEdit
```

The **offered** set is that plus `WebSearch` and `WebFetch`, which are never
allowed. See below.

!!! note "A denial is not a failure"
    Because the harness refuses the web tools on purpose, `permission_denied`
    events are an expected, correct outcome. An intended policy refusal and a
    misconfiguration produce the *same* event, so there is no sound way to branch
    on it — the harness records the count in the event summary for observability
    and does not treat it as an error. (An earlier version failed the run on any
    denial, which would have turned every deliberate web refusal into a spurious
    `HARNESS_ERROR`.)

## Web access

`WebSearch` and `WebFetch` are **offered to the model but never allowed**, so a
call is transparently refused with a message the agent can read:

```text
Permission to use WebSearch has been denied because Claude Code is
running in don't ask mode.
```

This is a deliberate choice of *transparent rejection* over silent withholding.
The agent then states plainly that it could not consult the web, rather than
reasoning without a capability it was never told existed — which makes the
transcript honest about what constrained the run.

!!! warning "The refusal is not the containment boundary"
    An agent denied `WebSearch` will try to route around it. Observed directly:
    given a task needing live data, Claude Code was refused both web tools, then
    ran `curl` against `wttr.in` via Bash and returned real weather data.

    What actually prevents egress is `sandbox_network`'s loopback-only network
    namespace. Verified inside the real runtime container: DNS does not resolve
    and no external host is reachable (`curl: (6) Could not resolve host`). The
    tool policy tells the model "no" in terms it understands; the namespace is
    what makes it true.

### How this differs from Codex's `web_search`

The mechanism is *not* the same as the one the
[codex harness](codex-harness.md#why-this-is-a-containment-control-not-a-tidiness-setting)
guards against, and the difference decides which control matters.

Claude Code declares all of its tools — `WebSearch` and `WebFetch` included — as
ordinary **client-executed** tools. On the wire they carry a `name` and an
`input_schema` and **no `type` field**:

```json
{"name": "WebFetch", "description": "...", "input_schema": {...}}
```

Verified by capturing real outbound request bodies from a Claude Code session:
all 23 declared tools have this shape, and none is a provider-executed
capability.

So they would execute *inside* the container, where the network namespace
confines them and the attempt appears in the transcript. automaton's
provider-executed tool guard correctly does **not** fire on them, because they
are correctly classified as locally executed — no change to that guard was
needed or made.

Anthropic's genuinely provider-executed tools (`web_search_20250305`,
`web_fetch_20250910`) are a separate mechanism: they run on the provider's own
infrastructure inside the model's HTTP response, where neither the namespace nor
the transcript can observe them. Those remain blocked by the proxy, which is the
right layer for them.

## Per-run state

Claude Code stores config, caches, session/debug logs, and a `projects/` tree
under `HOME` and `CLAUDE_CONFIG_DIR`. The harness creates a fresh `mktemp -d`
runtime root per invocation under `/var/tmp` and removes it on exit, with `HOME`,
`CLAUDE_CONFIG_DIR`, and all four `XDG_*` roots pointed inside it.

The container `HOME` (`/var/tmp/automaton-home`) is shared and mode `0777`, so
agent state written there would collide across parallel attempts on one node.
`/var/tmp` rather than `/tmp` also keeps the cache off a small tmpfs.

This was confirmed under real concurrency rather than reasoned about: see the
isolation result in [Verification status](#verification-status).

Sessions are not persisted (`--no-session-persistence`) so attempts cannot inherit
conversation state from each other, and only this run's own settings are loaded
(`--setting-sources user`, `--strict-mcp-config`, `--mcp-config '{"mcpServers":{}}'`)
so a task workspace cannot reconfigure the harness or introduce an MCP server.

Note that the CLI still discovers `CLAUDE.md` from the *workspace*, which is
intentional — it is part of the task input. A fresh home prevents a previous
attempt's memory from carrying over.

## Request retries

Claude Code's own request retries are left at their default (exponential backoff,
honoring `Retry-After`).

automaton's proxy retries only **transport** failures; an upstream HTTP error such
as `503 Overloaded` is forwarded to the client verbatim by design. The CLI is
therefore the only layer that can ride out a transient `5xx`. An earlier version
of this harness set `CLAUDE_CODE_MAX_RETRIES=0`, copying the codex harness, and a
single `503` killed a run that had already done real work.

The two layers cover disjoint failure classes and do not compound. An endpoint
that genuinely will not answer still ends the run as a harness failure; nothing
here turns a dead endpoint into a graded result. `api_retry` events are counted in
the event summary, so a run that only succeeded after fighting a flaky endpoint
stays visible rather than looking clean.

The sandbox has no route off localhost, so update checks, telemetry, and error
reporting are disabled (`DISABLE_AUTOUPDATER`, `DISABLE_UPDATES`,
`DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) rather than paying their timeouts on
every run.

## Claude Code's own Bash sandbox

Claude Code ships an optional bubblewrap-based sandbox for Bash. It cannot nest
inside Charliecloud's user namespace, and ABC Bench already confines the run via
the container plus a network namespace, so it is disabled explicitly in the
per-run `settings.json` (and `CLAUDE_CODE_BUBBLEWRAP=0`).

This is a deliberate configuration, not a silent fallback: the container is the
security boundary.

## `harness_config` options

All optional:

```yaml
agents:
  claude-code-default:
    harness: claude-code
    models:
      - anthropic-claude
    harness_config:
      effort: high              # low | medium | high | xhigh | max
      max_turns: 100            # agent-loop cap, NOT benchmark attempts
      max_budget_usd: 5         # per-run spend cap
      tools:                    # overrides the ALLOWED set (not the offered set)
        - Bash
        - Read
        - Write
        - Edit
        - Glob
        - Grep
```

Invalid values are rejected with a clear error rather than being passed through.
A tool name containing whitespace is rejected too, since it would silently become
two CLI arguments.

`tools` sets the **allowed** list. The web tools are appended to the *offered*
list regardless, so they are always refused-on-call rather than absent; adding
one to `tools` would allow it, which the sandbox would then block at the network
layer anyway.

!!! note "`--max-turns` is a loop cap"
    It bounds the agent loop within one run. It is not a count of independent
    benchmark attempts — use `--attempts` on `run-tasks` for that.

## Output artifacts

In addition to the standard `harness.log`:

```text
harness_output/
  combined_prompt.md
  harness.log
  claude-code/
    events.jsonl          # raw Claude Code JSONL event stream, verbatim
    events_summary.json   # result status, usage, denials, retries, counts
    settings.json         # the settings actually used
    stderr.log            # CLI diagnostics, kept out of the event stream
    logs/                 # the CLI's own debug/session logs, if any
```

`lib/render_events.py` tees the raw stream to `events.jsonl` while printing a
readable transcript, so a long run is observable live in `harness.log` and
`ch-run.log`. It preserves unknown event types instead of dropping them, and
always exits 0 — the harness reads the CLI's status from `PIPESTATUS[0]`, so a
rendering problem cannot masquerade as an agent failure.

A `Write` call's transcript line shows the file path rather than the file's whole
contents, which would otherwise flood the log.

## Failure semantics

| Condition | Harness exit | Runner status |
| --- | --- | --- |
| result `is_error: false`, exit 0 | 0 | graded by the verifier |
| wall-clock timeout | 124 | `HARNESS_ERROR` / timeout |
| result `is_error: true` with process exit 0 | 1 | `HARNESS_ERROR` |
| non-zero CLI exit | that code | `HARNESS_ERROR` |

!!! warning "`subtype: "success"` is not a verdict"
    An API error still reports `"subtype": "success"` while setting
    `"is_error": true` and `"terminal_reason": "api_error"`. Reading the subtype
    alone would let a run that never reached the model be treated as a normal
    completion. The harness and the renderer both key off `is_error`, and a test
    pins the distinction.

Infrastructure problems are therefore not silently converted into semantic test
failures.

## Testing

`tests/test_claude_code_harness.py` covers the harness contract, the container
template, the config validation (by actually running the script), and the event
renderer — without needing Charliecloud or a live model.

`tools_dev/harness_smoke.py` runs `run_benchmark.sh` against a real `ModelProxy`
and a real task, then runs the hidden verifier. It exists for environments where
`ch-run` cannot create namespaces (for example a Docker sandbox whose seccomp
profile blocks `unshare`), and covers everything except `ch-run` itself, the bind
mounts, the clean container environment, and the image build — so the `claude`
binary must be on the host `PATH`:

```bash
python3 tools_dev/harness_smoke.py --agent claude-code-default --task hello-world-python
```

The full path, once namespaces are available:

```bash
abc-bench build-container claude-code
abc-bench build-container claude-code/smoke_test
abc-bench run --task smoke_test/hello-world-python --agent claude-code-default
```

## See Also

- [Adding Harnesses](adding-harnesses.md)
- [Codex Harness](codex-harness.md)
- [Harness Interface](../reference/harness-interface.md)
- [Container System](container-system.md)
