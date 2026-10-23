#!/usr/bin/env python3
"""The ``abc-mine`` command-line interface.

Subcommands, in the order a run uses them::

    init        create a run and lock its configuration
    preflight   prove the container can build and test the codebase
    screen      score every selected commit against the rubric   (parallel)
    rank        collapse viable summaries into a ranked shortlist (one agent)
    build       turn each shortlisted commit into a task         (parallel)
    status      report where a run stands
    export      copy emitted tasks into a abc-bench checkout
    prompts     inspect, scaffold, and render prompt customization

Every stage command loads the config, loads the prompt pack, resolves the model
from the environment, and **checks the invocation against the run's locked
configuration** before doing any work. That ordering is deliberate: all of it is
cheap, and all of it fails in seconds rather than after provisioning containers.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from . import config as config_mod
from . import manifest as manifest_mod
from . import preflight as preflight_mod
from . import prompts as prompts_mod
from . import repo as repo_mod
from .config import ConfigError, MineConfig
from .manifest import ManifestError
from .prompts import PromptError, PromptPack
from .stages import build as build_stage, rank as rank_stage, screen as screen_stage


class CliError(RuntimeError):
    """A user-facing error. Reported without a traceback."""


# -- shared plumbing ---------------------------------------------------------


def _collect_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Translate CLI flags into dotted config overrides.

    CLI flags win over the file but are folded into the *same* fingerprint the
    manifest locks, so ``--workers 32`` is visible to drift detection rather than
    being an invisible per-invocation difference.
    """
    overrides: dict[str, Any] = {}
    mapping = (
        ("parallel", "execution.parallel"),
        ("workers", "execution.workers"),
        ("max_attempts", "execution.max_attempts"),
        ("timeout", "execution.timeout_seconds"),
        ("scratch_dir", "execution.scratch_dir"),
        ("keep_logs", "execution.keep_logs"),
        ("run_id", "run.id"),
        ("data_root", "run.data_root"),
        ("rank_method", "rank.method"),
    )
    for attr, dotted in mapping:
        value = getattr(args, attr, None)
        if value is not None:
            overrides[dotted] = value
    return overrides


def _load(args: argparse.Namespace) -> tuple[MineConfig, PromptPack]:
    """Load configuration and the prompt pack, reporting problems clearly."""
    if not getattr(args, "config", None):
        raise CliError("--config is required")
    try:
        cfg = config_mod.load(args.config, overrides=_collect_overrides(args))
    except ConfigError as exc:
        raise CliError(str(exc)) from exc

    try:
        pack = prompts_mod.load_pack(
            cfg.prompts.pack,
            allow_template_overrides=cfg.prompts.allow_template_overrides,
        )
    except PromptError as exc:
        raise CliError(str(exc)) from exc

    return cfg, pack


def _announce_pack(pack: PromptPack) -> None:
    """Print the override banner and any pack warnings.

    Overrides are announced at every stage start, not just at init: a run assembled
    from several allocations should never let a non-stock prompt configuration go
    unnoticed.
    """
    banner = pack.banner()
    if banner:
        print(banner, file=sys.stderr)
        print(file=sys.stderr)
    for warning in pack.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if pack.warnings:
        print(file=sys.stderr)


def _prepare_stage(
    args: argparse.Namespace, stage: str
) -> tuple[MineConfig, PromptPack, dict[str, str]]:
    """Load everything a stage needs and enforce the configuration lock."""
    cfg, pack = _load(args)
    _announce_pack(pack)

    try:
        model = config_mod.llm_env()
    except ConfigError as exc:
        raise CliError(str(exc)) from exc

    problems = config_mod.validate_paths(cfg)
    if problems:
        raise CliError("configuration problems:\n  - " + "\n  - ".join(problems))

    try:
        run_manifest = manifest_mod.load(cfg)
    except ManifestError as exc:
        raise CliError(str(exc)) from exc

    try:
        drifts = manifest_mod.enforce(
            run_manifest,
            cfg,
            pack,
            model,
            allow_drift=getattr(args, "allow_config_drift", False),
        )
    except ManifestError as exc:
        raise CliError(str(exc)) from exc

    # Report from rank 0 only: under a parallel launcher every rank computes the
    # same drift, and N copies of the same report buries it rather than conveying it.
    noteworthy = [d for d in drifts if d.severity != "exempt"]
    if noteworthy and _launcher_rank() == 0:
        print(
            manifest_mod.format_drift_report(
                drifts, allowed=getattr(args, "allow_config_drift", False)
            ),
            file=sys.stderr,
        )
        print(file=sys.stderr)

    run_manifest.record_stage(
        stage,
        {
            "model_id": model.get("model", ""),
            "parallel": cfg.execution.parallel,
            "workers": cfg.execution.workers,
            "dry_run": bool(getattr(args, "dry_run", False)),
        },
    )
    return cfg, pack, model


def _header(cfg: MineConfig, title: str) -> None:
    """Print a stage header."""
    print("=" * 72)
    print(f"abc-mine {title}")
    print(f"  run:      {cfg.run_id}")
    print(f"  codebase: {cfg.codebase.name} ({cfg.codebase.repo})")
    print(f"  output:   {cfg.run_dir}")
    print("=" * 72)
    print()


# -- init --------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create a run directory and lock its configuration."""
    cfg, pack = _load(args)
    _announce_pack(pack)

    problems = config_mod.validate_paths(cfg)
    if problems:
        raise CliError("configuration problems:\n  - " + "\n  - ".join(problems))

    try:
        model = config_mod.llm_env()
    except ConfigError as exc:
        # init is allowed without credentials so a run can be prepared on a
        # machine that will never execute it; the lock then records no model, and
        # the first stage to run will supply and lock one via drift.
        if not args.allow_missing_model:
            raise CliError(
                str(exc) + "\n\n(Pass --allow-missing-model to initialize anyway, "
                "e.g. when preparing a run on a submission host.)"
            ) from exc
        print(f"WARNING: {exc}", file=sys.stderr)
        print(file=sys.stderr)
        model = {"base_url": "", "model": "", "api": ""}

    cfg.run_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = cfg.run_dir / manifest_mod.MANIFEST_NAME
    if args.reinit and manifest_path.is_file():
        # Re-lock an existing run to the current settings. Outputs are untouched;
        # only the lock is rewritten, and the previous one is kept beside it so the
        # change remains auditable.
        backup = manifest_path.with_suffix(".json.superseded")
        shutil.copy2(manifest_path, backup)
        manifest_path.unlink()
        print(f"Re-initializing: previous manifest saved as {backup.name}")

    try:
        run_manifest = manifest_mod.create(cfg, pack, model, notes=args.notes or "")
    except ManifestError as exc:
        raise CliError(str(exc)) from exc

    toolchain_problems = preflight_mod.check_config(cfg)

    print(f"Initialized run '{cfg.run_id}' at {cfg.run_dir}")
    print()
    git = run_manifest.data.get("codebase", {}).get("git", {})
    print(f"  codebase:   {cfg.codebase.name}")
    print(f"    HEAD:     {git.get('head', '?')[:12]}  {git.get('head_subject', '')[:48]}")
    print(f"    commits:  {git.get('commit_count', '?')}")
    print(f"  container:  {cfg.container.image}")
    print(f"  model:      {model.get('model') or '(not configured)'}")
    print(f"  prompts:    {cfg.prompts.pack or '(generic, no pack)'}")
    if pack.slots:
        print(f"    slots:    {', '.join(sorted(pack.slots))}")
    print(f"  manifest:   {run_manifest.path}")
    print()

    if toolchain_problems:
        print("Toolchain review:", file=sys.stderr)
        for problem in toolchain_problems:
            print(f"  - {problem}", file=sys.stderr)
        print(file=sys.stderr)

    print("Next:  abc-mine preflight --config <config>")
    print("       (proves the container can actually build and test this codebase)")
    return 0


# -- preflight ---------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> int:
    """Prove the container can build and test the codebase."""
    cfg, pack = _load(args)
    _header(cfg, "preflight")

    problems = config_mod.validate_paths(cfg)
    if problems:
        raise CliError("configuration problems:\n  - " + "\n  - ".join(problems))

    static = preflight_mod.check_config(cfg)
    if static:
        print("Toolchain review:")
        for problem in static:
            print(f"  - {problem}")
        print()

    result = preflight_mod.run(
        cfg,
        dry_run=args.dry_run,
        include_full_test=args.full_test,
    )
    print(result.describe())
    print()

    report = preflight_mod.save_report(cfg, result) if cfg.run_dir.exists() else None
    if report:
        print(f"Report: {report}")

    if result.ok and args.write_config:
        if result.full_build_seconds is None and result.targeted_test_seconds is None:
            print("Nothing measured to write back.")
        else:
            backup = preflight_mod.write_measurements(cfg, result)
            print(f"Wrote measured timings into {cfg.source_path}")
            print(f"  (previous version saved as {backup.name})")
            print(
                "  Every stage now quotes these measured figures in its prompts, "
                "so cost\n  judgements are based on fact rather than assumption."
            )
    elif result.ok and not args.dry_run:
        print(
            "Tip: re-run with --write-config to record the measured build and test\n"
            "     costs in your config. Agents reason about verification cost, and\n"
            "     a measured number is far better than an assumed one."
        )

    if not result.ok:
        print()
        print(
            "Preflight FAILED. The mining stages depend on these commands working\n"
            "inside the container: screening agents use them to check candidates,\n"
            "and every emitted benchmark's verifier must call them at grading time.\n"
            "Running the workflow now would burn agent budget producing tasks that\n"
            "cannot be verified.",
            file=sys.stderr,
        )
        return 1

    print()
    print("Next:  abc-mine screen --config <config>")
    return 0


# -- screen ------------------------------------------------------------------


def cmd_screen(args: argparse.Namespace) -> int:
    """Score every selected commit against the rubric."""
    cfg, pack, model = _prepare_stage(args, "screen")
    _header(cfg, "screen")

    if args.plan:
        work_plan = screen_stage.plan(cfg, only=args.only)
        print(work_plan.describe(cfg.execution.max_attempts))
        return 0

    _warn_if_no_preflight(cfg)

    outcome = screen_stage.run(
        cfg, pack, model, dry_run=args.dry_run, only=args.only
    )
    if not outcome.is_primary:
        return 0

    print()
    print(outcome.describe())
    stats = screen_stage.summarize(cfg)
    print()
    print(f"Run totals: {stats['screened']} screened, {stats['viable']} viable, "
          f"{stats['disqualified']} disqualified")
    if stats.get("viable"):
        print(f"  best score {stats['score_max']}, mean {stats['score_mean']}")
    usage = stats.get("usage") or {}
    if usage.get("requests"):
        print(
            f"  model usage: {usage['requests']} requests, "
            f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens"
        )

    failed = outcome.counts.get("failed", 0)
    if failed:
        print()
        print(
            f"{failed} item(s) failed. They are recorded in "
            f"{screen_stage.attempts_path(cfg)}\n"
            f"and will be retried on the next run, up to "
            f"execution.max_attempts ({cfg.execution.max_attempts}).",
            file=sys.stderr,
        )
    print()
    print("Next:  abc-mine rank --config <config>")
    return 0


def _warn_if_no_preflight(cfg: MineConfig) -> None:
    """Warn when a stage runs without a passing preflight on record."""
    report = preflight_mod.load_report(cfg)
    if report is None:
        print(
            "WARNING: no preflight report for this run. The workflow assumes the\n"
            "         container can build and test this codebase; if it cannot,\n"
            "         agents will spend budget producing unverifiable results.\n"
            "         Run `abc-mine preflight --config <config>` first.\n",
            file=sys.stderr,
        )
    elif not report.get("ok"):
        print(
            "WARNING: the recorded preflight for this run did not pass. Expect\n"
            "         poor results until the toolchain works inside the container.\n",
            file=sys.stderr,
        )


# -- rank --------------------------------------------------------------------


def cmd_rank(args: argparse.Namespace) -> int:
    """Collapse viable summaries into a ranked shortlist."""
    cfg, pack, model = _prepare_stage(args, "rank")
    _header(cfg, "rank")

    try:
        if args.plan:
            if cfg.rank.method == "interleaving":
                from .stages import rank_interleaving

                il_plan = rank_interleaving.plan(cfg, pack, model)
                print(il_plan.describe())
                over = il_plan.over_budget
            else:
                single = rank_stage.plan(cfg, pack, model)
                print(single.describe())
                over = [single] if single.over_budget else []
            for group_plan in over:
                print()
                print(rank_stage._budget_error(group_plan))
            return 1 if over else 0

        shortlist = rank_stage.run(
            cfg, pack, model, dry_run=args.dry_run, force=args.force
        )
    except rank_stage.RankError as exc:
        raise CliError(str(exc)) from exc

    # Non-primary MPI ranks have done their share; rank 0 reports.
    if shortlist.get("dry_run") or shortlist.get("is_primary") is False:
        return 0

    print()
    print("Next:  abc-mine build --config <config>")
    return 0


# -- build -------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    """Turn each shortlisted commit into a abc-bench task."""
    cfg, pack, model = _prepare_stage(args, "build")
    _header(cfg, "build")

    try:
        if args.plan:
            print(build_stage.plan(
                cfg, only=args.only, force=args.force, limit=args.limit
            ).describe())
            return 0

        _warn_if_no_preflight(cfg)
        outcome = build_stage.run(
            cfg, pack, model,
            dry_run=args.dry_run, only=args.only, force=args.force,
            limit=args.limit,
        )
    except rank_stage.RankError as exc:
        raise CliError(str(exc)) from exc

    if not outcome.is_primary:
        return 0

    print()
    print(outcome.describe())
    stats = build_stage.summarize(cfg)
    print()
    print(f"Emitted tasks: {stats['ok']} ok, {stats['failed']} failed")
    print(f"  group: {stats['group_dir']}")

    if stats["failures"]:
        print()
        print("Failures:", file=sys.stderr)
        for failure in stats["failures"]:
            detail = (failure["detail"] or "").splitlines()
            print(f"  {failure['task_id']}: {detail[0] if detail else ''}", file=sys.stderr)

    if stats["ok"]:
        print()
        print("Next:  abc-mine export --config <config> --dest <abc-bench-checkout>")
    return 0 if not stats["failed"] else 1


# -- status ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Report where a run stands."""
    cfg, pack = _load(args)

    try:
        run_manifest = manifest_mod.load(cfg)
    except ManifestError as exc:
        raise CliError(str(exc)) from exc

    print("=" * 72)
    print(f"Run: {cfg.run_id}")
    print("=" * 72)
    print(f"  created:    {run_manifest.created_at} by "
          f"{run_manifest.data.get('run', {}).get('created_by', '?')}")
    print(f"  codebase:   {cfg.codebase.name}")
    git = run_manifest.data.get("codebase", {}).get("git", {})
    print(f"    HEAD:     {git.get('head', '?')[:12]} ({git.get('commit_count', '?')} commits)")
    print(f"  container:  {cfg.container.image}")
    locked_model = run_manifest.fingerprint.get("model.id") or "(none)"
    print(f"  model:      {locked_model}")
    overrides = run_manifest.data.get("prompt_overrides_active") or []
    print(f"  prompts:    {cfg.prompts.pack or '(generic)'}"
          + (f"  OVERRIDES: {', '.join(overrides)}" if overrides else ""))
    if run_manifest.data.get("accepted_drift"):
        print(f"  NOTE: this run has accepted configuration drift "
              f"({len(run_manifest.data['accepted_drift'])} time(s)); its outputs "
              f"are a mixture")
    print()

    report = preflight_mod.load_report(cfg)
    if report is None:
        print("  preflight:  not run")
    else:
        mark = "passed" if report.get("ok") else "FAILED"
        detail = []
        if report.get("full_build_seconds"):
            detail.append(f"build {config_mod._humanize_seconds(report['full_build_seconds'])}")
        if report.get("targeted_test_seconds"):
            detail.append(
                f"targeted test {config_mod._humanize_seconds(report['targeted_test_seconds'])}"
            )
        print(f"  preflight:  {mark}" + (f"  ({', '.join(detail)})" if detail else ""))

    screen_stats = screen_stage.summarize(cfg)
    if screen_stats["screened"]:
        print(
            f"  screen:     {screen_stats['screened']} screened, "
            f"{screen_stats['viable']} viable, "
            f"{screen_stats['disqualified']} disqualified"
        )
        failures = manifest_mod.attempt_counts(
            screen_stage.attempts_path(cfg), status="failed"
        )
        if failures:
            spent = manifest_mod.exhausted_items(
                screen_stage.attempts_path(cfg), cfg.execution.max_attempts
            )
            print(f"                {len(failures)} with failures, "
                  f"{len(spent)} out of attempts")
    else:
        print("  screen:     not run")

    try:
        shortlist = rank_stage.load_shortlist(cfg)
        method = shortlist.get("method", {})
        print(
            f"  rank:       {shortlist.get('n_selected', 0)} "
            f"{'ranked' if shortlist.get('complete') else 'selected'} of "
            f"{shortlist.get('n_candidates', 0)} candidates"
            + ("  (single-context)" if method.get("single_context") else "")
            + (f"  (interleaving, {method.get('groups')} groups)"
               if method.get("name") == "interleaving" else "")
        )
    except rank_stage.RankError:
        print("  rank:       not run")

    build_stats = build_stage.summarize(cfg)
    if build_stats["tasks"]:
        print(f"  build:      {build_stats['ok']} ok, {build_stats['failed']} failed")
    else:
        print("  build:      not run")

    print()
    if args.verbose:
        print("Stage history:")
        for record in run_manifest.data.get("stages", []):
            job = f" job {record['slurm_job_id']}" if record.get("slurm_job_id") else ""
            print(f"  {record.get('at', '?')}  {record.get('stage', '?'):<10}"
                  f" on {record.get('host', '?')}{job}")
            # Resources are provenance, not configuration: they explain a measured
            # timing rather than constraining a resume. Shown so a run assembled
            # from differently-sized allocations can be interpreted.
            res = record.get("resources") or {}
            parts = [
                f"{k.replace('SLURM_', '').lower()}={v}"
                for k, v in res.items()
                if k not in ("SLURM_JOB_ID", "SLURM_JOB_NODELIST")
            ]
            if parts:
                print(f"    {' '.join(parts)}")
            model = record.get("model_id")
            if model:
                print(f"    model={model} parallel={record.get('parallel', '?')}"
                      f" workers={record.get('workers', '?')}")
    return 0


# -- export ------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    """Copy emitted tasks into a abc-bench checkout."""
    cfg, _ = _load(args)
    source = build_stage.group_dir(cfg)
    if not source.is_dir():
        raise CliError(
            f"no emitted task group at {source}\n"
            "Run the build stage first:  abc-mine build --config <config>"
        )

    ok_tasks = build_stage.completed_tasks(build_stage.manifest_path(cfg))
    if not ok_tasks:
        raise CliError(
            "no tasks have been built successfully, so there is nothing to export.\n"
            f"See {build_stage.manifest_path(cfg)} for what failed."
        )

    dest_root = Path(args.dest).expanduser().resolve()
    dest = dest_root / "tasks" / cfg.group_id if args.abc_bench_root else dest_root
    dest.mkdir(parents=True, exist_ok=True)

    # Only verified-ok tasks are exported. A failed task's directory may exist and
    # look complete, but its verifier is known not to discriminate — exporting it
    # would put a benchmark that cannot grade anything into a real suite.
    exported: list[str] = []
    for child in sorted(source.iterdir()):
        if child.is_file():
            shutil.copy2(child, dest / child.name)
            continue
        if child.name not in ok_tasks:
            continue
        target = dest / child.name
        if target.exists():
            if not args.force:
                print(f"  skip {child.name} (already present; --force to overwrite)")
                continue
            shutil.rmtree(target)
        shutil.copytree(child, target, symlinks=True)
        exported.append(child.name)

    print(f"Exported {len(exported)} task(s) to {dest}")
    for name in exported:
        print(f"  {name}")

    skipped = len(ok_tasks) - len(exported)
    if skipped > 0:
        print(f"  ({skipped} already present)")

    print()
    print("Remaining manual steps:")
    print(f"  1. Provide a container template for the group, e.g.")
    print(f"       containers/task_groups/{cfg.group_id}.Dockerfile.jinja2")
    print(f"     It must supply the same toolchain the verifiers call.")
    print(f"  2. Run:  abc-bench run {cfg.group_id}")
    return 0


# -- prompts -----------------------------------------------------------------


def cmd_prompts(args: argparse.Namespace) -> int:
    """Inspect, scaffold, and render prompt customization."""
    action = args.prompts_action

    if action == "slots":
        print("Prompt slots — optional per-codebase context.\n")
        print("Each is a markdown file under <pack>/slots/. Every one is optional:")
        print("an absent or empty file omits its section entirely.\n")
        for slot in prompts_mod.SLOTS:
            stages = ", ".join(sorted({t.split("_")[0] for t in slot.templates}))
            print(f"  {slot.key}")
            print(f"    used by:  {stages}")
            print(f"    heading:  {slot.heading.lstrip('# ')}")
            print(f"    purpose:  {slot.purpose}")
            print()
        print("Scaffold a pack:  abc-mine prompts scaffold <dir>")
        return 0

    if action == "scaffold":
        created = prompts_mod.scaffold(args.dest, with_overrides=args.with_overrides)
        dest = Path(args.dest)
        print(f"Scaffolded a prompt pack at {dest}")
        for path in created:
            print(f"  {path.relative_to(dest)}")
        print()
        print("Fill in the slots you need and delete the rest. Then point your")
        print("config at it:")
        print()
        print("    prompts:")
        print(f"      pack: {dest}")
        if args.with_overrides:
            print()
            print("An overrides/ directory was also created. Overrides REPLACE a")
            print("shipped template rather than adding to it, and require")
            print("prompts.allow_template_overrides: true. Prefer slots unless you")
            print("specifically need to rewrite a base template — see")
            print(f"{dest / 'overrides' / 'README.md'}.")
        return 0

    if action == "show":
        name = args.template
        if name not in prompts_mod.TEMPLATE_NAMES:
            raise CliError(
                f"unknown template {name!r}. Available: "
                + ", ".join(prompts_mod.TEMPLATE_NAMES)
            )
        sys.stdout.write(prompts_mod.load_template(name))
        return 0

    if action == "render":
        return _render_prompt(args)

    raise CliError(f"unknown prompts action: {action!r}")


def _render_prompt(args: argparse.Namespace) -> int:
    """Render a fully-composed prompt exactly as an agent would receive it.

    This exists so prompt engineering stays inspectable. Slots, overrides, the
    single-sourced rubric tables, and the measured toolchain block all compose into
    the final text, and reasoning about that by reading source is a poor substitute
    for looking at the result.
    """
    cfg, pack = _load(args)
    _announce_pack(pack)

    stage = args.stage
    work = "/workspace"
    out = f"{work}/out"
    common = {
        "codebase_name": cfg.codebase.name,
        "toolchain_block": cfg.toolchain.render_prompt_block(cfg.codebase.source_mount),
        "source_path": cfg.codebase.source_mount,
        "run_id": cfg.run_id,
    }

    if stage == "screen":
        # A real commit where possible, so the rendering shows true sizes and
        # subjects rather than placeholder text.
        try:
            commits = repo_mod.annotate(
                cfg.codebase.repo,
                repo_mod.enumerate_commits(cfg.codebase.repo)[:1],
            )
            commit = commits[0]
        except (repo_mod.GitError, IndexError):
            commit = repo_mod.Commit(
                sha="0" * 40, parent="1" * 40, subject="(example commit)"
            )
        text = prompts_mod.compose(
            "screen_system", "screen_task", pack=pack,
            variables={
                **common,
                "rubric_axis_table": cfg.rubric.render_axis_table(),
                "rubric_axis_guidance": cfg.rubric.render_axis_guidance(),
                "rubric_axes_schema": cfg.rubric.render_axes_schema(),
                "task_types": ", ".join(f"`{t}`" for t in cfg.rubric.task_types),
                "sha": commit.sha,
                "parent": commit.parent,
                "subject": commit.subject,
                "author_date": commit.author_date or "unknown",
                "files_changed": commit.files_changed,
                "diff_lines": commit.diff_lines,
                "touched_files": "  - `example/path.f90`",
                "diff_truncation_note": "",
                "diff_path": f"{work}/commit.diff",
                "message_path": f"{work}/commit_message.txt",
                "summary_path": f"{out}/summary.json",
            },
        )
    elif stage == "rank":
        text = prompts_mod.compose(
            "rank_system", "rank_task", pack=pack,
            variables={
                **common,
                "candidates_block": rank_stage.render_candidates([{
                    "sha": "0" * 40, "subject": "(example candidate)",
                    "benchmark_score": 50, "one_line": "(example)",
                    "axes": {"verifiability": {"score": 3, "why": "(example)"}},
                }]),
                "shortlist_path": f"{out}/shortlist.json",
                "rationale_path": f"{out}/rationale.md",
                "n_candidates": 42,
                "n_disqualified": 17,
                "candidate_scope": rank_stage.SCOPE_ALL,
            },
        )
    else:
        text = prompts_mod.compose(
            "build_system", "build_task", pack=pack,
            variables={
                **common,
                "sha": "0" * 40,
                "parent": "1" * 40,
                "subject": "(example task description)",
                "rank": 1,
                "est_difficulty": "medium",
                "task_type": "bug_fix",
                "proposed_task": "_(example proposed task)_",
                "proposed_verification": "_(example proposed verification)_",
                "screening_summary": "```json\n{ \"example\": true }\n```",
                "shortlist_entry": "```json\n{ \"example\": true }\n```",
                "reference_diff_path": f"{work}/reference.diff",
                "message_path": f"{work}/commit_message.txt",
                "out_prompt": f"{out}/prompt.md",
                "out_build_env": f"{out}/build_env.sh",
                "out_verify": f"{out}/verification/verify_solution.sh",
            },
        )

    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote the composed {stage} prompt to {args.output} "
              f"({len(text):,} chars, ~{int(len(text) / 3.5):,} tokens)")
    else:
        sys.stdout.write(text)
    return 0


# -- argument parsing --------------------------------------------------------


def _add_config(parser: argparse.ArgumentParser) -> None:
    """Add the ``--config`` option."""
    parser.add_argument(
        "--config", required=True, metavar="PATH", help="Run configuration YAML."
    )


def _add_execution(parser: argparse.ArgumentParser) -> None:
    """Add execution overrides shared by the stage commands."""
    group = parser.add_argument_group("execution overrides")
    group.add_argument(
        "--parallel",
        choices=("serial", "local", "mpi"),
        help="Parallel backend. 'serial' (default) needs no MPI; 'local' forks "
             "worker processes on this node; 'mpi' fans out across ranks and "
             "requires the [mpi] extra plus a launcher.",
    )
    group.add_argument("--workers", type=int, help="Worker count for --parallel local.")
    group.add_argument("--max-attempts", type=int, dest="max_attempts",
                       help="Attempts per item before giving up.")
    group.add_argument("--timeout", type=int, help="Per-agent timeout, seconds.")
    group.add_argument("--scratch-dir", dest="scratch_dir",
                       help="Node-local scratch root (keep off shared filesystems).")
    group.add_argument("--keep-logs", dest="keep_logs",
                       choices=("none", "failed", "all"),
                       help="Agent transcript retention (default: failed).")
    group.add_argument("--run-id", dest="run_id", help="Override run.id.")
    group.add_argument("--data-root", dest="data_root", help="Override run.data_root.")
    group.add_argument(
        "--allow-config-drift",
        action="store_true",
        dest="allow_config_drift",
        help="Proceed even when settings differ from the run's locked "
             "configuration. The run's outputs then become a mixture; the change "
             "is recorded in the manifest.",
    )
    group.add_argument("--dry-run", action="store_true",
                       help="Print container commands without executing agents.")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="abc-mine",
        description=(
            "Mine a git codebase for agentic-coding benchmarks and emit "
            "ABC Bench tasks, in three stages: screen, rank, build."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical run:\n"
            "  abc-mine init      --config mine.yaml\n"
            "  abc-mine preflight --config mine.yaml --write-config\n"
            "  abc-mine screen    --config mine.yaml --parallel local --workers 8\n"
            "  abc-mine rank      --config mine.yaml\n"
            "  abc-mine build     --config mine.yaml --parallel local --workers 4\n"
            "  abc-mine export    --config mine.yaml --dest ../abc-bench\n"
            "\n"
            "The model is configured entirely by environment variables:\n"
            "  LLM_BASE_URL  LLM_MODEL  LLM_API  LLM_API_KEY\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"abc-mine {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = subparsers.add_parser(
        "init", help="Create a run and lock its configuration."
    )
    _add_config(p_init)
    p_init.add_argument("--notes", help="Free-text note recorded in the manifest.")
    p_init.add_argument(
        "--allow-missing-model", action="store_true",
        help="Initialize without model credentials (e.g. on a submission host).",
    )
    p_init.add_argument(
        "--reinit", action="store_true",
        help="Re-lock an already-initialized run to the current settings. Existing "
             "outputs are left untouched and the previous manifest is kept as "
             "run.json.superseded. Use this to recover a run whose manifest was "
             "lost, or to deliberately adopt changed settings mid-run.",
    )
    p_init.set_defaults(func=cmd_init)

    p_pre = subparsers.add_parser(
        "preflight",
        help="Prove the container can build and test the codebase, and measure cost.",
    )
    _add_config(p_pre)
    p_pre.add_argument("--dry-run", action="store_true",
                       help="Print the container commands without running them.")
    p_pre.add_argument("--full-test", action="store_true",
                       help="Also time the full test suite (can be very slow).")
    p_pre.add_argument("--write-config", action="store_true",
                       help="Write measured timings back into the config file.")
    p_pre.set_defaults(func=cmd_preflight)

    p_screen = subparsers.add_parser(
        "screen", help="Score every selected commit against the rubric (parallel)."
    )
    _add_config(p_screen)
    _add_execution(p_screen)
    p_screen.add_argument("--only", nargs="+", default=(), metavar="SHA",
                          help="Screen only these commits (prefix match).")
    p_screen.add_argument("--plan", action="store_true",
                          help="Show what would be screened, then exit.")
    p_screen.set_defaults(func=cmd_screen)

    p_rank = subparsers.add_parser(
        "rank", help="Collapse viable summaries into a ranked shortlist (one agent)."
    )
    _add_config(p_rank)
    _add_execution(p_rank)
    p_rank.add_argument("--plan", action="store_true",
                        help="Show the candidate payload and token estimate, then exit.")
    p_rank.add_argument("--force", action="store_true",
                        help="Discard an existing shortlist (and any recorded "
                             "interleaving groups) and rank again.")
    p_rank.add_argument("--method", dest="rank_method",
                        choices=config_mod.RANK_METHODS,
                        help="Override rank.method.")
    p_rank.set_defaults(func=cmd_rank)

    p_build = subparsers.add_parser(
        "build", help="Turn each shortlisted commit into a abc-bench task (parallel)."
    )
    _add_config(p_build)
    _add_execution(p_build)
    p_build.add_argument("--only", nargs="+", default=(), metavar="SHA",
                         help="Build only these commits (prefix match).")
    p_build.add_argument("--plan", action="store_true",
                         help="Show what would be built, then exit.")
    p_build.add_argument("--force", action="store_true",
                         help="Rebuild tasks already recorded as complete.")
    p_build.add_argument("--limit", type=int, metavar="N",
                         help="Start at most N commits in this invocation, from the "
                              "top of the remaining rank order. A per-job stopping "
                              "point, not a run setting: rerun to continue down the "
                              "order.")
    p_build.set_defaults(func=cmd_build)

    p_status = subparsers.add_parser("status", help="Report where a run stands.")
    _add_config(p_status)
    p_status.add_argument("-v", "--verbose", action="store_true",
                          help="Also show the stage invocation history.")
    p_status.set_defaults(func=cmd_status)

    p_export = subparsers.add_parser(
        "export", help="Copy successfully-built tasks into a abc-bench checkout."
    )
    _add_config(p_export)
    p_export.add_argument("--dest", required=True,
                          help="Destination: a abc-bench checkout, or any directory.")
    p_export.add_argument(
        "--abc-bench-root", action="store_true", default=True,
        help="Treat --dest as a abc-bench checkout and write to "
             "<dest>/tasks/<group>/ (default).",
    )
    p_export.add_argument(
        "--raw", dest="abc_bench_root", action="store_false",
        help="Treat --dest as a literal destination directory.",
    )
    p_export.add_argument("--force", action="store_true",
                          help="Overwrite tasks already present at the destination.")
    p_export.set_defaults(func=cmd_export)

    p_prompts = subparsers.add_parser(
        "prompts", help="Inspect, scaffold, and render prompt customization."
    )
    prompt_subs = p_prompts.add_subparsers(dest="prompts_action", metavar="<action>")

    p_slots = prompt_subs.add_parser("slots", help="List the available prompt slots.")
    p_slots.set_defaults(func=cmd_prompts)

    p_scaffold = prompt_subs.add_parser(
        "scaffold", help="Create an empty, commented prompt pack."
    )
    p_scaffold.add_argument("dest", help="Directory to create.")
    p_scaffold.add_argument(
        "--with-overrides", action="store_true",
        help="Also create overrides/ for full template replacement (advanced).",
    )
    p_scaffold.set_defaults(func=cmd_prompts)

    p_show = prompt_subs.add_parser(
        "show", help="Print a shipped base template (a good override starting point)."
    )
    p_show.add_argument("template", choices=prompts_mod.TEMPLATE_NAMES)
    p_show.set_defaults(func=cmd_prompts)

    p_render = prompt_subs.add_parser(
        "render",
        help="Render a fully-composed prompt exactly as an agent would receive it.",
    )
    _add_config(p_render)
    p_render.add_argument("stage", choices=("screen", "rank", "build"))
    p_render.add_argument("-o", "--output", help="Write to a file instead of stdout.")
    p_render.set_defaults(func=cmd_prompts)

    p_prompts.set_defaults(func=cmd_prompts, prompts_action="slots")
    return parser


def _launcher_rank() -> int:
    """Return this process's rank under a parallel launcher, or 0.

    Read from the launcher's own environment rather than by importing mpi4py,
    because this is used on the error path -- where MPI may be exactly what is
    broken -- and because it must work before any MPI initialization.

    Used only to decide which process reports a startup failure. Every rank still
    exits non-zero, so the job's status is unaffected; the point is that one
    legible error beats N identical copies of it. A 32-rank job hitting a
    configuration problem should not produce 32 stack traces.
    """
    for var in ("SLURM_PROCID", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "MV2_COMM_WORLD_RANK"):
        value = os.environ.get(var)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                continue
    return 0


def _report_error(message: str) -> None:
    """Print a startup error once, from rank 0 only."""
    if _launcher_rank() == 0:
        print(f"\nerror: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    if args.command == "prompts" and not getattr(args, "prompts_action", None):
        args.prompts_action = "slots"

    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        _report_error(str(exc))
        return 1
    except KeyboardInterrupt:
        if _launcher_rank() == 0:
            print("\ninterrupted", file=sys.stderr)
        return 130
    except (ConfigError, PromptError, ManifestError, repo_mod.GitError) as exc:
        _report_error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
