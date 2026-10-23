#!/usr/bin/env python3
"""Exercise a harness's run_benchmark.sh without Charliecloud.

Some sandboxes have a seccomp filter that blocks `unshare`, so `ch-run` cannot
create the user+mount namespaces that `abc-bench run` needs. This rig runs the
harness script directly on the host while keeping the parts of the contract the
harness actually depends on:

  * a real automaton ModelProxy (host broker + a loopback HTTP server serving
    the same handler the sandbox helper uses), so model traffic goes through the
    same proxy code path, model-name rewriting, and usage accounting;
  * a real models_config.yaml produced by Sandbox.harness_models_config();
  * the same nine CLI flags automaton passes to run_benchmark.sh;
  * the same ABC_BENCH_MODEL_PROXY_API_KEY env var contract.

What it does NOT test: ch-run itself, the network namespace, bind mounts, the
`--unset-env=*` clean environment, and the container image build. In particular
the agent CLI must already be installed on the host, since the container image
is not used.

The harness is whatever the chosen agent declares, so this works for any
harness; `--agent` selects it.

Usage:
  python3 tools_dev/harness_smoke.py --agent codex-default --task hello-world-python
  python3 tools_dev/harness_smoke.py --agent claude-code-default --task hello-world-python
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from automaton import Agent, Harness  # noqa: E402
from automaton.proxy import ModelProxy  # noqa: E402
from automaton.sandbox import Sandbox  # noqa: E402
from automaton.usage import aggregate_usage  # noqa: E402

from abc_bench.config import Config  # noqa: E402
from abc_bench.task import Task  # noqa: E402

# Reuse automaton's real sandbox-side proxy handler.

from automaton.sandbox_helper import SCRIPT as _HELPER_SCRIPT  # noqa: E402

_helper_ns: dict = {"__name__": "sandbox_helper_inline"}
exec(compile(_HELPER_SCRIPT, "sandbox_helper_inline", "exec"), _helper_ns)
_BaseHandler = _helper_ns["Handler"]
_ProxyFailure = _helper_ns["ProxyFailure"]


class Handler(_BaseHandler):  # type: ignore[misc, valid-type]
    """Serve the proxy without killing this process on transport failure.

    In production the helper runs as its own process, so its `os._exit(86)` on a
    dead proxy socket only ends the helper. Here the handler runs in-process, so
    that call would kill the rig itself before it could report or verify. This
    happens routinely on the timeout path: the harness kills the agent CLI
    mid-request, leaving an in-flight connection whose socket then reads EOF.
    """

    def handle_one_request(self):  # noqa: D102
        try:
            return super(_BaseHandler, self).handle_one_request()
        except _ProxyFailure as exc:
            print(f"[model-proxy] {exc}")



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="hello-world-python")
    ap.add_argument("--task-group", default="smoke_test")
    ap.add_argument("--agent", default="codex-default")
    ap.add_argument(
        "--out",
        default=None,
        help="run directory (default: /tmp/abc-bench-harness-smoke/<agent>)",
    )
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--port", type=int, default=18080)
    args = ap.parse_args()

    config = Config()
    resolver = config.group_resolver()
    task = Task(args.task, resolver, task_group=args.task_group)
    agent = Agent(args.agent, config)
    harness = Harness(agent.harness_id, REPO / "harnesses" / agent.harness_id)

    errors = harness.validate()
    if errors:
        print("harness.validate() errors:", errors)
        return 1

    run_dir = Path(args.out or f"/tmp/abc-bench-harness-smoke/{args.agent}")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    workspace = run_dir / "workspace"
    shutil.copytree(task.workspace_dir, workspace)

    harness_dir = run_dir / "harness"
    shutil.copytree(harness.harness_dir, harness_dir)

    harness_output = run_dir / "harness_output"
    harness_output.mkdir()

    shutil.copyfile(task.prompt_file, run_dir / "prompt.md")

    group_prompt = None
    if task.group_prompt_file and Path(task.group_prompt_file).exists():
        group_prompt = run_dir / "group_prompt.md"
        shutil.copyfile(task.group_prompt_file, group_prompt)

    sandbox = Sandbox(
        host_root=run_dir,
        proxy_api_key_env="ABC_BENCH_MODEL_PROXY_API_KEY",
        proxy_api_key_value="abc-bench-proxy",
        proxy_provider_id="abc-bench-proxy",
        sandbox_proxy_port=args.port,
    )

    models_config_file = run_dir / "models_config.yaml"
    harness_config_file = run_dir / "harness_config.yaml"

    import yaml

    with open(models_config_file, "w") as f:
        yaml.dump(sandbox.harness_models_config(agent, sandboxed=True), f)
    with open(harness_config_file, "w") as f:
        yaml.dump(agent.harness_config, f)

    print("--- models_config.yaml ---")
    print(models_config_file.read_text())
    print("--- harness_config.yaml ---")
    print(harness_config_file.read_text())

    proxy_log = harness_output / "model_proxy.log"
    usage_file = harness_output / "usage.jsonl"

    cmd = [
        "bash",
        str(harness_dir / "run_benchmark.sh"),
        "--workspace", str(workspace),
        "--task-prompt", str(run_dir / "prompt.md"),
        "--system-prompt", str(harness_dir / "system_prompt.md"),
        "--models-config", str(models_config_file),
        "--harness-config", str(harness_config_file),
        "--output-dir", str(harness_output),
        "--task-env", str(workspace / "abc-bench-task-env"),
    ]
    if group_prompt is not None:
        cmd += ["--group-prompt", str(group_prompt)]
    cmd += ["--timeout-seconds", str(args.timeout)]

    env = dict(os.environ)
    env["ABC_BENCH_MODEL_PROXY_API_KEY"] = "abc-bench-proxy"

    returncode = None
    with ModelProxy(
        sandbox.model_registry(agent), proxy_log, usage_records_file=usage_file
    ) as proxy:
        proxy.stream_errors = True
        Handler.proxy_socket = proxy.sandbox_socket()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print(f"--- running harness (proxy on 127.0.0.1:{args.port}) ---")
            result = subprocess.run(cmd, env=env)
            returncode = result.returncode
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        usage_records = list(proxy.usage_records)
        summary = aggregate_usage(usage_records)

    print("=" * 70)
    print("harness returncode:", returncode)
    print("usage summary:", json.dumps(summary, indent=2, default=str)[:1500])

    # Run the hidden verifier the same way the runner does (plain subprocess
    # here, since ch-run is unavailable).
    verification_src = task.task_dir / "verification"
    verdict = "NOT-RUN"
    if verification_src.exists():
        verification = workspace / "verification"
        if verification.exists():
            shutil.rmtree(verification)
        shutil.copytree(verification_src, verification)
        verify_log = run_dir / "verify.log"
        with open(verify_log, "w") as log:
            vres = subprocess.run(
                ["bash", str(verification / "verify_solution.sh")],
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        text = verify_log.read_text()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() in ("PASS", "FAIL")]
        verdict = (lines[-1] if lines else "NO-VERDICT") if vres.returncode == 0 else "FAIL"
        print("verify returncode:", vres.returncode)
        print("verify verdict:", verdict)
        print("--- verify.log (tail) ---")
        print("\n".join(text.splitlines()[-25:]))

    print("=" * 70)
    print("run dir:", run_dir)
    return 0 if (returncode == 0 and verdict == "PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
