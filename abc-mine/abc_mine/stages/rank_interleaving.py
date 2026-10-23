"""RANK (interleaving method) — rank random groups separately, then interleave.

When every viable candidate cannot fit in one model context, this method splits
the candidates uniformly at random into ``rank.interleaving.groups`` groups of
(near-)equal size, ranks each group with its own single-context ranking agent —
exactly the agent, prompt, and validation the single-context method uses — and
merges the per-group shortlists by **interleaving** them: one pick from each
group in turn, round by round, until every group's shortlist is exhausted.

The assumption
--------------
Because the split is random, each group is a random sample of the same pool, so
with large enough groups each contains about the same share of strong
candidates. Interleaving takes that at face value: a group's k-th pick is
treated as equivalent in merit to any other group's k-th pick. Nothing compares
candidates across groups; the method relies on the sampling, not on judgement,
to make the groups comparable. It degrades as groups shrink (sampling noise
grows) and when the few best candidates happen to fall in the same group — then
the second best of that group ranks alongside the best of a weaker one.

Within a round, the order in which groups contribute is itself shuffled (seeded
by round), because a fixed order would systematically favour group 1: its k-th
pick would always precede every other group's k-th pick.

Parallelism
-----------
Groups are independent, so under ``execution.parallel: mpi`` they are ranked
concurrently: group ``i`` goes to MPI rank ``i % world_size`` (static
round-robin), each rank ranks its share serially, and all ranks then
synchronize and rank 0 merges. With as many ranks as groups, every group runs at
once. Every other backend ranks the groups serially in this process. Rank 0 alone
plans, validates, writes the split record, and writes the final shortlist; the
other ranks only run agents and save their own groups' results.

Resume
------
Each group's validated shortlist and rationale are saved to
``rank/interleaving/group-<n>/`` as soon as it finishes. A rerun skips
completed groups. ``rank/interleaving/split.json`` records the split; a
rerun that would produce a different split (different candidates, seed, or group
count) is refused, since completed groups would then have ranked a different
partition. ``--force`` sets the recorded state aside and starts over.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import parallel
from ..config import MineConfig
from ..prompts import PromptPack
from . import rank as rank_stage

#: Subdirectory of the rank directory holding per-group state.
STATE_DIRNAME = "interleaving"
SPLIT_NAME = "split.json"
GROUP_SHORTLIST = "shortlist.json"
GROUP_RATIONALE = "rationale.md"
GROUP_META = "result.json"

#: Split record schema version.
SPLIT_VERSION = 1


def state_dir(cfg: MineConfig) -> Path:
    return cfg.rank_dir / STATE_DIRNAME


def group_dir(cfg: MineConfig, index: int) -> Path:
    """Directory for group ``index`` (zero-based; named one-based)."""
    return state_dir(cfg) / f"group-{index + 1}"


# -- pure pieces -------------------------------------------------------------


def _rng(seed: int, *parts: object) -> random.Random:
    """A ``random.Random`` seeded deterministically from ``seed`` and ``parts``.

    Hash-derived rather than ``Random(seed)`` directly so that the split and the
    per-round shuffles draw from independent streams.
    """
    key = ":".join(str(p) for p in (seed, *parts)).encode()
    return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))


def split_groups(shas: Sequence[str], n_groups: int, seed: int) -> list[list[str]]:
    """Split ``shas`` uniformly at random into ``n_groups`` near-equal groups.

    Deterministic in ``(set of shas, n_groups, seed)``: the input order does not
    matter, so reordering the summaries on disk cannot change the partition.
    Group sizes differ by at most one. Within each group, SHAs are returned in
    sorted order (the prompt applies its own reading order).

    Raises
    ------
    ValueError
        If ``n_groups`` < 1, or there are fewer SHAs than groups.
    """
    if n_groups < 1:
        raise ValueError(f"n_groups must be >= 1, got {n_groups}")
    if len(set(shas)) != len(shas):
        raise ValueError("SHAs must be unique")
    if len(shas) < n_groups:
        raise ValueError(f"cannot split {len(shas)} candidate(s) into {n_groups} groups")
    pool = sorted(shas)
    _rng(seed, "split").shuffle(pool)
    groups = [sorted(pool[i::n_groups]) for i in range(n_groups)]
    return groups


def interleave(
    rankings: Sequence[Sequence[Any]], seed: int
) -> list[tuple[int, int, Any]]:
    """Interleave ranked lists, one item from each list per round.

    Round ``r`` takes the ``r``-th item (one-based) of every list that still has
    one; the order in which lists contribute within a round is shuffled from
    ``seed`` and ``r``. Lists may differ in length: an exhausted list simply
    stops contributing.

    Returns ``(group_index, rank_in_group, item)`` tuples in merged order.
    """
    merged: list[tuple[int, int, Any]] = []
    depth = max((len(r) for r in rankings), default=0)
    for round_number in range(1, depth + 1):
        order = list(range(len(rankings)))
        _rng(seed, "round", round_number).shuffle(order)
        for g in order:
            if round_number <= len(rankings[g]):
                merged.append((g, round_number, rankings[g][round_number - 1]))
    return merged


def split_record(
    groups: Sequence[Sequence[str]],
    seed: int,
    candidate_shas: Sequence[str],
    output: str = "shortlist",
) -> dict[str, Any]:
    """The persisted description of a split (what a resume must match)."""
    return {
        "version": SPLIT_VERSION,
        "output": output,
        "seed": seed,
        "n_groups": len(groups),
        "candidates_digest": hashlib.sha256(
            "\n".join(sorted(candidate_shas)).encode()
        ).hexdigest(),
        "groups": [list(g) for g in groups],
    }


def split_mismatch(existing: dict[str, Any], fresh: dict[str, Any]) -> list[str]:
    """Human-readable reasons a recorded split differs from the one now implied."""
    problems: list[str] = []
    if existing.get("version") != SPLIT_VERSION:
        problems.append(f"split record version {existing.get('version')!r} != {SPLIT_VERSION}")
    if existing.get("candidates_digest") != fresh["candidates_digest"]:
        problems.append("the candidate set changed")
    if existing.get("output", "shortlist") != fresh.get("output", "shortlist"):
        problems.append(
            f"rank.output changed ({existing.get('output', 'shortlist')} -> "
            f"{fresh.get('output', 'shortlist')})"
        )
    if existing.get("seed") != fresh["seed"]:
        problems.append(f"rank.interleaving.seed changed ({existing.get('seed')} -> {fresh['seed']})")
    if existing.get("n_groups") != fresh["n_groups"]:
        problems.append(
            f"rank.interleaving.groups changed ({existing.get('n_groups')} -> {fresh['n_groups']})"
        )
    if not problems and existing.get("groups") != fresh["groups"]:
        problems.append("the recorded partition differs from the one implied by the settings")
    return problems


# -- plan --------------------------------------------------------------------


@dataclass
class InterleavingPlan:
    """The split and one single-context plan per group."""

    pool: rank_stage.CandidatePool
    groups: list[list[str]]
    group_plans: list[rank_stage.RankPlan]
    seed: int

    @property
    def over_budget(self) -> list[rank_stage.RankPlan]:
        return [p for p in self.group_plans if p.over_budget]

    def describe(self) -> str:
        pool = self.pool
        lines = [
            f"Screened commits:        {pool.n_screened}",
            f"  disqualified:          {pool.n_disqualified}",
            f"  viable candidates:     {len(pool.candidates)}",
        ]
        if pool.n_dropped_by_cap:
            lines.append(
                f"  dropped by max_candidates: {pool.n_dropped_by_cap} (lowest-scoring)"
            )
        lines.append(
            f"Method:                  interleaving: {len(self.groups)} random groups "
            f"(seed {self.seed}), ranked separately, then interleaved"
        )
        for p in self.group_plans:
            if p.prompt_tokens is None:
                size = f"{len(p.prompt):,} chars (not counted: rank.max_prompt_tokens unset)"
            else:
                budget = f" of {p.budget:,}" if p.budget else ""
                flag = "  OVER BUDGET" if p.over_budget else ""
                size = f"{p.prompt_tokens:,} tokens (endpoint count){budget}{flag}"
            lines.append(f"  {p.label + ':':<22} {len(p.candidates):>5} candidates, {size}")
        return "\n".join(lines)


def plan(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    counter: Any = None,
) -> InterleavingPlan:
    """Split the candidates and compose and measure each group's prompt.

    Raises
    ------
    RankError
        If there are no summaries, or fewer candidates than groups.
    TokenCountError
        If a budget is set but a prompt cannot be counted.
    """
    settings = cfg.rank.interleaving
    pool = rank_stage.load_pool(cfg)
    if not pool.candidates:
        raise rank_stage._no_candidates_error(pool)
    by_sha = {c["sha"]: c for c in pool.candidates}
    try:
        groups = split_groups(list(by_sha), settings.groups, settings.seed)
    except ValueError as exc:
        raise rank_stage.RankError(f"cannot split candidates for interleaving: {exc}") from exc

    group_plans = [
        rank_stage.plan_group(
            cfg, pack, model, pool, [by_sha[s] for s in group],
            label=f"group {i + 1} of {len(groups)}", counter=counter,
        )
        for i, group in enumerate(groups)
    ]
    return InterleavingPlan(pool=pool, groups=groups, group_plans=group_plans, seed=settings.seed)


# -- merge -------------------------------------------------------------------


def merge_shortlists(
    group_shortlists: Sequence[dict[str, Any]],
    seed: int,
) -> list[dict[str, Any]]:
    """Interleave validated per-group shortlists into one ranked selection.

    Each merged entry keeps the agent's fields and gains ``group`` (one-based)
    and ``group_rank``; ``rank`` is renumbered densely over the merged order.
    """
    rankings = [
        sorted(s.get("selected") or [], key=lambda e: e["rank"]) for s in group_shortlists
    ]
    merged = []
    for position, (g, group_rank, entry) in enumerate(interleave(rankings, seed), start=1):
        merged.append({**entry, "rank": position, "group": g + 1, "group_rank": group_rank})
    return merged


def merge_rationales(
    rationales: Sequence[str], groups: Sequence[Sequence[str]], *, full: bool = False
) -> str:
    """Concatenate per-group rationales under a header explaining the method."""
    product = "ranking of every candidate" if full else "shortlist"
    parts = [
        "# Ranking rationale (interleaving)\n",
        f"The candidates were split at random into {len(groups)} groups "
        f"({', '.join(str(len(g)) for g in groups)} candidates). Each group was "
        "ranked by its own agent, which never saw the other groups; the final "
        f"{product} takes one entry from each group in turn. Ranks are therefore "
        "comparable only by round: a group's k-th pick is assumed to be as strong "
        "as any other group's k-th pick. The per-group reasoning follows.\n",
    ]
    for i, text in enumerate(rationales, start=1):
        body = text.strip() or "_This group's agent did not emit a rationale document._"
        parts.append(f"\n---\n\n## Group {i} of {len(rationales)}\n\n{body}\n")
    return "".join(parts)


# -- run ---------------------------------------------------------------------


def _load_group(cfg: MineConfig, index: int) -> rank_stage.GroupResult | None:
    """Return a completed group's saved result, or ``None`` if it has none."""
    directory = group_dir(cfg, index)
    meta_path = directory / GROUP_META
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text())
    return rank_stage.GroupResult(
        shortlist=json.loads((directory / GROUP_SHORTLIST).read_text()),
        rationale=(directory / GROUP_RATIONALE).read_text(),
        usage=meta.get("usage") or {},
        seconds=float(meta.get("seconds") or 0.0),
    )


def _save_group(cfg: MineConfig, index: int, result: rank_stage.GroupResult) -> None:
    directory = group_dir(cfg, index)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / GROUP_SHORTLIST).write_text(
        json.dumps(result.shortlist, indent=2, sort_keys=True) + "\n"
    )
    (directory / GROUP_RATIONALE).write_text(result.rationale)
    # Written last: its presence is what marks the group complete.
    tmp = directory / (GROUP_META + ".tmp")
    tmp.write_text(json.dumps(
        {"usage": result.usage, "seconds": result.seconds,
         "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        indent=2, sort_keys=True,
    ) + "\n")
    tmp.replace(directory / GROUP_META)


def _sum_usage(usages: Sequence[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + int(value)
    return totals


def _set_aside_state(cfg: MineConfig) -> None:
    directory = state_dir(cfg)
    if directory.exists():
        backup = directory.with_name(f"{STATE_DIRNAME}.bak-{time.strftime('%Y%m%dT%H%M%S')}")
        shutil.move(str(directory), str(backup))
        print(f"Moved previous interleaving state to {backup}")


def _prepare(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool,
    force: bool,
    progress: bool,
) -> InterleavingPlan:
    """Plan, validate, and record the split. Rank 0 only: it writes to disk."""
    work_plan = plan(cfg, pack, model)
    if progress:
        print(work_plan.describe())
        print()

    for group_plan in work_plan.over_budget:
        raise rank_stage.RankError(rank_stage._budget_error(group_plan))

    if force and not dry_run:
        _set_aside_state(cfg)
    rank_stage._require_no_shortlist(cfg, force)

    record = split_record(
        work_plan.groups, work_plan.seed, [c["sha"] for c in work_plan.pool.candidates],
        cfg.rank.output,
    )
    split_file = state_dir(cfg) / SPLIT_NAME
    if split_file.is_file():
        problems = split_mismatch(json.loads(split_file.read_text()), record)
        if problems:
            raise rank_stage.RankError(
                "the recorded interleaving split cannot be resumed:\n  - "
                + "\n  - ".join(problems)
                + "\nGroups already ranked were ranked over a different partition, so "
                "mixing them with new groups would not be a random split. Pass --force "
                "to set the recorded state aside and start over."
            )
    elif not dry_run:
        split_file.parent.mkdir(parents=True, exist_ok=True)
        split_file.write_text(json.dumps(record, indent=2) + "\n")
    return work_plan


def _group_worker(
    cfg: MineConfig,
    model: dict[str, str],
    *,
    dry_run: bool,
    progress: bool,
):
    """Build the per-group worker for :func:`parallel.run_round_robin`.

    An item is ``(index, RankPlan)``. The worker resumes a completed group from
    disk, or ranks it and saves the result before returning — so a group's
    work survives even if another rank later fails. Failures are returned (not
    raised), so one failed group never prevents the others from finishing.
    """

    def worker(item: tuple[int, rank_stage.RankPlan]) -> parallel.ItemResult:
        index, group_plan = item
        item_id = f"group-{index + 1}"
        done = _load_group(cfg, index)
        if done is not None:
            if progress:
                print(f"[{group_plan.label}] already ranked: "
                      f"{done.shortlist['n_selected']} entries (skipping)", flush=True)
            return parallel.ItemResult(item_id, "ok", data={"resumed": True})
        if progress:
            print(f"[{group_plan.label}] ranking {len(group_plan.candidates)} "
                  "candidates ...", flush=True)
        try:
            result = rank_stage.rank_group(
                cfg, model, group_plan,
                logs_dir=group_dir(cfg, index) / "logs",
                item_id=f"{cfg.run_id}:{item_id}",
                dry_run=dry_run,
            )
        except rank_stage.RankError as exc:
            if progress:
                print(f"[{group_plan.label}] FAILED: {exc}", flush=True)
            return parallel.ItemResult(item_id, "failed", detail=str(exc))
        if result is None:
            return parallel.ItemResult(item_id, "dry_run")
        _save_group(cfg, index, result)
        if progress:
            print(f"[{group_plan.label}] {result.shortlist['n_selected']} entries "
                  f"in {result.seconds:.0f}s", flush=True)
        return parallel.ItemResult(item_id, "ok")

    return worker


def run(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool = False,
    force: bool = False,
    progress: bool = True,
) -> dict[str, Any]:
    """Rank each group (resuming completed ones), interleave, and write the shortlist.

    **Collective** under ``execution.parallel: mpi``: every rank must call it.
    Rank 0 returns the shortlist; other ranks return ``{"is_primary": False}``.

    Raises
    ------
    RankError
        On rank 0, if a prompt exceeds the budget or cannot be measured, the
        recorded split does not match, or any group's agent fails (after every
        other group has finished). Completed groups are kept, so rerunning
        resumes at the failed ones. Setup errors are broadcast, so every rank
        exits rather than waiting forever for work.
    """
    try:
        world = parallel.mpi_world(cfg.execution.parallel)
    except parallel.ParallelError as exc:
        raise rank_stage.RankError(str(exc)) from exc
    speak = progress and world.is_primary

    # Only rank 0 plans and writes; the outcome (or the error that stopped it)
    # is broadcast so no rank is left waiting in a collective for work.
    work_plan: InterleavingPlan | None = None
    setup_error: str | None = None
    if world.is_primary:
        try:
            work_plan = _prepare(cfg, pack, model, dry_run=dry_run, force=force,
                                 progress=speak)
        except rank_stage.RankError as exc:
            setup_error = str(exc)
    setup_error = world.bcast(setup_error)
    if setup_error is not None:
        if world.is_primary:
            raise rank_stage.RankError(setup_error)
        return {"is_primary": False}

    items = (
        list(enumerate(work_plan.group_plans)) if work_plan is not None else None
    )
    if speak and world.size > 1:
        assignment = parallel.round_robin(len(items), world.size)
        print(f"Parallel: {world.size} MPI ranks, round-robin: " + "; ".join(
            f"rank {r} -> " + (", ".join(f"group {i + 1}" for i in idx) or "idle")
            for r, idx in enumerate(assignment)
        ))
        print()
    outcome = parallel.run_round_robin(
        items or [], _group_worker(cfg, model, dry_run=dry_run, progress=progress), world,
    )
    if not outcome.is_primary:
        return {"is_primary": False}
    assert work_plan is not None

    if any(r.status == "dry_run" for r in outcome.results):
        if speak:
            print("Dry run: no agent was executed.")
        return rank_stage._dry_run_result(cfg, work_plan.pool)
    failed = [r for r in outcome.results if not r.ok]
    if failed:
        done = len(outcome.results) - len(failed)
        raise rank_stage.RankError(
            f"{len(failed)} of {len(outcome.results)} group(s) failed; {done} "
            "completed and saved, so rerunning ranks only the failed group(s):\n  - "
            + "\n  - ".join(f"{r.item_id}: {r.detail.splitlines()[0] if r.detail else ''}"
                            for r in failed)
        )

    results = [_load_group(cfg, i) for i in range(len(work_plan.groups))]
    missing = [i + 1 for i, r in enumerate(results) if r is None]
    if missing:
        raise rank_stage.RankError(
            f"group(s) {missing} reported success but left no saved result in "
            f"{state_dir(cfg)}; is the run directory on a filesystem shared by every rank?"
        )
    return _finish(cfg, model, work_plan, results, progress=speak)


def _finish(
    cfg: MineConfig,
    model: dict[str, str],
    work_plan: InterleavingPlan,
    results: Sequence[rank_stage.GroupResult],
    *,
    progress: bool,
) -> dict[str, Any]:
    """Interleave the saved group results and write the final shortlist (rank 0)."""
    selected = merge_shortlists([r.shortlist for r in results], work_plan.seed)
    shortlist: dict[str, Any] = {
        "run_id": cfg.run_id,
        "selected": selected,
        "n_selected": len(selected),
        "n_candidates": len(work_plan.pool.candidates),
        "n_disqualified": work_plan.pool.n_disqualified,
        "ranked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_id": model.get("model", ""),
        "usage": _sum_usage([r.usage for r in results]),
        "method": {
            "name": "interleaving",
            "output": cfg.rank.output,
            "single_context": False,
            "groups": len(work_plan.groups),
            "seed": work_plan.seed,
            "group_sizes": [len(g) for g in work_plan.groups],
            "group_selected": [r.shortlist["n_selected"] for r in results],
            "prompt_tokens": [p.prompt_tokens for p in work_plan.group_plans],
            "note": (
                "Candidates were split at random into groups, each ranked by a "
                "separate agent that saw only its own group; the shortlist "
                "interleaves one pick per group per round. A group's k-th pick is "
                "assumed equivalent to any other group's k-th pick. No candidate was "
                "compared against one outside its group."
            ),
        },
    }
    warnings = [
        f"group {i + 1}: {w}"
        for i, r in enumerate(results)
        for w in r.shortlist.get("validation_warnings") or []
    ]
    if warnings:
        shortlist["validation_warnings"] = warnings
    if cfg.rank.output == "full":
        shortlist["complete"] = True

    path = rank_stage.write_outputs(
        cfg, shortlist,
        merge_rationales([r.rationale for r in results], work_plan.groups,
                         full=cfg.rank.output == "full"),
    )
    if progress:
        print()
        verb = "Ranked" if cfg.rank.output == "full" else "Selected"
        print(f"{verb} {len(selected)} of {len(work_plan.pool.candidates)} candidate(s) "
              f"across {len(results)} groups "
              f"({', '.join(str(r.shortlist['n_selected']) for r in results)}).")
        print(f"  shortlist: {path}")
        print(f"  rationale: {rank_stage.rationale_path(cfg)}")
    return shortlist
