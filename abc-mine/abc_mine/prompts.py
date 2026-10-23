"""Prompt composition: generic templates, per-codebase slots, and overrides.

abc-mine ships **generic** prompt templates (``abc_mine/templates/``)
that know nothing about any particular codebase. There are two, deliberately
distinct, mechanisms for adapting them to a specific repository.

.. _prompt-layering:

The two layers
--------------
A *prompt pack* is a directory the user points at with ``prompts.pack``:

.. code-block:: text

    my-codebase-prompts/
      slots/                     # (1) ADDITIVE context — the normal path
        codebase_overview.md
        build_test_guidance.md
        verification_guidance.md
        screening_notes.md
        ranking_notes.md
        construction_notes.md
        exclusions.md
      overrides/                 # (2) FULL REPLACEMENT — advanced, opt-in
        screen_system.md
        ...

**(1) Slots — additive, the expected path.** Each file fills one named slot in
the shipped templates. A slot renders as a *whole titled section that disappears
entirely when the file is absent*, so the surrounding prose reads correctly
whether or not the user supplied anything. Writing a slot pack requires no
knowledge of the base templates. Every slot is optional; the tool works with an
empty pack, or no pack at all.

**(2) Overrides — full replacement, deliberately gated.** Occasionally a user
needs to rewrite a base template outright rather than append to it. That is
supported, but it is intentionally hard to do by accident, because a silently
overridden system prompt is a debugging nightmare. Three independent things must
line up:

1. the file must live in ``overrides/`` (not ``slots/``),
2. it must be named *exactly* after a known base template (unknown names are a
   hard error, never a silent no-op),
3. the run config must set ``prompts.allow_template_overrides: true``.

If ``overrides/`` contains files but the flag is not set, loading **fails
loudly** rather than ignoring them — the one thing worse than an accidental
override is an accidentally-ignored one. Active overrides are recorded in the run
manifest with their SHA-256 and announced in a banner at every stage start, so a
run using them can never be mistaken for a stock run.

Overrides still receive normal placeholder substitution (including slots), and
are validated: a template that drops a *mechanically required* placeholder — an
output path the stage depends on — is rejected, since the stage could not
function. Dropping a merely advisory placeholder (e.g. the rubric table) warns
instead. That severity split is deliberate: mechanical breakage is an error,
prompt-quality regression is the user's call.

Placeholder syntax
------------------
All substitution uses ``@@name@@``. Unlike ``{name}``, this cannot collide with
the JSON braces that appear throughout these prompts (the original Truchas
pipeline needed a single-pass regex hack to work around exactly that), so
injected JSON blocks are never re-scanned or mangled.

Slots are referenced as ``@@slot:codebase_overview@@``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

#: Directory holding the shipped generic templates.
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

#: Subdirectory of a prompt pack holding additive slot files.
SLOTS_SUBDIR = "slots"

#: Subdirectory of a prompt pack holding full template overrides.
OVERRIDES_SUBDIR = "overrides"

#: File extension for slot and override files.
PROMPT_SUFFIX = ".md"

#: Filenames ignored inside a prompt-pack directory. ``README.md`` is written by
#: :func:`scaffold` itself to explain the override mechanism, and editors and
#: filesystems leave the rest behind. These are skipped rather than rejected: the
#: strict unknown-filename check exists to catch a *misnamed override*, and a file
#: called ``README.md`` is plainly not an attempt to override ``screen_system``.
#: Anything else unrecognized is still an error.
IGNORED_PACK_FILES = frozenset(
    {"README.md", "readme.md", ".gitignore", ".gitkeep", ".DS_Store"}
)


@dataclass(frozen=True)
class Slot:
    """One named, optional, per-codebase prompt slot.

    Attributes
    ----------
    key : str
        Slot name; the file stem under ``slots/`` and the ``@@slot:<key>@@``
        placeholder token.
    heading : str
        Markdown heading emitted above the user's content when the slot is
        filled. Omitted entirely when the slot is empty.
    purpose : str
        One-line description shown by ``abc-mine prompts slots``, and used
        as the doc comment when scaffolding an empty pack.
    templates : tuple[str, ...]
        Base templates in which this slot appears.
    """

    key: str
    heading: str
    purpose: str
    templates: tuple[str, ...]


#: Base template names (file stems under :data:`TEMPLATE_DIR`).
TEMPLATE_NAMES: tuple[str, ...] = (
    "screen_system",
    "screen_task",
    "rank_system",
    "rank_task",
    "rank_full_task",
    "build_system",
    "build_task",
)

#: The slot vocabulary. Every slot is optional.
SLOTS: tuple[Slot, ...] = (
    Slot(
        key="codebase_overview",
        heading="## About this codebase",
        purpose=(
            "What the project is, its language(s), layout, and domain — the "
            "orientation a new contributor would want."
        ),
        templates=("screen_system", "rank_system", "build_system"),
    ),
    Slot(
        key="build_test_guidance",
        heading="## Building and testing this codebase",
        purpose=(
            "Codebase-specific advice on compiling and running tests: useful "
            "targets, how to select a single test, pitfalls. Complements the "
            "machine-readable toolchain commands, which are injected separately."
        ),
        templates=("screen_system", "rank_system", "build_system"),
    ),
    Slot(
        key="verification_guidance",
        heading="## What good verification looks like here",
        purpose=(
            "What a deterministic, targeted check looks like in this codebase, "
            "and which check styles to avoid (e.g. known-flaky suites)."
        ),
        templates=("screen_system", "build_system"),
    ),
    Slot(
        key="screening_notes",
        heading="## Additional screening guidance",
        purpose=(
            "Screening-stage-only notes: commit conventions, subsystems worth "
            "favouring, historical quirks that affect scoring."
        ),
        templates=("screen_system",),
    ),
    Slot(
        key="ranking_notes",
        heading="## Additional ranking guidance",
        purpose=(
            "Ranking-stage-only notes: which candidates to prefer, desired "
            "coverage across the suite, project-specific tie-breakers."
        ),
        templates=("rank_system",),
    ),
    Slot(
        key="construction_notes",
        heading="## Additional task-construction guidance",
        purpose=(
            "Build-stage-only notes: house style for task prompts, verifier "
            "conventions, environment details a verifier must honour."
        ),
        templates=("build_system",),
    ),
    Slot(
        key="exclusions",
        heading="## Areas to exclude",
        purpose=(
            "Paths, subsystems, or change kinds that must never become "
            "benchmarks (vendored code, generated files, licence-encumbered "
            "areas)."
        ),
        templates=("screen_system", "rank_system"),
    ),
)

#: Placeholders each template *mechanically requires*. Dropping one of these in
#: an override breaks the stage outright (the agent would not know where to write
#: its output), so their absence is a hard error rather than a warning.
REQUIRED_VARS: dict[str, tuple[str, ...]] = {
    "screen_system": (),
    "screen_task": ("sha", "diff_path", "summary_path"),
    "rank_system": ("candidate_scope",),
    "rank_task": ("candidates_block", "shortlist_path", "rationale_path"),
    "rank_full_task": (
        "candidates_block", "ranking_path", "rationale_path",
        "candidate_shas_path", "checker_path",
    ),
    "build_system": (),
    "build_task": (
        "sha",
        "source_path",
        "reference_diff_path",
        "out_prompt",
        "out_build_env",
        "out_verify",
    ),
}

#: Placeholders whose absence in an override is merely *advisory*. These carry
#: single-sourced content (the rubric tables, the toolchain block) that the
#: template would normally inject; a user who deliberately inlines their own
#: version is making a legitimate choice, so this warns rather than fails.
ADVISORY_VARS: dict[str, tuple[str, ...]] = {
    "screen_system": ("rubric_axis_table", "rubric_axis_guidance"),
    "screen_task": ("rubric_axes_schema",),
    "rank_system": (),
    "rank_task": (),
    "rank_full_task": (),
    "build_system": (),
    "build_task": (),
}

_VAR_RE = re.compile(r"@@(slot:)?([a-zA-Z_][a-zA-Z0-9_]*)@@")


class PromptError(RuntimeError):
    """Raised for malformed prompt packs, bad overrides, or missing templates."""


def slot_by_key(key: str) -> Slot:
    """Return the :class:`Slot` named ``key`` or raise :class:`KeyError`."""
    for s in SLOTS:
        if s.key == key:
            return s
    raise KeyError(key)


@dataclass
class PromptPack:
    """A loaded prompt pack: per-codebase slot content plus any overrides.

    Attributes
    ----------
    root : Path or None
        The pack directory, or ``None`` when no pack was configured.
    slots : dict[str, str]
        Slot key -> file content, for slots the user actually supplied. Absent
        and whitespace-only files are omitted, so an empty file behaves exactly
        like a missing one (a blank slot must not emit a dangling heading).
    overrides : dict[str, str]
        Template name -> replacement body, for active overrides only.
    override_digests : dict[str, str]
        Template name -> SHA-256 of the override body, recorded in the run
        manifest so an overridden run is auditable after the fact.
    warnings : list[str]
        Non-fatal problems (e.g. an override dropping an advisory placeholder),
        surfaced by the CLI at stage start.
    """

    root: Path | None = None
    slots: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)
    override_digests: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_overrides(self) -> bool:
        """True if any base template is being replaced."""
        return bool(self.overrides)

    def banner(self) -> str | None:
        """Return a loud multi-line banner when overrides are active, else None."""
        if not self.has_overrides:
            return None
        lines = [
            "!" * 72,
            "PROMPT TEMPLATE OVERRIDES ACTIVE — this run does NOT use stock prompts.",
            "",
        ]
        for name in sorted(self.overrides):
            lines.append(f"  {name:<16} overridden  (sha256 {self.override_digests[name][:16]})")
        lines += [
            "",
            "Recorded in the run manifest. Re-run without "
            "prompts.allow_template_overrides",
            "to restore the shipped templates.",
            "!" * 72,
        ]
        return "\n".join(lines)


def load_pack(
    pack_dir: Path | str | None,
    *,
    allow_template_overrides: bool = False,
) -> PromptPack:
    """Load a prompt pack from ``pack_dir``.

    Parameters
    ----------
    pack_dir : Path, str, or None
        The pack directory. ``None`` yields an empty pack (fully generic
        prompts) — the tool is usable with no pack at all.
    allow_template_overrides : bool, optional
        Whether ``overrides/`` may take effect. Defaults to False.

    Raises
    ------
    PromptError
        If the directory does not exist; if ``slots/`` or ``overrides/``
        contains an unrecognized filename; if ``overrides/`` is non-empty while
        ``allow_template_overrides`` is False; or if an override drops a
        mechanically required placeholder.
    """
    if pack_dir is None:
        return PromptPack()

    root = Path(pack_dir).expanduser()
    if not root.is_dir():
        raise PromptError(
            f"prompt pack directory not found: {root}\n"
            "Create one with `abc-mine prompts scaffold <dir>`, or omit "
            "prompts.pack to use the generic prompts unchanged."
        )

    pack = PromptPack(root=root)
    _load_slots(root, pack)
    _load_overrides(root, pack, allow_template_overrides=allow_template_overrides)
    return pack


def _load_slots(root: Path, pack: PromptPack) -> None:
    """Load ``slots/*.md`` into ``pack``, rejecting unknown slot names."""
    slots_dir = root / SLOTS_SUBDIR
    if not slots_dir.is_dir():
        # A pack with no slots/ is legal (e.g. an overrides-only pack).
        return

    known = {s.key for s in SLOTS}
    found = sorted(
        p for p in slots_dir.iterdir() if p.is_file() and p.name not in IGNORED_PACK_FILES
    )

    unknown = [p.name for p in found if p.suffix != PROMPT_SUFFIX or p.stem not in known]
    if unknown:
        raise PromptError(
            f"unrecognized file(s) in {slots_dir}: {', '.join(unknown)}\n"
            f"Valid slot files are: {', '.join(sorted(k + PROMPT_SUFFIX for k in known))}\n"
            "Unknown names are rejected rather than ignored so a typo cannot "
            "silently drop your prompt content."
        )

    for path in found:
        text = strip_comments(path.read_text())
        if text:
            pack.slots[path.stem] = text


#: Matches HTML comments, including multi-line ones.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_comments(text: str) -> str:
    """Remove HTML comments from slot content and normalize surrounding blanks.

    Scaffolded slot files consist of an instructional ``<!-- ... -->`` block
    describing what to write. Those comments must never reach a live prompt: a
    freshly-scaffolded pack would otherwise inject "this file is OPTIONAL, delete
    it" into the agent's instructions under a real heading. Stripping them also
    means a comment-only file is correctly treated as *empty*, so its slot
    vanishes exactly as an absent file's would — which is what a user who
    scaffolded a pack and filled in only two slots expects.

    Users may also keep explanatory comments alongside real content; those are
    removed too, so a slot's rendered form is only its prose.
    """
    return _COMMENT_RE.sub("", text).strip()


def _load_overrides(root: Path, pack: PromptPack, *, allow_template_overrides: bool) -> None:
    """Load ``overrides/*.md`` into ``pack``, enforcing the opt-in gate."""
    ov_dir = root / OVERRIDES_SUBDIR
    if not ov_dir.is_dir():
        return

    found = sorted(
        p for p in ov_dir.iterdir() if p.is_file() and p.name not in IGNORED_PACK_FILES
    )
    present = [p for p in found if p.suffix == PROMPT_SUFFIX and p.stem in TEMPLATE_NAMES]
    unknown = [p.name for p in found if p not in present]

    if unknown:
        raise PromptError(
            f"unrecognized file(s) in {ov_dir}: {', '.join(unknown)}\n"
            f"An override must be named exactly after a base template: "
            f"{', '.join(n + PROMPT_SUFFIX for n in TEMPLATE_NAMES)}\n"
            "Unknown names are rejected rather than ignored so a misnamed "
            "override cannot silently do nothing."
        )

    if not present:
        return

    if not allow_template_overrides:
        raise PromptError(
            f"{ov_dir} contains template override(s) — "
            f"{', '.join(p.name for p in present)} — but "
            "prompts.allow_template_overrides is not set to true.\n\n"
            "Replacing a base template is an advanced, deliberately-gated "
            "action: it discards the shipped prompt engineering for that "
            "stage. To proceed, set in your run config:\n\n"
            "    prompts:\n"
            "      allow_template_overrides: true\n\n"
            "Overrides are refused rather than ignored here, because an "
            "override you *believe* is active but silently is not would be "
            "worse than either alternative. If you did not intend to override "
            "anything, move these files to the slots/ directory (additive "
            "context) or delete them."
        )

    for path in present:
        body = path.read_text()
        name = path.stem
        _validate_override(name, body, path)
        pack.overrides[name] = body
        pack.override_digests[name] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        for var in ADVISORY_VARS.get(name, ()):  # advisory: warn only
            if f"@@{var}@@" not in body:
                pack.warnings.append(
                    f"override {path.name} omits @@{var}@@; the content it would "
                    "have injected (single-sourced from your run config) will be "
                    "absent from this prompt. Intentional if you inlined your own."
                )


def _validate_override(name: str, body: str, path: Path) -> None:
    """Reject an override that drops a mechanically required placeholder."""
    missing = [v for v in REQUIRED_VARS.get(name, ()) if f"@@{v}@@" not in body]
    if missing:
        raise PromptError(
            f"override {path} is missing required placeholder(s): "
            f"{', '.join('@@' + m + '@@' for m in missing)}\n"
            "These carry paths the stage substitutes at run time (e.g. where the "
            "agent must write its output). Without them the stage cannot "
            "function, so the override is rejected rather than silently "
            "producing agents that write nowhere.\n"
            f"Start from the shipped template: "
            f"`abc-mine prompts show {name}`."
        )


def load_template(name: str, pack: PromptPack | None = None) -> str:
    """Return the body of base template ``name``, honoring an active override."""
    if name not in TEMPLATE_NAMES:
        raise PromptError(
            f"unknown template {name!r}; expected one of {', '.join(TEMPLATE_NAMES)}"
        )
    if pack is not None and name in pack.overrides:
        return pack.overrides[name]
    path = TEMPLATE_DIR / f"{name}{PROMPT_SUFFIX}"
    if not path.is_file():
        raise PromptError(f"shipped template missing from installation: {path}")
    return path.read_text()


def render_slot(key: str, pack: PromptPack | None) -> str:
    """Render one slot as a complete titled section, or an empty string.

    This all-or-nothing shape is what lets the base templates read naturally in
    both states: an unfilled slot leaves no heading, no lead-in, and no stray
    whitespace behind — as though the section had never been written.
    """
    slot = slot_by_key(key)
    content = (pack.slots.get(key, "") if pack else "").strip()
    if not content:
        return ""
    return f"{slot.heading}\n\n{content}"


def render(
    name: str,
    *,
    pack: PromptPack | None = None,
    variables: Mapping[str, Any] | None = None,
    strict: bool = True,
) -> str:
    """Render template ``name`` with slots and variables substituted.

    Substitution is a **single simultaneous pass**: replaced content is never
    re-scanned, so an injected value that happens to contain ``@@foo@@`` (or JSON
    braces) is left intact.

    Parameters
    ----------
    name : str
        Base template name.
    pack : PromptPack, optional
        Loaded prompt pack supplying slot content and overrides.
    variables : mapping, optional
        Values for ``@@name@@`` placeholders.
    strict : bool, optional
        If True (default), an unresolved non-slot placeholder raises. This
        catches the class of bug where a stage forgets to pass a path and the
        agent receives a literal ``@@summary_path@@`` in its instructions.
        Unresolved *slot* placeholders are always fine — that is how an unfilled
        slot vanishes.

    Raises
    ------
    PromptError
        If ``strict`` and an unknown placeholder remains.
    """
    body = load_template(name, pack)
    variables = dict(variables or {})
    unresolved: list[str] = []

    def _sub(match: re.Match) -> str:
        is_slot, key = match.group(1), match.group(2)
        if is_slot:
            try:
                return render_slot(key, pack)
            except KeyError:
                unresolved.append(f"slot:{key}")
                return match.group(0)
        if key in variables:
            return str(variables[key])
        unresolved.append(key)
        return match.group(0)

    rendered = _VAR_RE.sub(_sub, body)

    if strict and unresolved:
        raise PromptError(
            f"template {name!r} has unresolved placeholder(s): "
            f"{', '.join('@@' + u + '@@' for u in sorted(set(unresolved)))}. "
            "Every non-slot placeholder must be supplied by the stage; leaving "
            "one unsubstituted would put a literal token into the agent's "
            "instructions."
        )

    return _tidy(rendered)


def _tidy(text: str) -> str:
    """Collapse the blank-line runs left behind by vanished slot sections."""
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.rstrip() + "\n"


def compose(
    system_name: str,
    task_name: str,
    *,
    pack: PromptPack | None = None,
    variables: Mapping[str, Any] | None = None,
    strict: bool = True,
) -> str:
    """Render a system + task template pair into one prompt string.

    The agent harness takes a single task prompt, so the stage-specific "system"
    guidance is prepended to the task instructions with a horizontal rule
    between them (matching how the original pipeline composed its prompts).
    """
    system = render(system_name, pack=pack, variables=variables, strict=strict)
    task = render(task_name, pack=pack, variables=variables, strict=strict)
    return f"{system}\n\n---\n\n{task}"


def scaffold(dest: Path | str, *, with_overrides: bool = False) -> list[Path]:
    """Create an empty, commented prompt pack at ``dest``.

    Writes one placeholder file per slot under ``slots/``, each containing only a
    comment describing its purpose — so a user can fill in what they need and
    delete the rest. ``overrides/`` is created **only** when explicitly
    requested, keeping the advanced mechanism out of sight for users who do not
    need it.

    Returns the list of created paths.
    """
    root = Path(dest).expanduser()
    slots_dir = root / SLOTS_SUBDIR
    slots_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    for slot in SLOTS:
        path = slots_dir / f"{slot.key}{PROMPT_SUFFIX}"
        if path.exists():
            continue
        stages = ", ".join(n.split("_")[0] for n in slot.templates)
        path.write_text(
            f"<!-- {slot.key} — used by: {stages}\n"
            f"\n"
            f"{slot.purpose}\n"
            f"\n"
            f"This file is OPTIONAL. Delete it, or leave it empty, and the\n"
            f"corresponding prompt section is omitted entirely — the surrounding\n"
            f"prompt still reads correctly. Write plain markdown below; it is\n"
            f"inserted under the heading \"{slot.heading.lstrip('# ')}\".\n"
            f"-->\n"
        )
        created.append(path)

    if with_overrides:
        ov_dir = root / OVERRIDES_SUBDIR
        ov_dir.mkdir(parents=True, exist_ok=True)
        readme = ov_dir / "README.md"
        if not readme.exists():
            readme.write_text(_OVERRIDES_README)
            created.append(readme)

    return created


_OVERRIDES_README = """\
# Template overrides (advanced)

Files here **replace** a shipped base template outright, rather than adding to
it. Prefer `../slots/` for per-codebase context: slots are additive, survive
upgrades to abc-mine, and cannot break a stage.

To use an override:

1. Copy a shipped template as a starting point:

       abc-mine prompts show screen_system > screen_system.md

2. Edit it, keeping every `@@placeholder@@` the stage substitutes. Removing a
   mechanically required one (an output path) is rejected; removing an advisory
   one (a rubric table) only warns.

3. Opt in explicitly in your run config:

       prompts:
         allow_template_overrides: true

Without that flag, an override present here is a **hard error**, not a silent
no-op — an override you think is active but is not would be worse than either
alternative.

Valid filenames (anything else is rejected):

    screen_system.md   screen_task.md
    rank_system.md     rank_task.md     rank_full_task.md
    build_system.md    build_task.md

Active overrides are recorded in the run manifest with their SHA-256 and
announced in a banner at every stage start.
"""
