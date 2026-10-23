"""RANK — collapse viable screening summaries into a ranked shortlist.

One decision-making agent reads **every** viable candidate in a single context and
selects the commits genuinely worth building into benchmarks. It may dispatch
research subagents to settle a specific uncertainty about a promising candidate, but
the selection itself is one judgement made with all candidates visible — which is
precisely the comparison the per-commit screening pass cannot make.

The only filtering done here in Python is dropping commits screening marked
``disqualified`` (they failed a rubric gate, and are unusable by construction). No
score threshold is applied: the scores travel along as *informational context*, and
the agent is explicitly told it may disagree with them.

.. _rank-limitations:

Known limitations — deliberate, not oversights
----------------------------------------------
**Single-context only.** This stage assumes every candidate fits in one model
context. There is no chunk-and-merge, no multi-round tournament, no hierarchical
reduction. That is a conscious scoping decision: those designs introduce real
complexity (how to compare candidates that never shared a context, how to merge
partial orderings fairly, how to keep a merge agent from simply deferring to
whichever chunk it saw last) and are not attempted here.

The assumption is therefore **checked rather than hoped for**. The candidates are
embedded in the prompt itself, not left in a file for the agent to page through
with a read tool (which adds a per-line overhead and a tool round-trip per 50 KB,
and invites skimming). Before any agent runs, the fully composed prompt — the
harness system prompt plus this stage's task prompt — is counted through the
endpoint's own token counter and compared against ``rank.max_prompt_tokens``;
exceeding it is a hard, actionable error. There is no estimate to fall back on:
if the count cannot be obtained, the stage refuses to start. (The count is only
as accurate as the endpoint's counter; a gateway whose counter disagrees with
its models' real tokenizer makes the budget a bound in *its* units.) Silently overflowing a context and receiving a shortlist quietly drawn
from the first 60% of candidates would be far worse than refusing to start,
because nothing in the output would reveal it had happened.

**No mid-stage resume.** One agent doing one long task either finishes or does not.
If it dies forty minutes in, rerunning starts over. Per-item checkpointing has no
meaning when there is one item. Bounding the cost of that is what
``execution.timeout_seconds`` is for. A partial-progress design is not possible
without the multi-agent machinery described above.

Both limitations are recorded in the run manifest when this stage runs, so a
shortlist is never mistaken for the product of a more sophisticated process than
actually produced it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import parallel, prompts as prompts_mod, ranking_check, sandbox as sandbox_mod
from ..config import MineConfig
from ..manifest import ATTEMPTS_NAME, record_attempt
from ..prompts import PromptPack
from . import screen as screen_stage

#: Filenames the ranking agent writes into its output directory.
SHORTLIST_FILENAME = "shortlist.json"
RATIONALE_FILENAME = "rationale.md"

#: Under ``rank.output: full``, the agent writes this JSON Lines file (one entry
#: per candidate, best first) instead of a shortlist; the stage converts it into
#: the same ``shortlist.json`` schema, with every candidate "selected".
RANKING_FILENAME = "ranking.jsonl"

#: Staged into the agent's workspace under ``rank.output: full``.
CANDIDATE_SHAS_FILENAME = "candidate_shas.txt"
CHECKER_FILENAME = "check_ranking.py"

#: The harness's own system prompt, which the harness prepends to every task
#: prompt. Counted with it, since both arrive in the same context.
HARNESS_SYSTEM_PROMPT = sandbox_mod.HARNESS_DIR / "system_prompt.md"

#: Anthropic's documented version header, required by ``count_tokens``.
ANTHROPIC_VERSION = "2023-06-01"

#: A gateway WAF may reject requests without a User-Agent with an opaque 403.
USER_AGENT = "abc-mine-rank/1.0"

#: Fields carried into the candidate payload. Everything else from a screening
#: summary (the full rationale paragraph, usage records, axis metadata) is dropped:
#: with hundreds of candidates sharing one context, every field costs budget in
#: proportion to the candidate count, so only what changes a decision is included.
CANDIDATE_FIELDS = (
    "sha",
    "subject",
    "benchmark_score",
    "one_line",
    "proposed_task",
    "proposed_verification",
    "est_difficulty",
    "task_type",
    "confidence",
    "build_attempted",
    "build_outcome",
)


class RankError(RuntimeError):
    """Raised when ranking cannot proceed (no candidates, budget exceeded, ...)."""


def shortlist_path(cfg: MineConfig) -> Path:
    """Return the durable shortlist path."""
    return cfg.rank_dir / SHORTLIST_FILENAME


def rationale_path(cfg: MineConfig) -> Path:
    """Return the durable rationale path."""
    return cfg.rank_dir / RATIONALE_FILENAME


def attempts_path(cfg: MineConfig) -> Path:
    """Return the rank stage's attempt log path."""
    return cfg.rank_dir / ATTEMPTS_NAME


def build_candidates(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the compact candidate payload from screening summaries.

    Drops disqualified commits, keeps a trimmed projection of the rest, and sorts by
    score descending purely as a reading order. Pure and side-effect free, so the
    budget check and the tests can exercise it without a container.
    """
    candidates: list[dict[str, Any]] = []
    for summary in summaries:
        if summary.get("disqualified"):
            continue
        candidate = {field: summary.get(field) for field in CANDIDATE_FIELDS}
        axes = summary.get("axes") or {}
        candidate["axes"] = {
            key: {"score": entry.get("score"), "why": entry.get("why")}
            for key, entry in axes.items()
            if isinstance(entry, dict)
        }
        candidates.append(candidate)

    candidates.sort(key=lambda c: c.get("benchmark_score") or 0, reverse=True)
    return candidates


def render_candidate(candidate: dict[str, Any]) -> str:
    """Render one candidate as a compact, self-delimiting text block.

    Carries exactly the projected fields of :func:`build_candidates`, but as
    labelled lines rather than indented JSON: the braces, quotes, and
    indentation of pretty-printed JSON cost about 15% more tokens for no gain in
    legibility, and with a thousand candidates in one context that margin is
    the difference between fitting and not.
    """

    def value(v: Any) -> str:
        if v is None or v == "":
            return "none"
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v) if v else "none"
        return str(v)

    lines = [
        f"### {candidate.get('sha')}",
        f"subject: {value(candidate.get('subject'))}",
        (
            f"benchmark_score: {value(candidate.get('benchmark_score'))} | "
            f"est_difficulty: {value(candidate.get('est_difficulty'))} | "
            f"task_type: {value(candidate.get('task_type'))} | "
            f"confidence: {value(candidate.get('confidence'))} | "
            f"build_attempted: {value(candidate.get('build_attempted'))} | "
            f"build_outcome: {value(candidate.get('build_outcome'))}"
        ),
        f"one_line: {value(candidate.get('one_line'))}",
        f"proposed_task: {value(candidate.get('proposed_task'))}",
        f"proposed_verification: {value(candidate.get('proposed_verification'))}",
    ]
    axes = candidate.get("axes") or {}
    if axes:
        lines.append("axes:")
        lines += [
            f"- {key} {value(entry.get('score'))}: {value(entry.get('why'))}"
            for key, entry in axes.items()
        ]
    return "\n".join(lines)


def render_candidates(candidates: list[dict[str, Any]]) -> str:
    """Render the whole candidate list for embedding in the prompt."""
    return "\n\n".join(render_candidate(c) for c in candidates)


class TokenCountError(RankError):
    """The prompt's token count could not be obtained. Never estimated instead."""


def count_tokens(text: str, model: dict[str, str], *, timeout: float = 600.0) -> int:
    """Return the endpoint's input-token count for ``text``.

    Uses Anthropic's ``/v1/messages/count_tokens``, which runs no inference.
    Transient failures (5xx, connection errors) are retried; anything else is a
    :class:`TokenCountError`. There is deliberately **no estimate fallback**:
    with a hard context limit, a wrong guess surfaces as a failed or truncated
    run far from its cause.
    """
    if model.get("api") != "anthropic_messages":
        raise TokenCountError(
            "prompt sizing needs LLM_API=anthropic_messages (the endpoint's "
            f"/v1/messages/count_tokens), but LLM_API is {model.get('api')!r}.\n"
            "Set rank.max_prompt_tokens: null to run without a size check."
        )
    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise TokenCountError("LLM_API_KEY is not set; cannot count prompt tokens")
    base = str(model["base_url"]).rstrip("/")
    url = f"{base}/messages/count_tokens" if base.endswith("/v1") else f"{base}/v1/messages/count_tokens"
    body = json.dumps(
        {"model": model["model"], "messages": [{"role": "user", "content": text}]}
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "user-agent": USER_AGENT,
        },
    )
    backoff = 2.0
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace").strip()
            if exc.code < 500 or attempt == 4:
                raise TokenCountError(f"count_tokens HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt == 4:
                raise TokenCountError(
                    f"count_tokens failed: {type(exc).__name__}: {exc}"
                ) from exc
        time.sleep(backoff)
        backoff *= 2
    tokens = payload.get("input_tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, int):
        raise TokenCountError(f"count_tokens returned no input_tokens: {str(payload)[:200]}")
    return tokens


def harness_prompt_text(task_prompt: str) -> str:
    """Return the text the harness actually sends: its system prompt + the task.

    Mirrors the composition in ``harness/run_benchmark.sh`` so the size check
    measures what the model receives, not just the part this stage wrote.
    """
    system = HARNESS_SYSTEM_PROMPT.read_text()
    return f"# System Instructions\n\n{system}\n---\n\n# Task\n\n{task_prompt}"


@dataclass
class RankPlan:
    """What a ranking invocation intends to do."""

    candidates: list[dict[str, Any]] = field(default_factory=list)
    n_screened: int = 0
    n_disqualified: int = 0
    n_dropped_by_cap: int = 0
    #: The fully composed task prompt, candidates embedded.
    prompt: str = ""
    #: Endpoint token count of the harness system prompt plus :attr:`prompt`,
    #: or ``None`` when no budget is configured (and so nothing was counted).
    prompt_tokens: int | None = None
    budget: int | None = None
    #: Group label under ``interleaving`` (e.g. ``"group 1 of 2"``); empty for a
    #: single-context plan.
    label: str = ""

    @property
    def over_budget(self) -> bool:
        """True if the measured prompt exceeds the configured token budget."""
        return (
            self.budget is not None
            and self.prompt_tokens is not None
            and self.prompt_tokens > self.budget
        )

    def describe(self) -> str:
        """Render a human-readable plan."""
        lines = [
            f"Screened commits:        {self.n_screened}",
            f"  disqualified:          {self.n_disqualified}",
            f"  viable candidates:     {len(self.candidates)}",
        ]
        if self.n_dropped_by_cap:
            lines.append(
                f"  dropped by max_candidates: {self.n_dropped_by_cap} "
                f"(lowest-scoring)"
            )
        if self.prompt_tokens is None:
            lines.append(
                f"Prompt:                  {len(self.prompt):,} chars "
                "(not counted: rank.max_prompt_tokens is unset)"
            )
        else:
            budget = f" of {self.budget:,} budget" if self.budget else ""
            headroom = (
                f"; {self.budget - self.prompt_tokens:,} left for the agent's work"
                if self.budget and not self.over_budget else ""
            )
            lines.append(
                f"Prompt:                  {self.prompt_tokens:,} tokens "
                f"(endpoint count){budget}{headroom}"
            )
        return "\n".join(lines)


@dataclass
class CandidatePool:
    """The viable candidates, after disqualification and the ``max_candidates`` cap."""

    candidates: list[dict[str, Any]]
    n_screened: int
    n_disqualified: int
    n_dropped_by_cap: int


def load_pool(cfg: MineConfig) -> CandidatePool:
    """Load summaries and build the candidate pool.

    Raises
    ------
    RankError
        If there are no summaries.
    """
    summaries = screen_stage.load_summaries(cfg)
    if not summaries:
        raise RankError(
            f"no screening summaries found in {cfg.summaries_dir}\n"
            "Run the screen stage first:  abc-mine screen --config <config>"
        )

    candidates = build_candidates(summaries)
    dropped = 0
    if cfg.rank.max_candidates is not None and len(candidates) > cfg.rank.max_candidates:
        dropped = len(candidates) - cfg.rank.max_candidates
        candidates = candidates[: cfg.rank.max_candidates]

    return CandidatePool(
        candidates=candidates,
        n_screened=len(summaries),
        n_disqualified=sum(1 for s in summaries if s.get("disqualified")),
        n_dropped_by_cap=dropped,
    )


def plan_group(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    pool: CandidatePool,
    candidates: list[dict[str, Any]],
    *,
    label: str = "",
    counter: Any = None,
) -> RankPlan:
    """Compose the ranking prompt for ``candidates`` and measure it.

    ``counter(text) -> int`` replaces the endpoint call (tests). Candidates are
    re-sorted by score as a reading order, whatever order they arrive in.

    Raises
    ------
    TokenCountError
        If a budget is set but the prompt cannot be counted.
    """
    ordered = sorted(candidates, key=lambda c: c.get("benchmark_score") or 0, reverse=True)
    work_plan = RankPlan(
        candidates=ordered,
        n_screened=pool.n_screened,
        n_disqualified=pool.n_disqualified,
        n_dropped_by_cap=pool.n_dropped_by_cap,
        budget=cfg.rank.max_prompt_tokens,
        label=label,
    )
    work_plan.prompt = _compose_prompt(
        cfg, pack, work_plan, n_total=len(pool.candidates)
    )
    if work_plan.budget is not None and ordered:
        count = counter or (lambda text: count_tokens(text, model))
        work_plan.prompt_tokens = count(harness_prompt_text(work_plan.prompt))
    return work_plan


def plan(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    counter: Any = None,
) -> RankPlan:
    """Compose the single-context ranking prompt and measure it against the budget.

    Raises
    ------
    RankError
        If there are no summaries.
    TokenCountError
        If a budget is set but the prompt cannot be counted.
    """
    pool = load_pool(cfg)
    return plan_group(cfg, pack, model, pool, pool.candidates, counter=counter)


def _budget_error(work_plan: RankPlan) -> str:
    """Compose the actionable message for an over-budget prompt.

    The message is long on purpose. This is the one place where the tool refuses a
    job outright for a reason rooted in a deliberate design limitation, so it owes
    the user an explanation of *why* it will not simply try, and a concrete menu of
    ways forward.
    """
    where = f" ({work_plan.label})" if work_plan.label else ""
    return (
        f"The ranking prompt{where} is too large for one model context.\n\n"
        f"  candidates:        {len(work_plan.candidates)}\n"
        f"  prompt tokens:     {work_plan.prompt_tokens:,} (endpoint count)\n"
        f"  configured budget: {work_plan.budget:,} (rank.max_prompt_tokens)\n\n"
        f"Every ranking agent must see all of its candidates in ONE context.\n"
        f"Rather than overflow a context and hand you a shortlist silently drawn\n"
        f"from only part of the field, the stage stops here.\n\n"
        f"Options, roughly in order of preference:\n\n"
        f"  1. Screen more selectively, so fewer commits reach this stage — e.g.\n"
        f"     selection.paths to scope to a subsystem, or\n"
        f"     selection.max_files_changed to drop sprawling commits.\n"
        f"  2. Set rank.max_candidates to keep only the top-scoring N. Blunt, but\n"
        f"     honest and visible: the drop is reported.\n"
        f"  3. Raise rank.max_prompt_tokens if your model's context genuinely\n"
        f"     accommodates this prompt. Leave headroom below the hard limit for\n"
        f"     the agent harness's own instructions and tool definitions, the\n"
        f"     agent's reasoning and tool calls, and the shortlist it writes.\n"
        f"  4. Use rank.method: interleaving, which splits the candidates into\n"
        f"     rank.interleaving.groups random groups, ranks each in its own\n"
        f"     context, and interleaves the results (or raise the group count if\n"
        f"     it is already in use). Read its assumption in the docs first."
    )


#: What the agent is told about the candidates it sees. Accuracy matters: an
#: agent told it holds the whole field while it holds a random half would
#: calibrate its bar against the wrong population.
SCOPE_ALL = "You are given **every** viable candidate at once."
SCOPE_GROUP = (
    "You are given a **random sample** of the viable candidates — {label} of an "
    "even random split of all {n_total} ({n_here} here). Every group is ranked "
    "separately, and the results are merged by taking one entry from each "
    "group in turn (your k-th with the other groups' k-th), so rank *this* "
    "group on its own merits: do not try to compensate for candidates you "
    "cannot see, and apply the same standard you would apply to the whole field."
)


def _candidate_scope(work_plan: RankPlan, n_total: int) -> str:
    if not work_plan.label:
        return SCOPE_ALL
    return SCOPE_GROUP.format(
        label=work_plan.label, n_total=n_total, n_here=len(work_plan.candidates)
    )


def _ranking_example(candidates: list[dict[str, Any]]) -> str:
    """Two illustrative ranking lines, using the real field set."""
    fields = {**{k: "..." for k in ranking_check.REQUIRED_FIELDS},
              **{k: None for k in ranking_check.OPTIONAL_FIELDS}}
    fields.update(task_type=["bug_fix"], est_difficulty="medium",
                  checked_with_subagent=False)
    shas = [c.get("sha") or "<full SHA>" for c in candidates[:2]] or ["<full SHA>"]
    return "\n".join(json.dumps({**fields, "sha": sha}) for sha in shas)


def _ranking_fields() -> str:
    lines = [f"- `{k}` (required) — {v}" for k, v in ranking_check.REQUIRED_FIELDS.items()]
    lines += [f"- `{k}` (optional) — {v}" for k, v in ranking_check.OPTIONAL_FIELDS.items()]
    return "\n".join(lines)


def _compose_prompt(
    cfg: MineConfig, pack: PromptPack, work_plan: RankPlan, *, n_total: int | None = None
) -> str:
    """Render the ranking prompt for ``cfg.rank.output``.

    ``n_total`` is the size of the whole pool, when ``work_plan`` is one group
    of it.
    """
    work = sandbox_mod.CONTAINER_WORK_ROOT
    out = f"{work}/{sandbox_mod.OUT_DIRNAME}"
    variables: dict[str, Any] = {
        "codebase_name": cfg.codebase.name,
        "toolchain_block": cfg.toolchain.render_prompt_block(cfg.codebase.source_mount),
        "run_id": cfg.run_id,
        "candidates_block": render_candidates(work_plan.candidates),
        "rationale_path": f"{out}/{RATIONALE_FILENAME}",
        "source_path": cfg.codebase.source_mount,
        "n_candidates": len(work_plan.candidates),
        "n_disqualified": work_plan.n_disqualified,
        "candidate_scope": _candidate_scope(
            work_plan, len(work_plan.candidates) if n_total is None else n_total
        ),
    }
    if cfg.rank.output == "full":
        task = "rank_full_task"
        variables.update(
            ranking_path=f"{out}/{RANKING_FILENAME}",
            candidate_shas_path=f"{work}/{CANDIDATE_SHAS_FILENAME}",
            checker_path=f"{work}/{CHECKER_FILENAME}",
            ranking_example=_ranking_example(work_plan.candidates),
            ranking_fields=_ranking_fields(),
        )
    else:
        task = "rank_task"
        variables["shortlist_path"] = f"{out}/{SHORTLIST_FILENAME}"
    return prompts_mod.compose("rank_system", task, pack=pack, variables=variables)


def _validate_ranking(text: str, work_plan: RankPlan) -> tuple[dict[str, Any] | None, str]:
    """Validate a full ranking. Returns ``(shortlist, error)``.

    Uses exactly the checks the agent was told to run (:mod:`ranking_check`),
    so the stage and the agent cannot disagree. Anything short of an exact
    permutation of the candidates is rejected whole — no repair.
    """
    entries, problems = ranking_check.parse_lines(text)
    problems += ranking_check.check(entries, [c["sha"] for c in work_plan.candidates])
    if problems:
        return None, (
            f"the ranking is not an exact ranking of all {len(work_plan.candidates)} "
            f"candidates ({len(problems)} problem(s)):\n" + ranking_check.summarize(problems)
        )
    by_sha = {c["sha"]: c for c in work_plan.candidates}
    selected = []
    for position, entry in enumerate(entries, start=1):
        entry = {k: v for k, v in entry.items() if k != "_line"}
        entry["sha"] = entry["sha"].strip()
        entry["rank"] = position
        # A null refinement means "keep the screening pass's version": resolve
        # it here, so every downstream reader (task ids, the build prompt) sees
        # one effective value rather than re-deriving the fallback.
        for key in ("one_line", "proposed_task", "proposed_verification"):
            if not entry.get(key):
                entry[key] = by_sha[entry["sha"]].get(key)
        selected.append(entry)
    return {
        "selected": selected,
        "n_selected": len(selected),
        "n_candidates": len(work_plan.candidates),
        "n_disqualified": work_plan.n_disqualified,
        "run_id": "",
        "complete": True,
    }, ""


def _validate_shortlist(
    raw: Any, work_plan: RankPlan
) -> tuple[dict[str, Any] | None, str]:
    """Validate the agent's shortlist. Returns ``(shortlist, error)``.

    Checks that selections reference real candidates and that ranks are sane. The
    unknown-SHA check matters: a hallucinated or mistyped SHA would fail much later,
    inside the build stage, as a confusing git error — far from its actual cause.
    """
    if not isinstance(raw, dict):
        return None, f"shortlist must be a JSON object, got {type(raw).__name__}"

    selected = raw.get("selected")
    if not isinstance(selected, list):
        return None, "shortlist is missing a 'selected' array"

    known = {c["sha"] for c in work_plan.candidates if c.get("sha")}
    cleaned: list[dict[str, Any]] = []
    problems: list[str] = []

    for index, entry in enumerate(selected):
        if not isinstance(entry, dict):
            problems.append(f"selected[{index}] is not an object")
            continue
        sha = str(entry.get("sha", "")).strip()
        if not sha:
            problems.append(f"selected[{index}] has no sha")
            continue
        if sha not in known:
            matches = [k for k in known if k.startswith(sha)]
            if len(matches) == 1:
                sha = matches[0]
            else:
                problems.append(
                    f"selected[{index}] sha {sha[:12]} is not among the candidates "
                    "that were offered"
                )
                continue
        entry = dict(entry)
        entry["sha"] = sha
        try:
            entry["rank"] = int(entry.get("rank", index + 1))
        except (TypeError, ValueError):
            entry["rank"] = index + 1
        cleaned.append(entry)

    if problems and not cleaned:
        return None, "; ".join(problems)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in sorted(cleaned, key=lambda e: e["rank"]):
        if entry["sha"] in seen:
            continue
        seen.add(entry["sha"])
        deduped.append(entry)

    # Renumber so ranks are always dense and 1-based, whatever the agent emitted.
    for position, entry in enumerate(deduped, start=1):
        entry["rank"] = position

    shortlist = dict(raw)
    shortlist["selected"] = deduped
    shortlist["n_selected"] = len(deduped)
    shortlist["n_candidates"] = len(work_plan.candidates)
    shortlist["n_disqualified"] = work_plan.n_disqualified
    shortlist["run_id"] = shortlist.get("run_id") or ""
    if problems:
        shortlist["validation_warnings"] = problems
    return shortlist, ""


@dataclass
class GroupResult:
    """A validated shortlist from one ranking agent, plus its provenance."""

    shortlist: dict[str, Any]
    rationale: str
    usage: dict[str, Any]
    seconds: float


def _no_candidates_error(pool: CandidatePool) -> RankError:
    return RankError(
        f"every screened commit was disqualified "
        f"({pool.n_disqualified} of {pool.n_screened}), so there is "
        f"nothing to rank.\n"
        "This usually means the codebase's changes are not verifiable in the "
        "way the rubric requires — check that the toolchain can actually run a "
        "targeted test (`abc-mine preflight`), since commits that cannot "
        "be verified are gated out during screening."
    )


def _require_no_shortlist(cfg: MineConfig, force: bool) -> None:
    existing = shortlist_path(cfg)
    if existing.is_file() and not force:
        raise RankError(
            f"a shortlist already exists: {existing}\n"
            "Ranking is not re-run implicitly. Pass --force to discard the existing "
            "shortlist and rank again (the current one is backed up first)."
        )


def rank_group(
    cfg: MineConfig,
    model: dict[str, str],
    work_plan: RankPlan,
    *,
    logs_dir: Path,
    item_id: str,
    dry_run: bool = False,
) -> GroupResult | None:
    """Run one ranking agent over ``work_plan`` and return its validated result.

    The unit of work shared by every method: one agent, one context, one prompt
    (the one ``work_plan`` measured, byte for byte). Returns ``None`` under a
    dry run.

    Raises
    ------
    RankError
        If the agent fails or produces no usable shortlist. The attempt is
        recorded under ``item_id`` and the transcript is kept in ``logs_dir``.
    """
    automaton_cfg = sandbox_mod.build_automaton_config(model)
    agent = sandbox_mod.make_agent(automaton_cfg)
    harness = sandbox_mod.make_harness()
    sandbox = sandbox_mod.make_sandbox(cfg, dry_run=dry_run)
    attempts = attempts_path(cfg)
    started = time.monotonic()

    with sandbox_mod.scratch_dir(cfg, f"lbm-rank-{cfg.run_id}-") as scratch:
        work = scratch / "work"
        run_dir = scratch / "run"
        work.mkdir(parents=True, exist_ok=True)

        # A read-only worktree at HEAD, so the agent (and any research subagent)
        # can inspect real code. Read-only in intent: nothing here is shipped.
        source_name = Path(cfg.codebase.source_mount).name
        from .. import repo as repo_mod

        full = cfg.rank.output == "full"
        if full:
            # The exact candidate set and the checker the prompt tells the agent
            # to run: the same module this stage validates with.
            (work / CANDIDATE_SHAS_FILENAME).write_text(
                "".join(f"{c['sha']}\n" for c in work_plan.candidates)
            )
            shutil.copy2(ranking_check.__file__, work / CHECKER_FILENAME)

        with repo_mod.worktree(cfg.codebase.repo, "HEAD", work / source_name):
            run_result = sandbox_mod.run_agent(
                cfg,
                sandbox=sandbox,
                harness=harness,
                agent=agent,
                work_dir=work,
                run_dir=run_dir,
                prompt_text=work_plan.prompt,
            )

        if run_result.dry_run:
            return None

        output_name = RANKING_FILENAME if full else SHORTLIST_FILENAME
        output_file = run_result.out_path(output_name)
        rationale_file = run_result.out_path(RATIONALE_FILENAME)

        failure: str | None = None
        shortlist: dict[str, Any] | None = None

        if not run_result.ok:
            failure = run_result.detail or "agent execution failed"
        elif not output_file.is_file():
            failure = f"agent finished successfully but wrote no {output_name}"
        elif full:
            shortlist, error = _validate_ranking(
                output_file.read_text(errors="replace"), work_plan
            )
            if shortlist is None:
                failure = f"{output_name} was rejected: {error}"
        else:
            try:
                raw = json.loads(output_file.read_text())
            except json.JSONDecodeError as exc:
                failure = f"{output_name} is not valid JSON: {exc}"
            else:
                shortlist, error = _validate_shortlist(raw, work_plan)
                if shortlist is None:
                    failure = f"{output_name} is unusable: {error}"

        # Always keep the ranking transcript when something went wrong: a ranking
        # agent runs once, expensively, so a lost transcript means re-spending the
        # whole cost to learn what happened.
        kept = sandbox_mod.preserve_logs(
            run_result.run_dir,
            logs_dir,
            keep_logs="all" if failure else cfg.execution.keep_logs,
            failed=bool(failure),
        )
        # A rejected output is kept too: the agent's reasoning is in it, and
        # seeing exactly what was wrong is what makes the next attempt succeed.
        if failure and kept is not None and output_file.is_file():
            shutil.copy2(output_file, kept / f"rejected-{output_name}")

        if failure is not None or shortlist is None:
            record_attempt(
                attempts, item_id, "failed", detail=failure or "unknown",
                model_id=model.get("model", ""),
            )
            where = f" ({work_plan.label})" if work_plan.label else ""
            raise RankError(f"ranking failed{where}: {failure}")

        seconds = round(time.monotonic() - started, 1)
        record_attempt(
            attempts, item_id, "ok",
            model_id=model.get("model", ""),
            n_selected=shortlist["n_selected"],
            n_candidates=len(work_plan.candidates),
            seconds=seconds,
        )
        return GroupResult(
            shortlist=shortlist,
            rationale=rationale_file.read_text() if rationale_file.is_file() else "",
            usage=sandbox_mod.read_usage(run_result.run_dir),
            seconds=seconds,
        )


def write_outputs(cfg: MineConfig, shortlist: dict[str, Any], rationale: str) -> Path:
    """Persist the shortlist (atomically, backing up any previous one) and rationale."""
    existing = shortlist_path(cfg)
    cfg.rank_dir.mkdir(parents=True, exist_ok=True)
    if existing.is_file():
        shutil.copy2(existing, existing.with_suffix(".json.bak"))
    tmp = existing.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(shortlist, indent=2, sort_keys=True) + "\n")
    tmp.replace(existing)

    rationale_path(cfg).write_text(
        rationale
        or (
            "# Ranking rationale\n\n"
            "The ranking agent did not emit a rationale document. "
            f"{shortlist['n_selected']} candidate(s) were selected; see "
            f"`{SHORTLIST_FILENAME}` for the per-candidate reasoning fields.\n"
        )
    )
    return existing


def _dry_run_result(cfg: MineConfig, pool: CandidatePool) -> dict[str, Any]:
    return {
        "run_id": cfg.run_id,
        "selected": [],
        "n_candidates": len(pool.candidates),
        "n_disqualified": pool.n_disqualified,
        "n_selected": 0,
        "dry_run": True,
    }


def run(
    cfg: MineConfig,
    pack: PromptPack,
    model: dict[str, str],
    *,
    dry_run: bool = False,
    force: bool = False,
    progress: bool = True,
) -> dict[str, Any]:
    """Execute the rank stage with the configured method; return the shortlist.

    Raises
    ------
    RankError
        If there are no candidates, a prompt exceeds the budget or cannot be
        measured, or an agent fails to produce a usable shortlist.
    """
    if cfg.rank.method == "interleaving":
        from . import rank_interleaving

        return rank_interleaving.run(
            cfg, pack, model, dry_run=dry_run, force=force, progress=progress
        )

    from .. import parallel

    if parallel.launcher_size() > 1:
        # One agent, one context: there is nothing to share out, and N copies
        # of this function would each run the whole ranking, racing on the same
        # shortlist. Refuse on every rank (the CLI reports from rank 0 only).
        raise RankError(
            f"rank.method: single_context runs one agent, but this process was "
            f"launched as one of {parallel.launcher_size()} tasks. Launch it once "
            "(without srun -n N), or use rank.method: interleaving to rank groups "
            "in parallel."
        )

    work_plan = plan(cfg, pack, model)
    pool = CandidatePool(
        work_plan.candidates, work_plan.n_screened,
        work_plan.n_disqualified, work_plan.n_dropped_by_cap,
    )

    if progress:
        print(work_plan.describe())
        print()

    if not work_plan.candidates:
        raise _no_candidates_error(pool)
    if work_plan.over_budget:
        raise RankError(_budget_error(work_plan))
    _require_no_shortlist(cfg, force)

    result = rank_group(
        cfg, model, work_plan,
        logs_dir=cfg.rank_dir / "logs", item_id=cfg.run_id, dry_run=dry_run,
    )
    if result is None:
        if progress:
            print("Dry run: no agent was executed.")
        return _dry_run_result(cfg, pool)

    shortlist = result.shortlist
    shortlist["run_id"] = cfg.run_id
    shortlist["ranked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    shortlist["model_id"] = model.get("model", "")
    shortlist["usage"] = result.usage
    # Recorded so a shortlist always carries evidence of the process that made
    # it, and cannot later be mistaken for a multi-agent result.
    shortlist["method"] = {
        "name": "single_context",
        "output": cfg.rank.output,
        "single_context": True,
        "rounds": 1,
        "prompt_tokens": work_plan.prompt_tokens,
        "note": (
            ("Every candidate ranked" if cfg.rank.output == "full" else "Selected")
            + " by one agent holding all candidates in a single context. "
            "No chunk-and-merge or multi-round ranking was performed."
        ),
    }
    path = write_outputs(cfg, shortlist, result.rationale)

    if progress:
        print()
        verb = "Ranked" if cfg.rank.output == "full" else "Selected"
        print(
            f"{verb} {shortlist['n_selected']} of {len(work_plan.candidates)} "
            f"candidate(s)."
        )
        print(f"  shortlist: {path}")
        print(f"  rationale: {rationale_path(cfg)}")

    return shortlist


def load_shortlist(cfg: MineConfig) -> dict[str, Any]:
    """Load this run's shortlist.

    Raises
    ------
    RankError
        If it is missing or unparseable.
    """
    path = shortlist_path(cfg)
    if not path.is_file():
        raise RankError(
            f"no shortlist at {path}\n"
            "Run the rank stage first:  abc-mine rank --config <config>"
        )
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RankError(f"shortlist is not valid JSON: {path} ({exc})") from exc
