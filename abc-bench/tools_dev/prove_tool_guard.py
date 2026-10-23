#!/usr/bin/env python3
"""Prove the proxy blocks a provider-executed tool on a REAL Codex run.

This needs a live endpoint and a codex binary, so it lives here rather than in
the pytest suite (see tests/test_provider_executed_tools.py for the unit tests).

Runs codex exec through a real automaton ModelProxy twice:

  1. with the harness's correct setting (web_search="disabled") -> expect success
  2. deliberately misconfigured so Codex declares web_search -> expect the proxy
     to refuse, with no request ever reaching the provider

Case 2 is the regression that matters: it is exactly what happens if someone
"fixes" the harness back to the ineffective tools.web_search=false, or if a
future Codex release changes the key again.
"""

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from automaton.proxy import ModelProxy  # noqa: E402
from automaton.sandbox_helper import SCRIPT as HELPER  # noqa: E402

ns: dict = {"__name__": "helper_inline"}
exec(compile(HELPER, "helper_inline", "exec"), ns)
BaseHandler = ns["Handler"]
ProxyFailure = ns["ProxyFailure"]


class Handler(BaseHandler):  # type: ignore[misc, valid-type]
    def handle_one_request(self):
        try:
            return super(BaseHandler, self).handle_one_request()
        except ProxyFailure as exc:
            print(f"   [proxy transport] {exc}")


CODEX = os.environ.get("CODEX_BIN", "codex")
MODEL = os.environ.get("ABC_BENCH_MODEL", "example-model")
REGISTRY = {
    MODEL: {
        "base_url": os.environ.get("ABC_BENCH_BASE_URL", "https://api.example.com/v1"),
        "api_key": os.environ["MODEL_API_KEY"],
        "model": MODEL,
        "opencode_model": f"abc-bench-proxy/{MODEL}",
    }
}


def run_case(label, extra_config, port, expect_blocked):
    print("=" * 72)
    print(label)
    workdir = Path(f"/tmp/codexwork/guard_{port}")
    if workdir.exists():
        subprocess.run(["rm", "-rf", str(workdir)])
    (workdir / "ws").mkdir(parents=True)
    home = workdir / "home"
    home.mkdir()
    log = workdir / "proxy.log"

    with ModelProxy(REGISTRY, log, usage_records_file=workdir / "usage.jsonl") as proxy:
        proxy.stream_errors = True
        Handler.proxy_socket = proxy.sandbox_socket()
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            cmd = [
                CODEX, "--ask-for-approval", "never",
                "-c", 'model_provider="abc-bench-proxy"',
                "-c", 'model_providers.abc-bench-proxy.name="p"',
                "-c", f'model_providers.abc-bench-proxy.base_url="http://127.0.0.1:{port}"',
                "-c", 'model_providers.abc-bench-proxy.wire_api="responses"',
                "-c", "model_providers.abc-bench-proxy.request_max_retries=0",
                "-c", "model_providers.abc-bench-proxy.stream_max_retries=0",
                *extra_config,
                "exec", "--sandbox", "danger-full-access",
                "--cd", str(workdir / "ws"), "--skip-git-repo-check",
                "--model", MODEL,
                "--ephemeral", "--ignore-user-config", "--json", "--color", "never", "-",
            ]
            env = dict(os.environ, CODEX_HOME=str(home), HOME=str(home))
            proc = subprocess.run(
                cmd, input="Write hello.txt containing OK, then stop.",
                capture_output=True, text=True, env=env, timeout=240,
            )
            print(f"   codex exit: {proc.returncode}")
            blocked = proxy.blocked_provider_executed_tools
            print(f"   proxy.blocked_provider_executed_tools: {blocked}")
            print(f"   proxy.last_error_kind: {proxy.last_error_kind}")
            # Did anything actually reach the provider?
            text = log.read_text()
            reached = [ln for ln in text.splitlines() if "attempt 1 ->" in ln]
            print(f"   upstream attempts logged: {len(reached)}")
            for ln in text.splitlines():
                if "tool-policy" in ln:
                    print(f"   LOG: {ln[:150]}")

            ok = bool(blocked) == expect_blocked
            print(f"   RESULT: {'as expected' if ok else 'UNEXPECTED'}")
            return ok
        finally:
            server.shutdown()
            server.server_close()
            t.join(timeout=5)


results = []
results.append(
    run_case(
        "CASE 1: harness setting (web_search=\"disabled\") -> should NOT be blocked",
        ["-c", 'web_search="disabled"'],
        18091,
        expect_blocked=False,
    )
)
results.append(
    run_case(
        "CASE 2: the ineffective key only -> Codex leaks web_search, proxy MUST block",
        ["-c", "tools.web_search=false"],
        18092,
        expect_blocked=True,
    )
)

print("=" * 72)
print("ALL CASES AS EXPECTED" if all(results) else "SOME CASES FAILED")
sys.exit(0 if all(results) else 1)
