"""SCREEN — score every selected commit against the rubric, in parallel.

One fresh agent per commit. Each reads the commit's diff, message, and surrounding
source, then emits per-axis scores plus prose. The **total score and the
disqualification decision are computed here in Python**, never by the model, so
scoring stays deterministic and a run can be re-weighted afterwards without
re-invoking a single agent.

Durable artifact and resume
---------------------------
The stage's source of truth is one file per commit:
``<run>/screen/summaries/<short-sha>.json``. Resume is simply "skip commits that
already have one". That choice has two properties worth naming:

- It is **backend-independent** — a run started serially can be resumed under MPI,
  because progress lives in files a human can read rather than in a pool's internal
  bookkeeping.
- It is **all-or-nothing per commit**. A commit either has a complete summary or has
  nothing. There is no partial state to reason about, and no way for a
  half-finished judgement to leak into ranking.

Failures are recorded separately, in an append-only ``attempts.jsonl``. This closes
a real gap in the predecessor pipeline, where a failed screen wrote nothing at all:
"never attempted" and "failed six times" were indistinguishable, retries were
unbounded across resumes, and the transcripts had already been deleted. Here
failures are counted (so ``max_attempts`` can be enforced) and, under the default
retention policy, their transcripts are kept.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import parallel, prompts as prompts_mod, repo as repo_mod, sandbox as sandbox_mod
from ..config import MineConfig
from ..manifest import ATTEMPTS_NAME, exhausted_items, record_attempt
from ..prompts import PromptPack
from ..rubric import CONFIDENCE_LABELS, DIFFICULTY_LABELS, score as score_axes

#: Cap on the diff text handed to an agent. Large diffs are truncated rather than
#: dropped: a commit with a 2 MB diff is usually a poor benchmark candidate
#: anyway, and the agent is told explicitly that its view was clipped so it can
#: temper its confidence instead of scoring a change it only partly saw.
MAX_DIFF_BYTES = 200 * 1024

#: Filename the agent writes its verdict to, inside its output directory.
VERDICT_FILENAME = "summary.json"


def summaries_dir(cfg: MineConfig) -> Path:
    """Return (and create) the per-run summaries directory."""
    path = cfg.summaries_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def summary_path(cfg: MineConfig, sha: str) -> Path:
    """Return the durable summary path for ``sha``."""
    return summaries_dir(cfg) / f"{sha[:repo_mod.SHORT_SHA_LEN]}.json"


def attempts_path(cfg: MineConfig) -> Path:
    """Return the screen stage's attempt log path."""
    return cfg.screen_dir / ATTEMPTS_NAME


def completed(cfg: MineConfig) -> set[str]:
    """Return short SHAs that already have a durable summary."""
    return {p.stem for p in summaries_dir(cfg).glob("*.json")}


def load_summaries(cfg: MineConfig) -> list[dict[str, Any]]:
    """Load every summary written for this run, sorted by score descending.

    Unreadable files are skipped rather than fatal: one corrupt summary (a job
    killed mid-write, say) should not block ranking over hundreds of good ones.
    """
    out: list[dict[str, Any]] = []
    for path in sorted(summaries_dir(cfg).glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda s: s.get("benchmark_score") or 0, reverse=True)
    return out


@dataclass
class ScreenPlan:
    """What a screen invocation intends to do.

    Separating the plan from execution lets the CLI show a meaningful ``--plan``
    without starting any container, and lets every MPI rank compute an identical
    work list from read-only state.
    """

    selected: list[repo_mod.Commit]
    pending: list[repo_mod.Commit]
    already_done: int
    exhausted: dict[str, int]
    report: repo_mod.SelectionReport

    def describe(self, max_attempts: int) -> str:
        """Render a human-readable plan."""
        lines = [self.report.describe()]
        if self.already_done:
            lines.append(f"  {self.already_done} already screened (skipping)")
        if self.exhausted:
            lines.append(
                f"  {len(self.exhausted)} skipped: failed {max_attempts} time(s) "
                f"already (raise execution.max_attempts to retry)"
            )
        lines.append(f"  {len(self.pending)} to screen now")
        return "\n".join(lines)


def plan(cfg: MineConfig, *, only: Sequence[str] = ()) -> ScreenPlan:
    """Compute the work list. Read-only, and identical on every rank.

    Parameters
    ----------
    only : sequence of str, optional
        Restrict to these SHAs (prefix-matched), for targeted debugging.
    """
    commits = repo_mod.enumerate_commits(cfg.codebase.repo, paths=cfg.selection.paths)
    commits = repo_mod.annotate(cfg.codebase.repo, commits)

    report = repo_mod.SelectionReport()
    selected = repo_mod.select(commits, cfg.selection, report=report)

    if only:
        wanted = tuple(only)
        selected = [c for c in selected if any(c.sha.startswith(w) for w in wanted)]
        report.selected = len(selected)

    done = completed(cfg)
    spent = exhausted_items(attempts_path(cfg), cfg.execution.max_attempts)

    pending = [
        c for c in selected if c.short not in done and c.short not in spent
    ]
    return ScreenPlan(
        selected=selected,
        pending=pending,
        already_done=sum(1 for c in selected if c.short in done),
        exhausted={c.short: spent[c.short] for c in selected if c.short in spent},
        report=report,
    )


def _compose_prompt(
    cfg: MineConfig,
    pack: PromptPack,
    commit: repo_mod.Commit,
    *,
    truncated: bool,
    touched: Sequence[str],
) -> str:
    """Render the screening prompt for one commit."""
    work = sandbox_mod.CONTAINER_WORK_ROOT
    touched_block = (
        "\n".join(f"  - `{p}`" for p in touched) if touched else "  _(none reported)_"
    )
    truncation_note = (
        " — **note: this diff was truncated for size; you are not seeing all of it, "
        "so temper your confidence accordingly**"
        if truncated
        else ""
    )
    return prompts_mod.compose(
        "screen_system",
        "screen_task",
        pack=pack,
        variables={
            "codebase_name": cfg.codebase.name,
            "rubric_axis_table": cfg.rubric.render_axis_table(),
            "rubric_axis_guidance": cfg.rubric.render_axis_guidance(),
            "rubric_axes_schema": cfg.rubric.render_axes_schema(),
            "task_types": ", ".join(f"`{t}`" for t in cfg.rubric.task_types),
            "toolchain_block": cfg.toolchain.render_prompt_block(
                cfg.codebase.source_mount
            ),
            "sha": commit.sha,
            "parent": commit.parent,
            "subject": commit.subject or "(no subject)",
            "author_date": commit.author_date or "unknown",
            "files_changed": commit.files_changed,
            "diff_lines": commit.diff_lines,
            "touched_files": touched_block,
            "diff_truncation_note": truncation_note,
            "diff_path": f"{work}/commit.diff",
            "message_path": f"{work}/commit_message.txt",
            "source_path": cfg.codebase.source_mount,
            "summary_path": f"{work}/{sandbox_mod.OUT_DIRNAME}/{VERDICT_FILENAME}",
        },
    )


def _finalize(
    cfg: MineConfig, commit: repo_mod.Commit, raw: dict[str, Any], run: sandbox_mod.AgentRun
) -> dict[str, Any]:
    """Validate the agent's verdict and attach the deterministic score.

    Coercion is intentionally forgiving about *shape* (accepting a bare int where a
    ``{"score": n}`` object was asked for) and strict about *meaning*: an
    unparseable axis becomes 0, which on a gate axis disqualifies. A screening
    agent that did not answer the gate questions has not produced a usable verdict,
    and treating that silence as a pass would let unusable commits through to a
    stage that trusts them.
    """
    axes = cfg.rubric.coerce_axes(raw.get("axes"))
    total, disqualified = score_axes(axes, rubric=cfg.rubric)

    task_type = raw.get("task_type") or []
    if isinstance(task_type, str):
        task_type = [task_type]
    task_type = [str(t) for t in task_type if str(t)]

    difficulty = str(raw.get("est_difficulty", "")).strip().lower()
    if difficulty not in DIFFICULTY_LABELS:
        difficulty = "medium"

    confidence = str(raw.get("confidence", "")).strip().lower()
    if confidence not in CONFIDENCE_LABELS:
        confidence = "low"

    build_attempted = bool(raw.get("build_attempted", False))
    build_outcome = raw.get("build_outcome")
    if build_outcome is not None and not isinstance(build_outcome, str):
        build_outcome = str(build_outcome)
    if not build_attempted:
        build_outcome = None

    return {
        "sha": commit.sha,
        "parent": commit.parent,
        "subject": commit.subject,
        "author_date": commit.author_date,
        "files_changed": commit.files_changed,
        "diff_lines": commit.diff_lines,
        "benchmark_score": total,
        "disqualified": disqualified,
        "axes": axes,
        "task_type": task_type,
        "est_difficulty": difficulty,
        "one_line": str(raw.get("one_line", "")),
        "proposed_task": str(raw.get("proposed_task", "")),
        "proposed_verification": str(raw.get("proposed_verification", "")),
        "build_attempted": build_attempted,
        "build_outcome": build_outcome,
        "rationale": str(raw.get("rationale", "")),
        "confidence": confidence,
        "screened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "usage": sandbox_mod.read_usage(run.run_dir),
    }


def make_worker(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool = False,
) -> Callable[[repo_mod.Commit], parallel.ItemResult]:
    """Build the per-commit worker callable.

    Returned as a closure so the same function serves all three parallel backends
    and can be exercised directly in a test without any pool.
    """
    automaton_cfg = sandbox_mod.build_automaton_config(model)
    agent = sandbox_mod.make_agent(automaton_cfg)
    harness = sandbox_mod.make_harness()
    log_root = cfg.screen_dir / "logs"

    def worker(commit: repo_mod.Commit) -> parallel.ItemResult:
        """Screen one commit end to end."""
        attempts = attempts_path(cfg)
        sandbox = sandbox_mod.make_sandbox(cfg, dry_run=dry_run)

        with sandbox_mod.scratch_dir(cfg, f"lbm-screen-{commit.short}-") as scratch:
            work = scratch / "work"
            run_dir = scratch / "run"
            work.mkdir(parents=True, exist_ok=True)

            # Materialize the source tree inside the single mounted directory, at
            # the basename of the configured mount point, so one bind presents it
            # exactly where the prompt says it is.
            source_name = Path(cfg.codebase.source_mount).name
            with repo_mod.worktree(cfg.codebase.repo, commit.sha, work / source_name):
                diff, truncated = repo_mod.commit_diff(
                    cfg.codebase.repo, commit.sha, max_bytes=MAX_DIFF_BYTES
                )
                touched = repo_mod.touched_files(cfg.codebase.repo, commit.sha)

                (work / "commit.diff").write_text(diff)
                (work / "commit_message.txt").write_text(
                    repo_mod.commit_message(cfg.codebase.repo, commit.sha) + "\n"
                )
                (work / "touched_files.txt").write_text("\n".join(touched) + "\n")

                prompt = _compose_prompt(
                    cfg, pack, commit, truncated=truncated, touched=touched
                )

                run = sandbox_mod.run_agent(
                    cfg,
                    sandbox=sandbox,
                    harness=harness,
                    agent=agent,
                    work_dir=work,
                    run_dir=run_dir,
                    prompt_text=prompt,
                )

            if run.dry_run:
                return parallel.ItemResult(item_id=commit.short, status="dry_run")

            verdict_file = run.out_path(VERDICT_FILENAME)
            failure: str | None = None

            if not run.ok:
                failure = run.detail or "agent execution failed"
            elif not verdict_file.is_file():
                failure = (
                    f"agent finished successfully but wrote no verdict to "
                    f"{VERDICT_FILENAME}"
                )
            else:
                try:
                    raw = json.loads(verdict_file.read_text())
                except json.JSONDecodeError as exc:
                    failure = f"verdict is not valid JSON: {exc}"
                else:
                    if not isinstance(raw, dict):
                        failure = (
                            f"verdict must be a JSON object, got "
                            f"{type(raw).__name__}"
                        )

            if failure is not None:
                sandbox_mod.preserve_logs(
                    run.run_dir,
                    log_root / commit.short,
                    keep_logs=cfg.execution.keep_logs,
                    failed=True,
                )
                record_attempt(
                    attempts,
                    commit.short,
                    "failed",
                    detail=failure,
                    model_id=model.get("model", ""),
                    sha=commit.sha,
                )
                return parallel.ItemResult(
                    item_id=commit.short, status="failed", detail=failure
                )

            summary = _finalize(cfg, commit, raw, run)

            # Write the durable artifact atomically. Resume keys on this file's
            # existence, so a torn write would be indistinguishable from a
            # completed screen — hence temp-then-rename rather than a direct write.
            dest = summary_path(cfg, commit.sha)
            tmp = dest.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
            tmp.replace(dest)

            sandbox_mod.preserve_logs(
                run.run_dir,
                log_root / commit.short,
                keep_logs=cfg.execution.keep_logs,
                failed=False,
            )
            record_attempt(
                attempts,
                commit.short,
                "ok",
                model_id=model.get("model", ""),
                sha=commit.sha,
                benchmark_score=summary["benchmark_score"],
                disqualified=summary["disqualified"],
            )

            return parallel.ItemResult(
                item_id=commit.short,
                status="ok",
                data={
                    "sha": commit.sha,
                    "benchmark_score": summary["benchmark_score"],
                    "disqualified": summary["disqualified"],
                },
            )

    return worker


def run(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool = False,
    only: Sequence[str] = (),
    progress: bool = True,
) -> parallel.BatchOutcome:
    """Execute the screen stage.

    Returns the batch outcome; on MPI worker ranks this is empty and
    ``is_primary`` is False, so callers must not report from it.
    """
    work_plan = plan(cfg, only=only)

    if progress:
        print(work_plan.describe(cfg.execution.max_attempts))
        print()

    if not work_plan.pending:
        return parallel.BatchOutcome(results=[], is_primary=True)

    worker = make_worker(cfg, pack, model, dry_run=dry_run)

    def on_result(result: parallel.ItemResult, index: int, total: int) -> None:
        """Report one completed item."""
        if not progress:
            return
        if result.status == "ok":
            data = result.data
            flag = " DISQUALIFIED" if data.get("disqualified") else ""
            tail = f"score {data.get('benchmark_score', 0):>3}{flag}"
        elif result.status == "dry_run":
            tail = "dry run"
        else:
            tail = f"FAILED: {result.detail.splitlines()[0][:70] if result.detail else ''}"
        print(f"  [{index:>4}/{total}] {result.item_id}  {tail}", flush=True)

    return parallel.run_batch(
        work_plan.pending,
        worker,
        backend=cfg.execution.parallel,
        workers=cfg.execution.workers,
        scratch_dir=cfg.execution.scratch_dir,
        run_id=cfg.run_id,
        on_result=on_result,
    )


def summarize(cfg: MineConfig) -> dict[str, Any]:
    """Return aggregate statistics over this run's summaries.

    Reads from disk rather than from a batch outcome so it reflects the whole run,
    including items screened in earlier allocations.
    """
    summaries = load_summaries(cfg)
    if not summaries:
        return {"screened": 0, "viable": 0, "disqualified": 0}

    viable = [s for s in summaries if not s.get("disqualified")]
    scores = [s.get("benchmark_score", 0) for s in viable]
    by_type: dict[str, int] = {}
    for s in viable:
        for tag in s.get("task_type", []) or ["(untagged)"]:
            by_type[tag] = by_type.get(tag, 0) + 1

    usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    for s in summaries:
        for key, value in (s.get("usage") or {}).items():
            if key in usage and isinstance(value, (int, float)):
                usage[key] += int(value)

    return {
        "screened": len(summaries),
        "viable": len(viable),
        "disqualified": len(summaries) - len(viable),
        "score_max": max(scores) if scores else 0,
        "score_mean": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "by_task_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "usage": usage,
    }
