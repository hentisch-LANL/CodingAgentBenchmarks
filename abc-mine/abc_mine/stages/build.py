"""BUILD — turn each shortlisted commit into a complete ABC Bench task.

One construction agent per shortlisted commit, in parallel. Each agent authors
**three** artifacts:

- ``prompt.md`` — the task statement, the solver's only instruction,
- ``build_env.sh`` — environment setup run before the solver,
- ``verification/verify_solution.sh`` — the hidden deterministic check.

The fourth component, ``workspace/`` (the starting state), is **not** authored by
any agent. The pipeline builds it deterministically by git bundle-cutoff at the
parent commit (see :func:`abc_mine.repo.build_cutoff_workspace`). An agent
asked to "remove the change" would introduce exactly the kind of subtle,
unauditable variation a benchmark cannot tolerate — and could accidentally leave
the answer in the tree.

Two independent polarity layers
-------------------------------
A verifier that does not actually discriminate between the fixed and unfixed trees
is worthless, and worse than worthless if it passes vacuously: it would certify
solvers that did nothing. So polarity is enforced twice, by different parties:

**Layer 1 (the agent, in its sandbox).** The construction agent is required to
check its own verifier against the post-change and pre-change trees before
finishing, and to iterate until it discriminates.

**Layer 2 (this stage, deterministically).** After the agent finishes, the pipeline
re-checks polarity *on the shipped artifact*, in an environment that mirrors the
real grader: the code tree bind-mounted at the grading path, the verifier staged
outside it, and **none** of the conveniences that existed during authoring. A task
reaches ``ok`` only when the verifier FAILs on the shipped starting state and PASSes
on a clean checkout of the change commit.

Layer 2 is not redundant. It catches the most common way a plausible-looking
verifier is actually broken: depending on a path that existed while the agent was
working but does not exist at grading time. Layer 1 cannot catch that by
construction, because Layer 1 *runs* in the environment that has those paths.

Build order
-----------
Commits are built in rank order. When the rank stage produced a **full ranking**
(``rank.output: full``) there is no shortlist to exhaust: the stage simply works
down the order, skipping commits already built or out of attempts, until either
the list runs out or the per-invocation limit (``--limit N``) is reached. That
limit is a property of one job, not of the run — resubmitting continues from
where the previous job stopped. Under ``parallel`` backends the limit is a count
of commits *started*, taken from the top of the remaining order.

Checkpoint and resume
---------------------
The durable record is an append-only ``manifest.jsonl``, written the instant each
task finishes. On resume, tasks whose latest entry is ``ok`` are skipped; any
planned-but-incomplete task directory (from a killed job) is removed and rebuilt.
Nothing partial is ever left for a later stage to trust.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from .. import parallel, prompts as prompts_mod, repo as repo_mod, sandbox as sandbox_mod
from ..config import MineConfig, slugify
from ..manifest import ATTEMPTS_NAME, exhausted_items, record_attempt
from ..prompts import PromptPack
from . import rank as rank_stage, screen as screen_stage

#: Append-only build record.
MANIFEST_NAME = "manifest.jsonl"

#: Agent deliverable filenames, inside its output directory.
OUT_PROMPT = "prompt.md"
OUT_BUILD_ENV = "build_env.sh"
OUT_VERIFY = "verification/verify_solution.sh"

#: In-container path at which the code tree is mounted during a Layer-2 check.
#: Must match what abc-bench does at grading time, since the whole purpose of the
#: re-check is to reproduce that environment faithfully.
GRADING_WORKSPACE = Path("/workspace")

#: In-container directory holding the verifier during a Layer-2 check. Deliberately
#: **outside** the code tree: at grading time the verifier lives in the task's
#: hidden ``verification/`` directory, never inside the solver's workspace, so a
#: verifier that assumes otherwise must fail here rather than in production.
GRADING_VERIFY_ROOT = Path("/lbm-verify")


def manifest_path(cfg: MineConfig) -> Path:
    """Return the build stage's append-only manifest path."""
    return cfg.build_dir / MANIFEST_NAME


def attempts_path(cfg: MineConfig) -> Path:
    """Return the build stage's attempt log path."""
    return cfg.build_dir / ATTEMPTS_NAME


def group_dir(cfg: MineConfig) -> Path:
    """Return the emitted task-group directory."""
    return cfg.tasks_dir / cfg.group_id


def task_id(one_line: str, sha: str) -> str:
    """Build a stable, filesystem-safe task id from a description and SHA.

    The SHA suffix guarantees uniqueness even when two commits carry identical
    descriptions, which is common in histories with templated commit subjects.
    """
    slug = slugify(one_line or "task", max_length=56)
    return f"{slug}-{sha[:8]}" if slug else f"task-{sha[:8]}"


def read_manifest(path: Path) -> list[dict[str, Any]]:
    """Read every manifest entry, skipping malformed lines."""
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def append_manifest(path: Path, entry: dict[str, Any]) -> None:
    """Append one entry atomically.

    ``O_APPEND`` makes a single small write atomic on POSIX, so parallel workers on
    a shared filesystem never interleave partial lines — which is what allows the
    manifest to be the source of truth without a lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def completed_tasks(path: Path) -> set[str]:
    """Return task ids whose **latest** manifest entry is ``ok``.

    Latest-entry-wins: a task that failed and was later rebuilt successfully counts
    as done, and one that was ``ok`` but failed on a later forced rebuild does not.
    """
    latest: dict[str, str] = {}
    for entry in read_manifest(path):
        tid = entry.get("task_id")
        if tid:
            latest[str(tid)] = str(entry.get("status", ""))
    return {tid for tid, status in latest.items() if status == "ok"}


def decide_verifier_result(output: str, returncode: int | None) -> str:
    """Decide ``PASS``/``FAIL`` from verifier output, matching abc-bench exactly.

    The **last** line whose stripped content is exactly ``PASS`` or ``FAIL`` wins,
    and ``PASS`` additionally requires a zero exit status. Both halves matter: a
    script that prints ``PASS`` then dies in cleanup has not passed, and a bare
    successful exit with no status line is not a pass either.

    This mirrors abc-bench's own logic deliberately. If the two disagreed, a task
    could satisfy this stage's polarity check and then be graded differently in
    production — the exact failure this check exists to prevent.
    """
    statuses = [
        line.strip() for line in output.splitlines() if line.strip() in {"PASS", "FAIL"}
    ]
    final = statuses[-1] if statuses else "FAIL"
    if final == "PASS" and (returncode is None or returncode == 0):
        return "PASS"
    return "FAIL"


def validate_task_dir(task_dir: Path) -> list[str]:
    """Return the missing components of an emitted task directory."""
    missing: list[str] = []
    for name in (OUT_PROMPT, OUT_BUILD_ENV, "workspace", OUT_VERIFY):
        if not (task_dir / name).exists():
            missing.append(name)
    return missing


def run_verifier_against_tree(
    cfg: MineConfig,
    *,
    sandbox: Any,
    verifier: Path,
    tree: Path,
    log_file: Path,
    scratch: Path,
) -> str:
    """Run ``verifier`` against ``tree`` the way the real grader will.

    Reproduces the grading environment faithfully, which is the entire point:

    - the code tree is bind-mounted at :data:`GRADING_WORKSPACE` and **is**
      ``$ABC_BENCH_WORKSPACE``;
    - the verifier is staged **outside** that mount, as the grader does;
    - nothing that existed for the agent's convenience during authoring is
      present.

    A verifier that quietly depends on an authoring-time path therefore fails
    *here*, where it is cheap to discover, rather than in production.

    The tree is hard-linked rather than copied where the filesystem permits: source
    trees are large, this runs twice per task, and a verifier that mutates its input
    still must not disturb the original.
    """
    stage = scratch / "verify"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    tree_copy = stage / "tree"
    try:
        shutil.copytree(tree, tree_copy, copy_function=os.link)
    except (OSError, shutil.Error):
        if tree_copy.exists():
            shutil.rmtree(tree_copy)
        shutil.copytree(tree, tree_copy)

    verify_dir = stage / "verification"
    verify_dir.mkdir()
    staged_verifier = verify_dir / "verify_solution.sh"
    shutil.copy2(verifier, staged_verifier)
    staged_verifier.chmod(0o755)

    from automaton.sandbox import BindMount

    log_file.parent.mkdir(parents=True, exist_ok=True)
    verifier_in = GRADING_VERIFY_ROOT / "verify_solution.sh"
    try:
        returncode = sandbox.run_command(
            cfg.container.image,
            ["bash", str(verifier_in)],
            cwd=GRADING_WORKSPACE,
            env_vars={
                "ABC_BENCH_WORKSPACE": str(GRADING_WORKSPACE),
                "ABC_BENCH_TASK_ENV": str(GRADING_WORKSPACE / "abc-bench-task-env"),
            },
            bind_mounts=[
                BindMount(tree_copy, GRADING_WORKSPACE),
                BindMount(verify_dir, GRADING_VERIFY_ROOT),
            ],
            log_file=log_file,
            network=False,
            check=False,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    output = log_file.read_text(errors="replace") if log_file.is_file() else ""
    return decide_verifier_result(output, returncode)


def write_group_yaml(cfg: MineConfig) -> Path:
    """Write the abc-bench ``group.yaml`` for the emitted task group."""
    dest = group_dir(cfg)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "group.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": cfg.group_id,
                "name": cfg.codebase.name,
                "description": (
                    cfg.codebase.group_description
                    or f"Benchmarks mined from the {cfg.codebase.name} git history."
                ),
                "container_template": cfg.group_id,
            },
            sort_keys=False,
            default_flow_style=False,
        )
    )
    return path


@dataclass
class BuildPlan:
    """What a build invocation intends to do."""

    selected: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    already_done: int = 0
    stale_removed: list[str] = field(default_factory=list)
    #: True when building down a full ranking rather than a shortlist.
    full_ranking: bool = False
    #: Commits skipped because they have used up ``execution.max_attempts``.
    exhausted: int = 0
    #: Per-invocation cap on commits started (``None`` = no cap).
    limit: int | None = None
    #: Commits eligible to build but beyond ``limit`` (left for a later job).
    deferred: int = 0

    def describe(self) -> str:
        """Render a human-readable plan."""
        head = "Ranked commits:" if self.full_ranking else "Shortlisted commits:"
        lines = [f"{head:<22} {len(self.selected)}"]
        if self.already_done:
            lines.append(f"  already built:       {self.already_done} (skipping)")
        if self.exhausted:
            lines.append(
                f"  out of attempts:     {self.exhausted} (skipping; raise "
                "execution.max_attempts to retry)"
            )
        if self.stale_removed:
            lines.append(
                f"  incomplete removed:  {len(self.stale_removed)} "
                f"({', '.join(sorted(self.stale_removed)[:3])}"
                f"{', ...' if len(self.stale_removed) > 3 else ''})"
            )
        if self.pending and self.full_ranking:
            ranks = [e.get("rank") for e in self.pending]
            span = f" (ranks {ranks[0]}–{ranks[-1]})" if ranks[0] is not None else ""
        else:
            span = ""
        lines.append(f"  to build now:        {len(self.pending)}{span}")
        if self.deferred:
            lines.append(
                f"  left for later jobs: {self.deferred} (--limit {self.limit}; "
                "resubmit to continue down the order)"
            )
        return "\n".join(lines)


def plan(
    cfg: MineConfig,
    *,
    only: Sequence[str] = (),
    force: bool = False,
    sweep: bool = False,
    limit: int | None = None,
) -> BuildPlan:
    """Compute the work list, in rank order.

    Read-only unless ``sweep`` is set, so every MPI rank derives an identical plan.
    Only rank 0 should pass ``sweep=True``, since removing directories concurrently
    with workers reading them would be a race.

    ``limit`` caps how many commits this invocation starts, taken from the top of
    the order after skipping those already built or out of attempts.
    """
    if limit is not None and limit < 1:
        raise rank_stage.RankError(f"--limit must be >= 1, got {limit}")
    shortlist = rank_stage.load_shortlist(cfg)
    full = bool(shortlist.get("complete"))
    selected = sorted(
        (e for e in shortlist.get("selected", []) if e.get("sha")),
        key=lambda e: e.get("rank") or 0,
    )

    if only:
        selected = [
            e for e in selected if any(str(e["sha"]).startswith(w) for w in only)
        ]

    done = set() if force else completed_tasks(manifest_path(cfg))
    spent = set() if force else set(exhausted_items(attempts_path(cfg), cfg.execution.max_attempts))
    planned = {task_id(e.get("one_line", ""), e["sha"]): e for e in selected}

    eligible = [
        (tid, entry) for tid, entry in planned.items() if tid not in done and tid not in spent
    ]
    chosen = eligible if limit is None else eligible[:limit]
    pending = [entry for _, entry in chosen]

    stale: list[str] = []
    if sweep:
        # Only directories this invocation will rebuild; a commit deferred to a
        # later job keeps whatever is on disk until that job starts it.
        stale = _sweep_incomplete(
            group_dir(cfg), {tid for tid, _ in chosen}, done, force=force
        )

    return BuildPlan(
        selected=selected,
        pending=pending,
        already_done=sum(1 for tid in planned if tid in done),
        stale_removed=stale,
        full_ranking=full,
        exhausted=sum(1 for tid in planned if tid in spent and tid not in done),
        limit=limit,
        deferred=len(eligible) - len(pending),
    )


def _sweep_incomplete(
    group: Path, planned: set[str], done: set[str], *, force: bool
) -> list[str]:
    """Remove planned-but-incomplete task directories before rebuilding.

    Leaves alone anything not planned for this run (it belongs to another
    selection), anything recorded complete, and the group-level files. A partial
    task directory from a killed job is indistinguishable from a complete one by
    inspection alone, so it is removed rather than trusted.
    """
    if not group.is_dir():
        return []
    removed: list[str] = []
    for child in sorted(group.iterdir()):
        if not child.is_dir() or child.name not in planned:
            continue
        if not force and child.name in done:
            continue
        shutil.rmtree(child)
        removed.append(child.name)
    return removed


def _compose_prompt(
    cfg: MineConfig,
    pack: PromptPack,
    entry: dict[str, Any],
    summary: dict[str, Any] | None,
) -> str:
    """Render the construction prompt for one shortlisted commit."""
    work = sandbox_mod.CONTAINER_WORK_ROOT
    out = f"{work}/{sandbox_mod.OUT_DIRNAME}"
    task_type = entry.get("task_type") or (summary or {}).get("task_type") or []
    if isinstance(task_type, str):
        task_type = [task_type]

    def _block(data: Any, absent: str) -> str:
        """Render a JSON block, or a note when the data is missing."""
        if not data:
            return f"_{absent}_"
        return "```json\n" + json.dumps(data, indent=2, sort_keys=True) + "\n```"

    parent = (
        (summary or {}).get("parent") or entry.get("parent") or f"{entry['sha']}^"
    )

    return prompts_mod.compose(
        "build_system",
        "build_task",
        pack=pack,
        variables={
            "codebase_name": cfg.codebase.name,
            "toolchain_block": cfg.toolchain.render_prompt_block(
                cfg.codebase.source_mount
            ),
            "sha": entry["sha"],
            "parent": parent,
            "subject": (
                entry.get("one_line") or (summary or {}).get("one_line") or "(no description)"
            ),
            "rank": entry.get("rank", "?"),
            "est_difficulty": (
                entry.get("est_difficulty") or (summary or {}).get("est_difficulty") or "unknown"
            ),
            "task_type": ", ".join(task_type) if task_type else "unspecified",
            "proposed_task": (
                entry.get("proposed_task")
                or (summary or {}).get("proposed_task")
                or "_(none supplied)_"
            ),
            "proposed_verification": (
                entry.get("proposed_verification")
                or (summary or {}).get("proposed_verification")
                or "_(none supplied)_"
            ),
            "screening_summary": _block(
                summary, "No screening summary was found for this commit."
            ),
            "shortlist_entry": _block(
                entry, "No shortlist entry was found for this commit."
            ),
            "source_path": cfg.codebase.source_mount,
            "reference_diff_path": f"{work}/reference.diff",
            "message_path": f"{work}/commit_message.txt",
            "out_prompt": f"{out}/{OUT_PROMPT}",
            "out_build_env": f"{out}/{OUT_BUILD_ENV}",
            "out_verify": f"{out}/{OUT_VERIFY}",
        },
    )


def make_worker(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool = False,
) -> Callable[[dict[str, Any]], parallel.ItemResult]:
    """Build the per-commit construction worker."""
    automaton_cfg = sandbox_mod.build_automaton_config(model)
    agent = sandbox_mod.make_agent(automaton_cfg)
    harness = sandbox_mod.make_harness()
    summaries = {
        s.get("sha"): s for s in screen_stage.load_summaries(cfg) if s.get("sha")
    }
    manifest = manifest_path(cfg)
    attempts = attempts_path(cfg)
    log_root = cfg.build_dir / "logs"

    def worker(entry: dict[str, Any]) -> parallel.ItemResult:
        """Construct one abc-bench task end to end."""
        sha = entry["sha"]
        tid = task_id(entry.get("one_line", ""), sha)
        summary = summaries.get(sha)
        parent = (summary or {}).get("parent") or entry.get("parent") or f"{sha}^"
        started = time.monotonic()
        sandbox = sandbox_mod.make_sandbox(cfg, dry_run=dry_run)

        def _record(status: str, detail: str = "", **extra: Any) -> parallel.ItemResult:
            """Record the outcome durably, then return it."""
            record = {
                "task_id": tid,
                "sha": sha,
                "parent_sha": parent,
                "status": status,
                "detail": detail,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "model_id": model.get("model", ""),
                "seconds": round(time.monotonic() - started, 1),
                **extra,
            }
            append_manifest(manifest, record)
            record_attempt(
                attempts, tid, status if status in ("ok", "failed") else "skipped",
                detail=detail, model_id=model.get("model", ""), sha=sha,
            )
            return parallel.ItemResult(
                item_id=tid,
                status="ok" if status == "ok" else ("dry_run" if status == "dry_run" else "failed"),
                detail=detail,
                data=record,
            )

        with sandbox_mod.scratch_dir(cfg, f"lbm-build-{sha[:8]}-") as scratch:
            work = scratch / "work"
            run_dir = scratch / "run"
            work.mkdir(parents=True, exist_ok=True)
            source_name = Path(cfg.codebase.source_mount).name

            # A full-history worktree: the agent must be able to move between the
            # change and its parent to validate polarity (Layer 1).
            with repo_mod.worktree(cfg.codebase.repo, sha, work / source_name) as tree:
                diff, _ = repo_mod.commit_diff(cfg.codebase.repo, sha)
                (work / "reference.diff").write_text(diff)
                (work / "commit_message.txt").write_text(
                    repo_mod.commit_message(cfg.codebase.repo, sha) + "\n"
                )

                prompt = _compose_prompt(cfg, pack, entry, summary)
                run_result = sandbox_mod.run_agent(
                    cfg,
                    sandbox=sandbox,
                    harness=harness,
                    agent=agent,
                    work_dir=work,
                    run_dir=run_dir,
                    prompt_text=prompt,
                )

                if run_result.dry_run:
                    return _record("dry_run")

                if not run_result.ok:
                    sandbox_mod.preserve_logs(
                        run_result.run_dir, log_root / tid,
                        keep_logs=cfg.execution.keep_logs, failed=True,
                    )
                    return _record(
                        "failed", run_result.detail or "agent execution failed"
                    )

                # Collect the three deliverables.
                task_dir = group_dir(cfg) / tid
                if task_dir.exists():
                    shutil.rmtree(task_dir)
                (task_dir / "verification").mkdir(parents=True, exist_ok=True)

                authored = {
                    OUT_PROMPT: task_dir / OUT_PROMPT,
                    OUT_BUILD_ENV: task_dir / OUT_BUILD_ENV,
                    OUT_VERIFY: task_dir / OUT_VERIFY,
                }
                for name, dest in authored.items():
                    src = run_result.out_path(*name.split("/"))
                    if src.is_file():
                        shutil.copy2(src, dest)
                        if name.endswith(".sh"):
                            dest.chmod(0o755)

                missing = [n for n, d in authored.items() if not d.is_file()]
                if missing:
                    sandbox_mod.preserve_logs(
                        run_result.run_dir, log_root / tid,
                        keep_logs=cfg.execution.keep_logs, failed=True,
                    )
                    return _record(
                        "failed",
                        f"agent did not write: {', '.join(missing)}",
                    )

                # Build the starting state deterministically (no agent).
                workspace = task_dir / "workspace"
                try:
                    cutoff = repo_mod.build_cutoff_workspace(
                        cfg.codebase.repo, parent, workspace, forbidden_sha=sha
                    )
                except repo_mod.CutoffError as exc:
                    sandbox_mod.preserve_logs(
                        run_result.run_dir, log_root / tid,
                        keep_logs=cfg.execution.keep_logs, failed=True,
                    )
                    return _record("failed", f"starting-state construction failed: {exc}")

                incomplete = validate_task_dir(task_dir)
                if incomplete:
                    return _record(
                        "failed", f"task directory is incomplete: {', '.join(incomplete)}"
                    )

                # -- Layer 2: independent polarity re-check -----------------
                verifier = task_dir / OUT_VERIFY
                verify_logs = log_root / tid
                verify_logs.mkdir(parents=True, exist_ok=True)

                fail_side = run_verifier_against_tree(
                    cfg,
                    sandbox=sandbox,
                    verifier=verifier,
                    tree=workspace,
                    log_file=verify_logs / "verify_prechange.log",
                    scratch=scratch / "polarity-pre",
                )

                repo_mod.checkout_detached(tree, sha)
                pass_side = run_verifier_against_tree(
                    cfg,
                    sandbox=sandbox,
                    verifier=verifier,
                    tree=tree,
                    log_file=verify_logs / "verify_postchange.log",
                    scratch=scratch / "polarity-post",
                )

                polarity = {
                    "prechange_result": fail_side,
                    "postchange_result": pass_side,
                    "workspace_object_count": cutoff.object_count,
                    "workspace_cutoff_sha": cutoff.cutoff_sha,
                }

                sandbox_mod.preserve_logs(
                    run_result.run_dir, log_root / tid / "agent",
                    keep_logs=cfg.execution.keep_logs,
                    failed=not (fail_side == "FAIL" and pass_side == "PASS"),
                )

                if fail_side != "FAIL":
                    return _record(
                        "failed",
                        "verifier polarity is wrong: it should FAIL on the "
                        f"pre-change starting state but returned {fail_side}. "
                        "A verifier that passes without the change would certify "
                        "solvers that did nothing.",
                        **polarity,
                    )
                if pass_side != "PASS":
                    return _record(
                        "failed",
                        "verifier polarity is wrong: it should PASS on the "
                        f"post-change tree but returned {pass_side}. This usually "
                        "means the verifier depends on something available while "
                        "authoring but absent at grading time.",
                        **polarity,
                    )

                return _record("ok", **polarity)

    return worker


def run(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool = False,
    only: Sequence[str] = (),
    force: bool = False,
    progress: bool = True,
    limit: int | None = None,
) -> parallel.BatchOutcome:
    """Execute the build stage, building commits in rank order.

    ``limit`` caps how many commits this invocation starts (see :func:`plan`).
    """
    rank_, size = parallel.mpi_rank_size()
    is_rank_zero = rank_ == 0

    # Destructive setup on one rank only, then a barrier so no worker reads a
    # directory another rank is deleting.
    work_plan = plan(cfg, only=only, force=force, sweep=is_rank_zero, limit=limit)
    if is_rank_zero:
        cfg.build_dir.mkdir(parents=True, exist_ok=True)
        write_group_yaml(cfg)
        if progress:
            print(work_plan.describe())
            print()

    if cfg.execution.parallel == "mpi" and size > 1:
        usable, _ = parallel.mpi_available()
        if usable:
            from mpi4py import MPI

            MPI.COMM_WORLD.Barrier()

    if not work_plan.pending:
        return parallel.BatchOutcome(results=[], is_primary=is_rank_zero)

    worker = make_worker(cfg, pack, model, dry_run=dry_run)

    def on_result(result: parallel.ItemResult, index: int, total: int) -> None:
        """Report one completed task."""
        if not progress:
            return
        if result.status == "ok":
            tail = "ok"
        elif result.status == "dry_run":
            tail = "dry run"
        else:
            tail = f"FAILED: {result.detail.splitlines()[0][:70] if result.detail else ''}"
        print(f"  [{index:>3}/{total}] {result.item_id}  {tail}", flush=True)

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
    """Return aggregate statistics over emitted tasks, from the manifest."""
    entries = read_manifest(manifest_path(cfg))
    latest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        tid = entry.get("task_id")
        if tid:
            latest[str(tid)] = entry

    counts: dict[str, int] = {}
    for entry in latest.values():
        status = str(entry.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1

    return {
        "tasks": len(latest),
        "by_status": dict(sorted(counts.items())),
        "ok": counts.get("ok", 0),
        "failed": counts.get("failed", 0),
        "group_dir": str(group_dir(cfg)),
        "failures": [
            {"task_id": tid, "detail": entry.get("detail", "")}
            for tid, entry in sorted(latest.items())
            if entry.get("status") == "failed"
        ],
    }
