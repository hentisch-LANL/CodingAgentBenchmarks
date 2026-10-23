"""The scoring rubric: a single source of truth for axes, weights, and gates.

Why this module exists
----------------------
In the original Truchas pipeline the rubric was written down **four** times: as
``_AXIS_WEIGHTS`` in the screen stage, as a markdown table in the screening
system prompt, as a JSON schema in the ranking prompt, and again in the scope
document. Changing a weight meant editing four files and hoping they agreed.

Here the rubric is defined **once** — as data (:data:`DEFAULT_AXES`, overridable
from the run config) — and everything else is *derived* from it:

- :func:`score` computes the deterministic weighted total and gate outcome,
- :func:`render_axis_table` / :func:`render_axis_guidance` generate the markdown
  the prompts embed,
- :func:`render_axes_schema` generates the JSON schema fragment the agent is
  told to emit.

So re-weighting the rubric is a single edit in one config block, and the prompts
cannot silently disagree with the scorer.

Scoring contract
----------------
Each axis is scored **0-4** by the model. The *model never computes the total*:
it supplies per-axis scores plus one-line justifications, and this module
computes ``benchmark_score`` and ``disqualified`` in Python. That keeps scoring
auditable and lets a run be re-weighted after the fact without re-invoking any
agent.

A **gate** axis scoring 0 disqualifies the commit outright (score forced to 0),
because a benchmark you cannot verify — or cannot extract a coherent task from —
is worthless regardless of how interesting the change is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: Maximum per-axis score. Axis scores are integers in ``[0, MAX_AXIS_SCORE]``.
MAX_AXIS_SCORE = 4


@dataclass(frozen=True)
class Axis:
    """One rubric axis.

    Attributes
    ----------
    key : str
        Machine-readable axis name; the key the model emits under ``axes``.
    title : str
        Human-readable axis name used in rendered prompt tables.
    weight : float
        Relative weight. Weights across all axes should sum to 1.0 (validated by
        :func:`validate_axes`).
    gate : bool
        If True, a score of 0 on this axis disqualifies the commit entirely.
    guidance : str
        Prose scoring guidance rendered into the screening prompt. Should explain
        what a high score and a low score look like, and — for gate axes — what
        a 0 means.
    """

    key: str
    title: str
    weight: float
    gate: bool
    guidance: str


#: The default six-axis rubric.
#:
#: Design principle: **verifiability dominates.** A change whose correctness
#: cannot be checked deterministically is not a candidate, no matter how
#: interesting it is. Verifiability carries the largest weight *and* is a gate.
#:
#: This is codebase-agnostic on purpose. Anything specific to a particular
#: repository (what its tests look like, how expensive its build is) belongs in
#: that codebase's prompt-slot pack, not here.
DEFAULT_AXES: tuple[Axis, ...] = (
    Axis(
        key="verifiability",
        title="Verifiability",
        weight=0.30,
        gate=True,
        guidance=(
            "Can success be checked deterministically? Is there — or could there "
            "plausibly be — a targeted compile/build check or a specific test that "
            "passes only when the change is correctly reproduced? Score 4 when a "
            "clear, fast, deterministic check exists. Score low for outcomes that "
            "are non-deterministic, tolerance-only, or require human judgement "
            "(\"eyeball the plot\"). **Score 0 only if no deterministic check is "
            "conceivable at all — this disqualifies the commit.**"
        ),
    ),
    Axis(
        key="task_well_defined",
        title="Task well-definedness",
        weight=0.20,
        gate=True,
        guidance=(
            "Is there a single, self-contained, clearly-scoped task hidden here — a "
            "bug with a reproducer, a feature with a clear interface, a refactor "
            "with invariants? Score 4 when a clean, isolated task is obviously "
            "extractable. Score low for sprawling, multi-concern, or purely "
            "cosmetic commits. **Score 0 only if no coherent task can be extracted "
            "at all — this disqualifies the commit.**"
        ),
    ),
    Axis(
        key="starting_state",
        title="Starting-state cleanliness",
        weight=0.15,
        gate=False,
        guidance=(
            "How cleanly can a workspace be constructed that is the repository "
            "*without* this change? A commit that reverts cleanly to a sensible "
            "pre-change state scores high; one entangled with unrelated churn, "
            "mass renames, vendored dependencies, or generated files scores low."
        ),
    ),
    Axis(
        key="self_contained",
        title="Self-containedness / scope",
        weight=0.15,
        gate=False,
        guidance=(
            "Is the change localized — few files, bounded blast radius — so an "
            "agent could plausibly solve it in one session? Huge diffs, "
            "cross-cutting API changes, and changes spanning many subsystems score "
            "low. Very trivial one-liners also score low: too easy to be a useful "
            "benchmark."
        ),
    ),
    Axis(
        key="difficulty_fit",
        title="Difficulty appropriateness",
        weight=0.10,
        gate=False,
        guidance=(
            "Is the *reasoning* required a good fit for an agentic-coding benchmark "
            "— non-trivial but tractable? Score the sweet spot high. Score trivial "
            "typo/formatting fixes low, and score research-grade or "
            "domain-expert-only changes low as well."
        ),
    ),
    Axis(
        key="verify_cost",
        title="Cost to verify",
        weight=0.10,
        gate=False,
        guidance=(
            "How expensive is verification likely to be, relative to this "
            "codebase's build and test cost as stated in the task prompt? A change "
            "checkable by compiling one module or running one fast test scores "
            "high; one requiring a very long or resource-heavy run scores low. "
            "Judge against the *stated* costs — do not assume a build is expensive "
            "if the prompt says it is cheap."
        ),
    ),
)


#: Task-type tags recorded by the screening agent. They inform ranking and
#: construction (and drive shortlist diversity) but do not contribute to the
#: score.
DEFAULT_TASK_TYPES: tuple[str, ...] = (
    "bug_fix",
    "feature_add",
    "refactor",
    "perf_optimization",
    "numerical_method",
    "io_format",
    "build_system",
    "test_only",
    "api_change",
    "docs_or_cosmetic",
)

#: Valid ``est_difficulty`` labels.
DIFFICULTY_LABELS: tuple[str, ...] = ("trivial", "easy", "medium", "hard", "research")

#: Valid ``confidence`` labels.
CONFIDENCE_LABELS: tuple[str, ...] = ("low", "medium", "high")


@dataclass(frozen=True)
class Rubric:
    """A validated rubric: an ordered set of axes plus the tag vocabulary."""

    axes: tuple[Axis, ...] = DEFAULT_AXES
    task_types: tuple[str, ...] = DEFAULT_TASK_TYPES

    @property
    def keys(self) -> tuple[str, ...]:
        """Axis keys, in rubric order."""
        return tuple(a.key for a in self.axes)

    @property
    def gate_keys(self) -> tuple[str, ...]:
        """Keys of the gate axes."""
        return tuple(a.key for a in self.axes if a.gate)

    def axis(self, key: str) -> Axis:
        """Return the axis with ``key``, or raise :class:`KeyError`."""
        for a in self.axes:
            if a.key == key:
                return a
        raise KeyError(f"no such rubric axis: {key!r}")

    # -- scoring -----------------------------------------------------------

    def score(self, axes: Mapping[str, Any] | None) -> tuple[int, bool]:
        """Compute ``(benchmark_score, disqualified)`` from per-axis scores.

        See :func:`score` for the full contract; this is the bound form.
        """
        return score(axes, rubric=self)

    def coerce_axes(self, axes: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        """Normalize model-supplied axis entries into the canonical shape.

        Accepts either ``{"key": 3}`` or ``{"key": {"score": 3, "why": "..."}}``
        and returns ``{"key": {"score": int, "gate": bool, "why": str}}`` for
        every axis in the rubric. Missing or unparseable scores become 0 — which,
        on a gate axis, disqualifies. That is deliberate: a screening agent that
        fails to answer the gate questions has not produced a usable verdict, and
        silently treating that as a passing score would let junk through.
        """
        src: Mapping[str, Any] = axes if isinstance(axes, Mapping) else {}
        out: dict[str, dict[str, Any]] = {}
        for ax in self.axes:
            entry = src.get(ax.key, {})
            if isinstance(entry, Mapping):
                raw_score = entry.get("score")
                why = str(entry.get("why", ""))
            else:
                raw_score = entry
                why = ""
            out[ax.key] = {
                "score": _clamp_score(raw_score),
                "gate": ax.gate,
                "why": why,
            }
        return out

    # -- prompt rendering --------------------------------------------------

    def render_axis_table(self) -> str:
        """Render the axis/weight/gate summary table as markdown."""
        return render_axis_table(self)

    def render_axis_guidance(self) -> str:
        """Render the per-axis scoring guidance as markdown."""
        return render_axis_guidance(self)

    def render_axes_schema(self, indent: int = 4) -> str:
        """Render the ``axes`` JSON-schema fragment the agent must emit."""
        return render_axes_schema(self, indent=indent)


#: The default rubric instance.
DEFAULT_RUBRIC = Rubric()


def _clamp_score(raw: Any) -> int:
    """Coerce ``raw`` to an int in ``[0, MAX_AXIS_SCORE]`` (unparseable -> 0)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_AXIS_SCORE, value))


def validate_axes(axes: Iterable[Axis], *, tolerance: float = 1e-6) -> tuple[Axis, ...]:
    """Validate a set of axes and return it as a tuple.

    Checks that the set is non-empty, has unique keys, has non-negative weights
    summing to 1.0 (within ``tolerance``), and declares at least one gate.

    The weight-sum check matters because :func:`score` normalizes by
    ``MAX_AXIS_SCORE`` alone; if the weights did not sum to 1.0 the resulting
    "0-100" score would not actually span 0-100, and scores would not be
    comparable across runs.

    Raises
    ------
    ValueError
        If any check fails.
    """
    axes = tuple(axes)
    if not axes:
        raise ValueError("rubric must define at least one axis")

    keys = [a.key for a in axes]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise ValueError(f"duplicate rubric axis keys: {sorted(duplicates)}")

    for a in axes:
        if a.weight < 0:
            raise ValueError(f"rubric axis {a.key!r} has a negative weight: {a.weight}")

    total = sum(a.weight for a in axes)
    if abs(total - 1.0) > tolerance:
        raise ValueError(
            f"rubric axis weights must sum to 1.0, got {total:.6f} "
            f"({', '.join(f'{a.key}={a.weight}' for a in axes)})"
        )

    if not any(a.gate for a in axes):
        raise ValueError(
            "rubric must declare at least one gate axis; without a gate, commits "
            "that are impossible to verify would still receive a nonzero score"
        )
    return axes


def score(
    axes: Mapping[str, Any] | None, *, rubric: Rubric = DEFAULT_RUBRIC
) -> tuple[int, bool]:
    """Compute ``(benchmark_score, disqualified)`` deterministically.

    Rules:

    - Every axis score is clamped to ``[0, MAX_AXIS_SCORE]``; missing or
      unparseable entries are treated as 0.
    - If **any gate axis** scores 0, the commit is disqualified:
      ``(0, True)``.
    - Otherwise ``benchmark_score = round(sum(w_i * s_i) / MAX_AXIS_SCORE * 100)``,
      clamped to ``[0, 100]``.

    Parameters
    ----------
    axes : mapping or None
        Per-axis entries, each either an int score or a mapping with a
        ``"score"`` key.
    rubric : Rubric, optional
        The rubric to score against (defaults to :data:`DEFAULT_RUBRIC`).

    Returns
    -------
    tuple[int, bool]
        ``(benchmark_score, disqualified)``.
    """
    src: Mapping[str, Any] = axes if isinstance(axes, Mapping) else {}
    scores: dict[str, int] = {}
    for ax in rubric.axes:
        entry = src.get(ax.key)
        raw = entry.get("score") if isinstance(entry, Mapping) else entry
        scores[ax.key] = _clamp_score(raw)

    for ax in rubric.axes:
        if ax.gate and scores[ax.key] == 0:
            return 0, True

    weighted = sum(ax.weight * scores[ax.key] for ax in rubric.axes)
    total = round(weighted / MAX_AXIS_SCORE * 100)
    return max(0, min(100, total)), False


# -- prompt rendering helpers ------------------------------------------------


def render_axis_table(rubric: Rubric = DEFAULT_RUBRIC) -> str:
    """Render the axis/weight/gate table as a markdown table.

    Embedded in the screening prompt so the model sees exactly the weights the
    Python scorer will apply.
    """
    lines = [
        "| # | Axis | Weight | Gate? |",
        "|---|------|--------|-------|",
    ]
    for i, ax in enumerate(rubric.axes, start=1):
        gate = "**yes**" if ax.gate else "no"
        lines.append(f"| {i} | {ax.title} | {ax.weight:.2f} | {gate} |")
    return "\n".join(lines)


def render_axis_guidance(rubric: Rubric = DEFAULT_RUBRIC) -> str:
    """Render per-axis scoring guidance as a markdown section body."""
    blocks: list[str] = []
    for i, ax in enumerate(rubric.axes, start=1):
        gate = "gate, " if ax.gate else ""
        blocks.append(f"**{i}. {ax.title}** ({gate}weight {ax.weight:.2f}) — {ax.guidance}")
    return "\n\n".join(blocks)


def render_axes_schema(rubric: Rubric = DEFAULT_RUBRIC, indent: int = 4) -> str:
    """Render the ``axes`` object of the required JSON output schema.

    Produces lines like::

        "verifiability":     {"score": 0-4, "why": "one-line justification"},

    aligned for readability. The rendered fragment is inserted into the screening
    task prompt, guaranteeing the advertised schema matches the rubric.
    """
    pad = " " * indent
    width = max(len(ax.key) for ax in rubric.axes) + 3  # quotes + colon
    lines: list[str] = []
    for i, ax in enumerate(rubric.axes):
        name = f'"{ax.key}":'
        comma = "" if i == len(rubric.axes) - 1 else ","
        lines.append(
            f'{pad}{name:<{width}} {{"score": 0-{MAX_AXIS_SCORE}, '
            f'"why": "one-line justification"}}{comma}'
        )
    return "\n".join(lines)


def rubric_from_config(spec: Mapping[str, Any] | None) -> Rubric:
    """Build a :class:`Rubric` from a config mapping, falling back to defaults.

    The config may override axis ``weight``, ``gate``, ``title``, and
    ``guidance`` by axis key, and may replace the ``task_types`` vocabulary. It
    may **not** invent new axes silently: an unknown key raises, because a typo'd
    axis name would otherwise be scored as a missing axis (0) and quietly
    disqualify every commit.

    Example config fragment::

        rubric:
          axes:
            verify_cost:
              weight: 0.05
            verifiability:
              weight: 0.35
          task_types: [bug_fix, feature_add, refactor]

    Raises
    ------
    ValueError
        If an unknown axis key is given, or the resulting axes fail
        :func:`validate_axes`.
    """
    if not spec:
        return DEFAULT_RUBRIC

    overrides = spec.get("axes") or {}
    if not isinstance(overrides, Mapping):
        raise ValueError("rubric.axes must be a mapping of axis-key -> overrides")

    known = {a.key for a in DEFAULT_AXES}
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(
            f"unknown rubric axis key(s): {sorted(unknown)}. "
            f"Known axes: {sorted(known)}. Overriding axes is supported; "
            "defining new ones is not."
        )

    axes: list[Axis] = []
    for ax in DEFAULT_AXES:
        ov = overrides.get(ax.key) or {}
        if not isinstance(ov, Mapping):
            raise ValueError(f"rubric.axes.{ax.key} must be a mapping")
        axes.append(
            Axis(
                key=ax.key,
                title=str(ov.get("title", ax.title)),
                weight=float(ov.get("weight", ax.weight)),
                gate=bool(ov.get("gate", ax.gate)),
                guidance=str(ov.get("guidance", ax.guidance)),
            )
        )

    task_types = spec.get("task_types")
    if task_types is None:
        tags = DEFAULT_TASK_TYPES
    else:
        if not isinstance(task_types, (list, tuple)) or not task_types:
            raise ValueError("rubric.task_types must be a non-empty list of strings")
        tags = tuple(str(t) for t in task_types)

    return Rubric(axes=validate_axes(axes), task_types=tags)
