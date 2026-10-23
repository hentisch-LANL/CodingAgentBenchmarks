"""Preflight: prove the container can actually build and test the codebase.

Why this stage exists
---------------------
The entire value of this tool rests on a precondition that "provide a git repo and
a container" does not capture: **the container must be able to verify a change in
that repository deterministically, ideally via a targeted test.** If it cannot,
the workflow still runs — it just produces garbage. Screening mass-disqualifies
commits, ranking selects from what is left, and construction authors verifiers that
cannot work. You would discover this only after spending real money on a great many
agent runs.

Preflight makes that failure cheap and immediate. It runs, in the real container
against the real repository at HEAD:

1. a **sanity** probe — is the source tree actually visible at the mount point?
2. **configure**, if configured;
3. **build**, timed;
4. **test_one** with a known-matching selector, timed — the single most important
   check, because targeted verification is what makes emitted benchmarks
   affordable to grade;
5. optionally the **full test** suite, timed, only when explicitly asked (it can be
   very slow, and nothing downstream depends on it).

Measured, not asserted
----------------------
The timings are the point as much as the pass/fail is. The original pipeline
hard-coded "a full build takes ~30+ minutes" into its screening prompt, which was
wrong by roughly an order of magnitude; the mistake was discovered late and patched
by adding a paragraph to the *ranking* prompt explaining that screening's cost
scores were systematically too harsh. Both stages were then reasoning from
different numbers, and every screening score already on disk was biased.

Here the numbers come from a real timed run and are written back into the config
with ``--write-config``, after which every stage quotes the same measured figure.
A guessed constant in a prompt is a bug; a measurement is a fact.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, MineConfig, _humanize_seconds
from . import repo as repo_mod
from . import sandbox as sandbox_mod

#: Marker printed by the sanity probe to confirm the mount is populated.
_SANITY_MARKER = "LBM_PREFLIGHT_SANITY_OK"


@dataclass
class Step:
    """One preflight step and its outcome.

    Attributes
    ----------
    name : str
        Step identifier (``sanity``, ``configure``, ``build``, ``test_one``,
        ``test``).
    command : str
        The command run, as rendered.
    status : str
        ``ok``, ``failed``, ``skipped``, or ``dry_run``.
    seconds : float
        Wall-clock duration.
    returncode : int or None
        Exit status, where applicable.
    detail : str
        Explanation for a non-``ok`` status.
    log_file : Path or None
        Captured output.
    """

    name: str
    command: str = ""
    status: str = "skipped"
    seconds: float = 0.0
    returncode: int | None = None
    detail: str = ""
    log_file: Path | None = None

    @property
    def ok(self) -> bool:
        """True if the step passed or was legitimately skipped."""
        return self.status in ("ok", "skipped", "dry_run")


@dataclass
class PreflightResult:
    """The full outcome of a preflight run."""

    steps: list[Step] = field(default_factory=list)
    head_sha: str = ""
    selector: str = ""
    full_build_seconds: float | None = None
    targeted_test_seconds: float | None = None
    full_test_seconds: float | None = None

    @property
    def ok(self) -> bool:
        """True if every step passed."""
        return all(s.ok for s in self.steps)

    def step(self, name: str) -> Step | None:
        """Return the named step, if it ran."""
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the run manifest / ``preflight.json``."""
        return {
            "ok": self.ok,
            "head_sha": self.head_sha,
            "selector": self.selector,
            "full_build_seconds": self.full_build_seconds,
            "targeted_test_seconds": self.targeted_test_seconds,
            "full_test_seconds": self.full_test_seconds,
            "steps": [
                {
                    "name": s.name,
                    "command": s.command,
                    "status": s.status,
                    "seconds": round(s.seconds, 2),
                    "returncode": s.returncode,
                    "detail": s.detail,
                    "log_file": str(s.log_file) if s.log_file else None,
                }
                for s in self.steps
            ],
        }

    def describe(self) -> str:
        """Render a human-readable report."""
        lines = ["Preflight results:", ""]
        width = max((len(s.name) for s in self.steps), default=8)
        for s in self.steps:
            mark = {"ok": "PASS", "failed": "FAIL", "skipped": "skip", "dry_run": "dry"}[
                s.status
            ]
            timing = f"{_humanize_seconds(s.seconds):>12}" if s.seconds else " " * 12
            lines.append(f"  [{mark:>4}] {s.name:<{width}} {timing}")
            if s.detail:
                for chunk in s.detail.splitlines():
                    lines.append(f"         {chunk}")
        lines.append("")

        if self.full_build_seconds is not None:
            lines.append(
                f"Measured full build: {_humanize_seconds(self.full_build_seconds)}"
            )
        if self.targeted_test_seconds is not None:
            lines.append(
                f"Measured targeted test: "
                f"{_humanize_seconds(self.targeted_test_seconds)}"
            )
        if self.full_test_seconds is not None:
            lines.append(
                f"Measured full test suite: {_humanize_seconds(self.full_test_seconds)}"
            )
        return "\n".join(lines)


def check_config(cfg: MineConfig) -> list[str]:
    """Return static problems with the toolchain, without running anything.

    Cheap checks that catch the common mistakes before a container is ever
    started. The ``test_one`` warning is the important one: a toolchain that can
    build but cannot select a single test yields benchmarks whose verifiers must
    run everything, which is precisely the expensive regime the rubric is designed
    to avoid.
    """
    problems: list[str] = []
    tc = cfg.toolchain

    if not tc.build and not tc.test and not tc.test_one:
        problems.append(
            "toolchain defines no commands at all. Agents will have no way to "
            "compile or test the codebase, so no emitted benchmark can be "
            "verified by anything more than inspection. Define at least "
            "toolchain.test_one."
        )
    if not tc.test_one:
        problems.append(
            "toolchain.test_one is not set. Without a way to run a *single* test, "
            "every emitted verifier must fall back to the full suite — expensive "
            "to grade and a poor fit for the rubric's verify_cost axis. Strongly "
            "recommended."
        )
    if tc.test_one and not tc.preflight_selector:
        problems.append(
            "toolchain.preflight_selector is not set, so preflight cannot prove "
            "test_one actually selects a real test. Set it to a selector known to "
            "match at least one test at HEAD."
        )
    return problems


def run(
    cfg: MineConfig,
    *,
    dry_run: bool = False,
    include_full_test: bool = False,
    log_dir: Path | None = None,
) -> PreflightResult:
    """Execute preflight against the repository at HEAD.

    Parameters
    ----------
    cfg : MineConfig
        The run configuration.
    dry_run : bool, optional
        Print the container commands without executing them.
    include_full_test : bool, optional
        Also time the full test suite. Off by default: it can take hours, and no
        downstream stage needs the number.
    log_dir : Path, optional
        Where to write per-step logs. Defaults to ``<run_dir>/preflight/``.

    Notes
    -----
    Runs against a **throwaway worktree at HEAD**, never the user's checkout, so a
    build cannot leave artifacts in, or otherwise disturb, the source of truth for
    every later stage.
    """
    result = PreflightResult()
    tc = cfg.toolchain
    source = cfg.codebase.source_mount

    repo_mod.require_repo(cfg.codebase.repo)
    head = repo_mod.git(cfg.codebase.repo, "rev-parse", "HEAD").strip()
    result.head_sha = head
    result.selector = tc.preflight_selector

    logs = Path(log_dir) if log_dir else (cfg.run_dir / "preflight")
    logs.mkdir(parents=True, exist_ok=True)

    sandbox = sandbox_mod.make_sandbox(cfg, dry_run=dry_run)

    with sandbox_mod.scratch_dir(cfg, "lbm-preflight-") as scratch:
        work = scratch / "work"
        work.mkdir(parents=True, exist_ok=True)

        # The source tree is materialized *inside* the single mounted directory,
        # at the basename of the configured mount point, so that one bind
        # presents it at exactly the path every prompt and command references.
        # (See sandbox.py for why nothing is nested.)
        source_name = Path(source).name
        with repo_mod.worktree(cfg.codebase.repo, head, work / source_name):
            env = tc.rendered_env(source)

            def _run(name: str, command: str) -> Step:
                """Run one timed step in the container."""
                step = Step(name=name, command=command)
                step.log_file = logs / f"{name}.log"
                start = time.monotonic()
                try:
                    rc = sandbox_mod.run_command(
                        cfg,
                        sandbox=sandbox,
                        command=command,
                        work_dir=work,
                        log_file=step.log_file,
                        env=env,
                        check=False,
                    )
                except Exception as exc:  # noqa: BLE001 - report, never crash
                    step.status = "failed"
                    step.detail = f"{type(exc).__name__}: {exc}"
                    step.seconds = time.monotonic() - start
                    return step
                step.seconds = time.monotonic() - start

                if dry_run:
                    step.status = "dry_run"
                    return step

                step.returncode = rc
                if rc == 0:
                    step.status = "ok"
                else:
                    step.status = "failed"
                    step.detail = _tail(step.log_file, rc)
                return step

            # 1. Sanity: is the tree actually where every prompt will claim it is?
            # Checked first and separately, because a mount problem otherwise
            # surfaces as a baffling compiler error many minutes later.
            sanity_cmd = (
                f'test -d "{source}" && ls -A "{source}" | head -1 >/dev/null '
                f'&& echo {_SANITY_MARKER}'
            )
            sanity = _run("sanity", sanity_cmd)
            if sanity.status == "failed":
                sanity.detail = (
                    f"the codebase is not present (or is empty) at {source} inside "
                    f"the container.\n"
                    f"Every prompt and toolchain command refers to that path, so "
                    f"nothing downstream can work.\n"
                    f"Check codebase.source_mount, and that the image does not "
                    f"already occupy that path."
                )
            result.steps.append(sanity)
            if not sanity.ok:
                return result

            # 2. Configure (optional).
            if tc.configure:
                step = _run("configure", tc.render(tc.configure, source))
                result.steps.append(step)
                if not step.ok:
                    return result

            # 3. Build, timed.
            if tc.build:
                step = _run("build", tc.render(tc.build, source))
                result.steps.append(step)
                if step.status == "ok":
                    result.full_build_seconds = step.seconds
                else:
                    return result

            # 4. Targeted test, timed — the decisive capability.
            if tc.test_one:
                if not tc.preflight_selector:
                    result.steps.append(
                        Step(
                            name="test_one",
                            command=tc.test_one,
                            status="skipped",
                            detail=(
                                "no toolchain.preflight_selector configured, so "
                                "targeted testing could not be proven. Emitted "
                                "verifiers depend on this working."
                            ),
                        )
                    )
                else:
                    step = _run(
                        "test_one",
                        tc.render(tc.test_one, source, selector=tc.preflight_selector),
                    )
                    if step.status == "ok":
                        result.targeted_test_seconds = step.seconds
                        if _looks_like_no_tests_ran(step.log_file):
                            step.status = "failed"
                            step.detail = (
                                f"the command succeeded but appears to have run no "
                                f"tests — selector "
                                f"{tc.preflight_selector!r} probably matches "
                                f"nothing.\n"
                                f"A selector that silently matches nothing is worse "
                                f"than one that errors: emitted verifiers would "
                                f"'pass' without testing anything. Pick a selector "
                                f"you know matches a real test at HEAD."
                            )
                            result.targeted_test_seconds = None
                    result.steps.append(step)
                    if not step.ok:
                        return result

            # 5. Full suite, only on request.
            if include_full_test and tc.test:
                step = _run("test", tc.render(tc.test, source))
                result.steps.append(step)
                if step.status == "ok":
                    result.full_test_seconds = step.seconds

    return result


def _tail(log_file: Path | None, returncode: int | None, lines: int = 25) -> str:
    """Return the tail of a log for an error report."""
    head = f"exit status {returncode}."
    if not log_file or not log_file.is_file():
        return head
    try:
        content = log_file.read_text(errors="replace").splitlines()
    except OSError:
        return head
    tail = [ln for ln in content if ln.strip()][-lines:]
    if not tail:
        return f"{head} (no output captured; see {log_file})"
    body = "\n".join(f"  | {ln}" for ln in tail)
    return f"{head} Last output from {log_file}:\n{body}"


#: Phrases indicating a test runner ran successfully but selected nothing.
_NO_TESTS_PATTERNS = (
    r"no tests were found",
    r"no tests to run",
    r"0 tests? (?:were )?(?:found|ran|selected|executed)",
    r"tests? passed,\s*0 tests? failed out of 0",
    r"total tests?:\s*0\b",
    r"ran 0 tests",
    r"collected 0 items",
    r"no tests ran",
)


def _looks_like_no_tests_ran(log_file: Path | None) -> bool:
    """Heuristically detect a zero-test run that still exited successfully.

    Necessarily a heuristic — every test runner phrases this differently — but
    worth having: an exit-0 run that tested nothing is the one failure mode that
    would otherwise propagate all the way into shipped verifiers that pass
    vacuously. False positives are cheap (the user picks a better selector);
    a false negative can poison a whole suite.
    """
    if not log_file or not log_file.is_file():
        return False
    try:
        text = log_file.read_text(errors="replace").lower()
    except OSError:
        return False
    return any(re.search(p, text) for p in _NO_TESTS_PATTERNS)


def write_measurements(cfg: MineConfig, result: PreflightResult) -> Path:
    """Write measured timings back into the run's config file.

    Rewrites only the ``toolchain`` timing keys, textually, preserving comments
    and formatting elsewhere. Editing in place is deliberate: the measurement must
    become the value every stage reads, otherwise it is just a number in a log and
    the prompts keep quoting nothing.

    Raises
    ------
    ConfigError
        If the config file location is unknown or unwritable.
    """
    if cfg.source_path is None or not cfg.source_path.is_file():
        raise ConfigError(
            "cannot write measurements: the originating config file is unknown"
        )

    path = cfg.source_path
    text = path.read_text()
    updates = {
        "full_build_seconds": result.full_build_seconds,
        "targeted_test_seconds": result.targeted_test_seconds,
    }

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)

    for key, value in updates.items():
        if value is None:
            continue
        rendered = f"{value:.1f}"
        pattern = re.compile(rf"^(\s*){key}\s*:.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(rf"\g<1>{key}: {rendered}", text)
        else:
            # Insert under the existing toolchain block, preserving its indent.
            tc_re = re.compile(r"^toolchain\s*:\s*$", re.MULTILINE)
            match = tc_re.search(text)
            if match:
                insert_at = match.end()
                text = (
                    text[:insert_at] + f"\n  {key}: {rendered}" + text[insert_at:]
                )
            else:
                text = text.rstrip() + f"\ntoolchain:\n  {key}: {rendered}\n"

    path.write_text(text)
    return backup


def save_report(cfg: MineConfig, result: PreflightResult) -> Path:
    """Write ``preflight.json`` into the run directory and return its path."""
    dest = cfg.run_dir / "preflight.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n")
    return dest


def load_report(cfg: MineConfig) -> dict[str, Any] | None:
    """Load a previous ``preflight.json``, or ``None`` if absent/unreadable."""
    path = cfg.run_dir / "preflight.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
