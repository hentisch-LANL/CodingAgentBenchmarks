"""Run configuration: the single YAML file that defines a mining run.

Shape
-----
.. code-block:: yaml

    run:
      id: truchas-v1                 # namespaces every output
      data_root: data                # where run outputs live

    codebase:
      name: Truchas                   # human-readable, used in prompts
      repo: ./truchas-src             # a git checkout (complete, not shallow)
      source_mount: /workspace/src    # where the repo appears in-container

    container:
      image: ./containers/truchas-dev.sqfs   # Charliecloud .sqfs or unpacked dir

    toolchain:                        # how to build/test INSIDE the container
      configure: /opt/truchas-tools/configure-truchas.sh Release
      build:     /opt/truchas-tools/build-truchas.sh
      test:      /opt/truchas-tools/test-truchas.sh
      test_one:  /opt/truchas-tools/test-truchas.sh -R '{selector}'
      env:
        TRUCHAS_SOURCE_DIR: "{source}"
        TRUCHAS_BUILD_DIR: "{build}"
      full_build_seconds: 240         # measured by `preflight`, not guessed

    selection:
      mode: all                       # all | recent | sample | range

    prompts:
      pack: ./prompts                 # optional per-codebase slot pack
      # allow_template_overrides: true  # advanced; see prompts.py

Design notes
------------
**The toolchain is structured data, not prose.** This is the crux of
generalizing the pipeline. Three consumers need the build/test commands, and two
of them are not agents at all:

1. the screening/ranking agents, which are *told* the commands in their prompts;
2. :mod:`abc_mine.preflight`, which *executes* them to prove they work and
   to measure real cost;
3. the emitted task verifiers, which must call something that still exists at
   grading time — and the build stage's Layer-2 polarity check, which runs those
   verifiers in a clean environment.

Free-form prose could serve (1) but not (2) or (3), which is why the commands
live here in a machine-readable block and are *rendered into* the prompts rather
than being written by hand into them.

**Costs are measured, not asserted.** ``full_build_seconds`` exists because the
original pipeline hard-coded "a full build takes ~30+ minutes" into the screening
prompt, then had to add a correcting paragraph to the *ranking* prompt explaining
that it actually takes ~4 minutes and that Stage 1's cost scores were therefore
systematically wrong. Here the number is one config value — ideally written by
``preflight`` from an actual timed build — consumed identically by every stage.

**Models come from the environment.** ``LLM_BASE_URL`` / ``LLM_MODEL`` /
``LLM_API`` / ``LLM_API_KEY``, never from this file, so a config is safe to
commit and a key is never serialized into a manifest.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from .rubric import Rubric, rubric_from_config

#: Default in-container mount point for the codebase under test.
DEFAULT_SOURCE_MOUNT = "/workspace/src"

#: Default in-container scratch build directory offered to the toolchain as
#: ``{build}``. Deliberately outside the source mount and outside ``/workspace``:
#: at grading time abc-bench bind-mounts the task's code tree onto
#: ``/workspace``, which would hide anything the image placed beneath it.
DEFAULT_BUILD_DIR = "/tmp/abc-mine-build"

#: Recognized commit-selection modes.
SELECTION_MODES = ("all", "recent", "sample", "range")

#: Environment variables that configure the model endpoint. Never in the config
#: file; ``LLM_API_KEY`` is never recorded anywhere.
LLM_ENV_VARS = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API", "LLM_API_KEY")

#: Upstream API styles a run may use, in ``LLM_API``. The style determines both
#: the protocol the harness speaks and the credential scheme the model proxy
#: sends upstream, so it is part of the run's identity rather than a transport
#: detail: ``anthropic_messages`` is the only style under which OpenCode enables
#: Anthropic prompt caching.
LLM_API_STYLES = ("chat_completions", "responses", "anthropic_messages")


class ConfigError(ValueError):
    """Raised for a malformed or internally inconsistent run configuration."""


@dataclass(frozen=True)
class CodebaseConfig:
    """The repository being mined.

    Attributes
    ----------
    name : str
        Human-readable project name, interpolated into prompts.
    repo : Path
        Host path to a **complete** (non-shallow) git checkout. Mining reads
        history and builds starting states from it; a shallow clone cannot
        support the bundle-cutoff procedure.
    source_mount : str
        Absolute in-container path at which the checkout is presented to agents.
    group_id : str
        abc-bench task-group id for emitted tasks. Defaults to a slug of
        ``name``.
    group_description : str
        Description written into the emitted ``group.yaml``.
    """

    name: str
    repo: Path
    source_mount: str = DEFAULT_SOURCE_MOUNT
    group_id: str = ""
    group_description: str = ""

    def __post_init__(self) -> None:
        if not self.source_mount.startswith("/"):
            raise ConfigError(
                f"codebase.source_mount must be an absolute in-container path, "
                f"got {self.source_mount!r}"
            )


@dataclass(frozen=True)
class ContainerConfig:
    """The Charliecloud image every agent runs inside.

    Attributes
    ----------
    image : Path
        A SquashFS ``.sqfs`` file or an unpacked image directory, handed verbatim
        to ``ch-run``. Charliecloud specifically: :class:`automaton.sandbox.Sandbox`
        shells out to ``ch-run``, so Docker/Podman/Apptainer images are not
        interchangeable here.
    writable : bool
        Whether to run with ``--write-fake`` (an ephemeral overlay). Required for
        any agent that compiles in-image.
    """

    image: Path
    writable: bool = True


@dataclass(frozen=True)
class ToolchainConfig:
    """How to configure, build, and test the codebase inside the container.

    Every command is a shell string run with ``bash -lc`` in the container, with
    ``{source}``, ``{build}``, and ``{install}`` interpolated. ``test_one``
    additionally receives ``{selector}``.

    Attributes
    ----------
    configure : str
        Optional configure/generate step (e.g. a ``cmake`` invocation). Empty for
        codebases that need none.
    build : str
        Build the project. Should be parallel where possible; a serial build is
        the main reason a build "feels expensive".
    test : str
        Run the full test suite. Used by ``preflight``; agents are steered away
        from it in favour of ``test_one``.
    test_one : str
        Run a **targeted** subset, given ``{selector}``. This is the single most
        important command in the file: targeted verification is what makes the
        emitted benchmarks affordable to grade, and the rubric's ``verify_cost``
        axis rewards commits checkable this way.
    build_dir, install_dir : str
        In-container scratch paths exposed as ``{build}`` / ``{install}``.
    env : dict[str, str]
        Extra environment variables, with the same interpolation applied.
    full_build_seconds : float or None
        Measured wall-clock cost of a clean build, written by ``preflight``.
        ``None`` means unmeasured, and the prompts say so explicitly rather than
        inventing a figure.
    targeted_test_seconds : float or None
        Measured cost of one targeted test invocation.
    preflight_selector : str
        A test selector known to match at least one real test at HEAD, used by
        ``preflight`` to prove ``test_one`` actually selects something.
    """

    configure: str = ""
    build: str = ""
    test: str = ""
    test_one: str = ""
    build_dir: str = DEFAULT_BUILD_DIR
    install_dir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    full_build_seconds: float | None = None
    targeted_test_seconds: float | None = None
    preflight_selector: str = ""

    def __post_init__(self) -> None:
        if self.test_one and "{selector}" not in self.test_one:
            raise ConfigError(
                "toolchain.test_one must contain the '{selector}' placeholder — "
                "it is the command used to run ONE targeted test, e.g.\n"
                "    test_one: ctest --test-dir {build} -R '{selector}'"
            )
        if self.build and not self.test and not self.test_one:
            raise ConfigError(
                "toolchain defines a build but no way to test. At least one of "
                "toolchain.test or toolchain.test_one is required: without a test "
                "command, no emitted benchmark can be verified beyond "
                "'it compiles', which the rubric treats as near-worthless."
            )

    # -- rendering ---------------------------------------------------------

    def paths(self, source: str) -> dict[str, str]:
        """Return the interpolation variables for a given source path."""
        return {
            "source": source,
            "build": self.build_dir,
            "install": self.install_dir or f"{self.build_dir}-install",
        }

    def render(self, command: str, source: str, *, selector: str | None = None) -> str:
        """Interpolate ``{source}``/``{build}``/``{install}``/``{selector}``.

        Raises
        ------
        ConfigError
            If the command references an unknown placeholder. Failing here beats
            shipping a literal ``{foo}`` into a container command line.
        """
        values = self.paths(source)
        if selector is not None:
            values["selector"] = selector
        try:
            return command.format(**values)
        except KeyError as exc:
            raise ConfigError(
                f"toolchain command references unknown placeholder {exc} in:\n"
                f"    {command}\n"
                f"Available: {', '.join('{' + k + '}' for k in values)}"
            ) from exc
        except (IndexError, ValueError) as exc:
            raise ConfigError(
                f"toolchain command has malformed placeholder syntax: {command!r} "
                f"({exc}). Literal braces must be doubled: '{{{{' and '}}}}'."
            ) from exc

    def rendered_env(self, source: str) -> dict[str, str]:
        """Return ``env`` with interpolation applied to each value."""
        return {k: self.render(str(v), source) for k, v in self.env.items()}

    def render_prompt_block(self, source: str) -> str:
        """Render the toolchain as the markdown block injected into prompts.

        This is what keeps the agents' instructions and the machinery in
        agreement: the commands an agent is told to use are generated from the
        same strings ``preflight`` verified and the verifiers will call.
        """
        lines: list[str] = []
        rows: list[tuple[str, str]] = []
        if self.configure:
            rows.append(("Configure", self.render(self.configure, source)))
        if self.build:
            rows.append(("Build", self.render(self.build, source)))
        if self.test_one:
            rows.append(
                ("Run one test", self.render(self.test_one, source, selector="<selector>"))
            )
        if self.test:
            rows.append(("Run all tests", self.render(self.test, source)))

        if rows:
            width = max(len(label) for label, _ in rows)
            lines.append("| Step | Command |")
            lines.append("|------|---------|")
            for label, cmd in rows:
                lines.append(f"| {label:<{width}} | `{cmd}` |")

        env = self.rendered_env(source)
        if env:
            lines.append("")
            lines.append("Environment set for these commands:")
            lines.append("")
            for key, value in sorted(env.items()):
                lines.append(f"- `{key}={value}`")

        lines.append("")
        if self.full_build_seconds is not None:
            lines.append(
                f"**Measured cost:** a clean full build takes about "
                f"**{_humanize_seconds(self.full_build_seconds)}** in this "
                f"container."
            )
            if self.targeted_test_seconds is not None:
                lines.append(
                    f"One targeted test invocation takes about "
                    f"**{_humanize_seconds(self.targeted_test_seconds)}**."
                )
            lines.append("")
            lines.append(
                "Weigh verification cost against these *measured* figures — do "
                "not assume building is expensive if the measurement says "
                "otherwise."
            )
        else:
            lines.append(
                "**Build cost has not been measured for this codebase.** Do not "
                "assume a build is prohibitively expensive, and do not assume it "
                "is cheap; judge verifiability primarily on whether a "
                "deterministic, *targeted* check is possible at all."
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class SelectionConfig:
    """Which commits to screen.

    Attributes
    ----------
    mode : str
        ``all``, ``recent``, ``sample``, or ``range``.
    count : int or None
        Item count for ``recent`` / ``sample``.
    stride : int or None
        Explicit stride for ``sample`` (otherwise derived from ``count``).
    range : tuple[str, str] or None
        Inclusive endpoints for ``range``.
    paths : tuple[str, ...]
        If set, keep only commits touching these pathspecs. A cheap, mechanical
        way to scope mining to one subsystem without spending agent budget.
    exclude_merges : bool
        Skip merge commits. On by default: a merge's "change" is rarely a
        coherent authored task, and its diff-to-first-parent is misleading.
    max_files_changed, max_diff_lines : int or None
        Mechanical pre-filters that drop sprawling commits before any model sees
        them. Filtering these in Python is far cheaper than paying an agent to
        score them 0.
    """

    mode: str = "all"
    count: int | None = None
    stride: int | None = None
    range: tuple[str, str] | None = None
    paths: tuple[str, ...] = ()
    exclude_merges: bool = True
    max_files_changed: int | None = None
    max_diff_lines: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in SELECTION_MODES:
            raise ConfigError(
                f"selection.mode must be one of {', '.join(SELECTION_MODES)}, "
                f"got {self.mode!r}"
            )
        if self.mode in ("recent", "sample") and not self.count and not self.stride:
            raise ConfigError(
                f"selection.mode '{self.mode}' requires 'count' "
                f"(or 'stride' for sample)"
            )
        if self.mode == "range" and not self.range:
            raise ConfigError(
                "selection.mode 'range' requires 'range: [<sha-a>, <sha-b>]'"
            )


@dataclass(frozen=True)
class PromptsConfig:
    """Where per-codebase prompt content comes from.

    Attributes
    ----------
    pack : Path or None
        A prompt-pack directory (see :mod:`abc_mine.prompts`).
    allow_template_overrides : bool
        Opt-in gate for ``overrides/``. Default False; an override present
        without this is a hard error, never a silent no-op.
    """

    pack: Path | None = None
    allow_template_overrides: bool = False


@dataclass(frozen=True)
class ExecutionConfig:
    """Parallelism, retries, timeouts, and artifact retention.

    Attributes
    ----------
    parallel : str
        ``serial``, ``local``, or ``mpi``. Default ``serial`` — nothing beyond
        the standard library. ``local`` forks worker processes on one node;
        ``mpi`` requires the ``[mpi]`` extra and a launcher, and is only needed
        for multi-node fan-out.
    workers : int
        Worker count for ``local``. Ignored by ``serial``; for ``mpi`` the
        launcher's rank count governs.
    max_attempts : int
        Attempts per item before giving up. Bounded so a systematically-failing
        item cannot be retried forever across resumes.
    timeout_seconds : int or None
        Per-agent wall-clock limit.
    scratch_dir : str
        Node-local scratch root for heavy throwaway state (worktrees, build
        trees). Should NOT be on a shared/Lustre filesystem.
    keep_logs : str
        ``none``, ``failed``, or ``all``. Retention of agent transcripts and
        container logs. Default ``failed``: keep exactly what is needed to
        diagnose a failure, discard the rest. (The original pipeline discarded
        everything, which made failures effectively unbudgeted and
        undiagnosable.)
    sandboxed : bool
        Route model traffic through the loopback proxy and isolate the network.
    """

    parallel: str = "serial"
    workers: int = 1
    max_attempts: int = 2
    timeout_seconds: int | None = None
    scratch_dir: str = "/var/tmp"
    keep_logs: str = "failed"
    sandboxed: bool = True

    def __post_init__(self) -> None:
        if self.parallel not in ("serial", "local", "mpi"):
            raise ConfigError(
                f"execution.parallel must be 'serial', 'local', or 'mpi', "
                f"got {self.parallel!r}"
            )
        if self.workers < 1:
            raise ConfigError(f"execution.workers must be >= 1, got {self.workers}")
        if self.max_attempts < 1:
            raise ConfigError(
                f"execution.max_attempts must be >= 1, got {self.max_attempts}"
            )
        if self.keep_logs not in ("none", "failed", "all"):
            raise ConfigError(
                f"execution.keep_logs must be 'none', 'failed', or 'all', "
                f"got {self.keep_logs!r}"
            )


#: What ranking produces. ``shortlist``: the agent selects the commits it judges
#: worth building. ``full``: the agent orders *every* candidate, and the build
#: stage works down that order for as long as it is allowed to.
RANK_OUTPUTS = ("shortlist", "full")

#: Ranking methods. ``single_context``: one agent judges every candidate at
#: once. ``interleaving``: the candidates are split into random groups, each
#: group is ranked by its own single-context agent, and the per-group rankings
#: are interleaved into one shortlist.
RANK_METHODS = ("single_context", "interleaving")


@dataclass(frozen=True)
class InterleavingConfig:
    """Settings for ``rank.method: interleaving``.

    Attributes
    ----------
    groups : int
        Number of random groups the candidates are split into (as evenly as
        possible). Each group must fit in one ranking prompt.
    seed : int
        Seed for the random split and for the order in which groups contribute
        within each interleaving round. Fixed per run: the split is recorded,
        and a resume that would produce a different split is refused.
    """

    groups: int = 2
    seed: int = 0

    def __post_init__(self) -> None:
        if self.groups < 2:
            raise ConfigError(
                f"rank.interleaving.groups must be >= 2, got {self.groups} "
                "(use rank.method: single_context for one group)"
            )


@dataclass(frozen=True)
class RankConfig:
    """Ranking-stage settings.

    Attributes
    ----------
    method : str
        One of :data:`RANK_METHODS`. See :class:`InterleavingConfig` for the
        assumption the ``interleaving`` method rests on.
    output : str
        One of :data:`RANK_OUTPUTS`. ``full`` ranks every candidate (an agent's
        ordering must be an exact permutation of its candidates, or it is
        rejected), and the build stage builds in that order.
    max_prompt_tokens : int or None
        Refuse to run when a composed ranking prompt (the harness system prompt
        plus the task prompt with the candidates embedded) exceeds this many
        tokens, as counted by the endpoint. Applies to each group under
        ``interleaving``. The single-context assumption must be *checked*
        rather than hoped for. Set it below the model's hard context limit,
        leaving room for the agent's own work. ``None`` disables the check (and
        the count).
    max_candidates : int or None
        Hard cap on candidates fed to the ranker, applied after sorting by
        score. A blunter instrument than the token budget; off by default.
    interleaving : InterleavingConfig
        Settings for ``method: interleaving``.
    """

    method: str = "single_context"
    output: str = "shortlist"
    max_prompt_tokens: int | None = 300_000
    max_candidates: int | None = None
    interleaving: InterleavingConfig = field(default_factory=InterleavingConfig)

    def __post_init__(self) -> None:
        if self.method not in RANK_METHODS:
            raise ConfigError(
                f"rank.method must be one of {', '.join(RANK_METHODS)}, got {self.method!r}"
            )
        if self.output not in RANK_OUTPUTS:
            raise ConfigError(
                f"rank.output must be one of {', '.join(RANK_OUTPUTS)}, got {self.output!r}"
            )


@dataclass(frozen=True)
class MineConfig:
    """A fully resolved run configuration."""

    run_id: str
    data_root: Path
    codebase: CodebaseConfig
    container: ContainerConfig
    toolchain: ToolchainConfig
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    rank: RankConfig = field(default_factory=RankConfig)
    rubric: Rubric = field(default_factory=lambda: rubric_from_config(None))
    source_path: Path | None = None

    # -- derived paths -----------------------------------------------------

    @property
    def run_dir(self) -> Path:
        """Root directory for this run's outputs."""
        return self.data_root / self.run_id

    @property
    def screen_dir(self) -> Path:
        """Screen-stage output directory."""
        return self.run_dir / "screen"

    @property
    def summaries_dir(self) -> Path:
        """Per-commit screening summaries (the durable screen artifact)."""
        return self.screen_dir / "summaries"

    @property
    def rank_dir(self) -> Path:
        """Rank-stage output directory."""
        return self.run_dir / "rank"

    @property
    def build_dir(self) -> Path:
        """Build-stage output directory."""
        return self.run_dir / "build"

    @property
    def tasks_dir(self) -> Path:
        """Emitted abc-bench task tree (``tasks/<group_id>/<task-id>/``)."""
        return self.build_dir / "tasks"

    @property
    def group_id(self) -> str:
        """abc-bench task-group id, defaulting to a slug of the codebase name."""
        return self.codebase.group_id or slugify(self.codebase.name)

    def with_toolchain(self, **changes: Any) -> "MineConfig":
        """Return a copy with toolchain fields replaced (used by ``preflight``)."""
        return replace(self, toolchain=replace(self.toolchain, **changes))


def slugify(text: str, *, max_length: int = 64) -> str:
    """Lowercase ``text`` and reduce it to a hyphen-separated slug."""
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_length].rstrip("-")


def _humanize_seconds(seconds: float) -> str:
    """Render a duration the way a person would say it."""
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    return f"{minutes / 60.0:.1f} hours"


# -- loading -----------------------------------------------------------------


def load(path: Path | str, *, overrides: Mapping[str, Any] | None = None) -> MineConfig:
    """Load and validate a run config from YAML.

    Relative paths resolve against the **config file's directory**, so a config
    plus its prompt pack and container form a relocatable bundle.

    Parameters
    ----------
    path : Path or str
        Path to the YAML config.
    overrides : mapping, optional
        Dotted-key overrides applied after loading, e.g.
        ``{"execution.workers": 8}``. Used for CLI flags. Recorded in the
        manifest so a resumed run can detect drift.

    Raises
    ------
    ConfigError
        On a missing file, malformed YAML, unknown top-level section, or any
        failed field validation.
    """
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")

    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {cfg_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"config must be a YAML mapping, got {type(raw).__name__}")

    raw = dict(raw)
    for dotted, value in (overrides or {}).items():
        _apply_override(raw, dotted, value)

    known = {
        "run",
        "codebase",
        "container",
        "toolchain",
        "selection",
        "prompts",
        "execution",
        "rank",
        "rubric",
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"unknown top-level config section(s): {sorted(unknown)}. "
            f"Expected: {sorted(known)}"
        )

    base = cfg_path.parent

    def _path(value: Any, what: str) -> Path:
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    run = _section(raw, "run")
    run_id = str(run.get("id") or "").strip()
    if not run_id:
        raise ConfigError("run.id is required (it namespaces every output)")
    if run_id != slugify(run_id):
        raise ConfigError(
            f"run.id must be a slug (lowercase alphanumerics and hyphens), got "
            f"{run_id!r}; suggested: {slugify(run_id)!r}"
        )
    data_root = _path(run.get("data_root", "data"), "run.data_root")

    cb = _section(raw, "codebase", required=True)
    if not cb.get("repo"):
        raise ConfigError("codebase.repo is required (path to a git checkout)")
    codebase = CodebaseConfig(
        name=str(cb.get("name") or "").strip() or "the codebase",
        repo=_path(cb["repo"], "codebase.repo"),
        source_mount=str(cb.get("source_mount", DEFAULT_SOURCE_MOUNT)),
        group_id=str(cb.get("group_id", "")),
        group_description=str(cb.get("group_description", "")),
    )

    ct = _section(raw, "container", required=True)
    if not ct.get("image"):
        raise ConfigError(
            "container.image is required (a Charliecloud .sqfs file or an "
            "unpacked image directory)"
        )
    container = ContainerConfig(
        image=_path(ct["image"], "container.image"),
        writable=bool(ct.get("writable", True)),
    )

    tc = _section(raw, "toolchain")
    env_raw = tc.get("env") or {}
    if not isinstance(env_raw, Mapping):
        raise ConfigError("toolchain.env must be a mapping of NAME -> value")
    toolchain = ToolchainConfig(
        configure=str(tc.get("configure", "")).strip(),
        build=str(tc.get("build", "")).strip(),
        test=str(tc.get("test", "")).strip(),
        test_one=str(tc.get("test_one", "")).strip(),
        build_dir=str(tc.get("build_dir", DEFAULT_BUILD_DIR)),
        install_dir=str(tc.get("install_dir", "")),
        env={str(k): str(v) for k, v in env_raw.items()},
        full_build_seconds=_opt_float(tc.get("full_build_seconds"), "toolchain.full_build_seconds"),
        targeted_test_seconds=_opt_float(
            tc.get("targeted_test_seconds"), "toolchain.targeted_test_seconds"
        ),
        preflight_selector=str(tc.get("preflight_selector", "")),
    )

    sel = _section(raw, "selection")
    rng = sel.get("range")
    if rng is not None:
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            raise ConfigError("selection.range must be a two-element list [sha_a, sha_b]")
        rng = (str(rng[0]), str(rng[1]))
    paths_raw = sel.get("paths") or ()
    if isinstance(paths_raw, str):
        paths_raw = (paths_raw,)
    selection = SelectionConfig(
        mode=str(sel.get("mode", "all")),
        count=_opt_int(sel.get("count"), "selection.count"),
        stride=_opt_int(sel.get("stride"), "selection.stride"),
        range=rng,
        paths=tuple(str(p) for p in paths_raw),
        exclude_merges=bool(sel.get("exclude_merges", True)),
        max_files_changed=_opt_int(sel.get("max_files_changed"), "selection.max_files_changed"),
        max_diff_lines=_opt_int(sel.get("max_diff_lines"), "selection.max_diff_lines"),
    )

    pr = _section(raw, "prompts")
    prompts = PromptsConfig(
        pack=_path(pr["pack"], "prompts.pack") if pr.get("pack") else None,
        allow_template_overrides=bool(pr.get("allow_template_overrides", False)),
    )

    ex = _section(raw, "execution")
    execution = ExecutionConfig(
        parallel=str(ex.get("parallel", "serial")),
        workers=int(ex.get("workers", 1)),
        max_attempts=int(ex.get("max_attempts", 2)),
        timeout_seconds=_opt_int(ex.get("timeout_seconds"), "execution.timeout_seconds"),
        scratch_dir=str(ex.get("scratch_dir", "/var/tmp")),
        keep_logs=str(ex.get("keep_logs", "failed")),
        sandboxed=bool(ex.get("sandboxed", True)),
    )

    rk = _section(raw, "rank")
    if "max_candidate_tokens" in rk:
        # Renamed, and deliberately not accepted as an alias: the old key was an
        # *estimate* of the candidate payload alone, while the new one is an
        # exact count of the whole prompt. Carrying an old value over silently
        # would change what it bounds.
        raise ConfigError(
            "rank.max_candidate_tokens has been replaced by rank.max_prompt_tokens, "
            "which bounds the exact token count of the whole ranking prompt "
            "(instructions plus every candidate), measured through the endpoint. "
            "Rename the key and review its value against your model's context limit."
        )
    known_rank = {"method", "output", "max_prompt_tokens", "max_candidates", "interleaving"}
    unknown_rank = set(rk) - known_rank
    if unknown_rank:
        raise ConfigError(
            f"unknown rank setting(s): {sorted(unknown_rank)}. Expected: {sorted(known_rank)}"
        )
    il = rk.get("interleaving") or {}
    if not isinstance(il, Mapping):
        raise ConfigError("rank.interleaving must be a mapping")
    unknown_il = set(il) - {"groups", "seed"}
    if unknown_il:
        raise ConfigError(
            f"unknown rank.interleaving setting(s): {sorted(unknown_il)}. "
            "Expected: ['groups', 'seed']"
        )
    il_defaults = InterleavingConfig()
    il_groups = _opt_int(il.get("groups"), "rank.interleaving.groups")
    il_seed = _opt_int(il.get("seed"), "rank.interleaving.seed")
    rank = RankConfig(
        method=str(rk.get("method", "single_context")),
        output=str(rk.get("output", "shortlist")),
        max_prompt_tokens=_opt_int(
            rk.get("max_prompt_tokens", 300_000), "rank.max_prompt_tokens"
        ),
        max_candidates=_opt_int(rk.get("max_candidates"), "rank.max_candidates"),
        interleaving=InterleavingConfig(
            groups=il_defaults.groups if il_groups is None else il_groups,
            seed=il_defaults.seed if il_seed is None else il_seed,
        ),
    )

    rubric = rubric_from_config(_section(raw, "rubric"))

    return MineConfig(
        run_id=run_id,
        data_root=data_root,
        codebase=codebase,
        container=container,
        toolchain=toolchain,
        selection=selection,
        prompts=prompts,
        execution=execution,
        rank=rank,
        rubric=rubric,
        source_path=cfg_path,
    )


def _section(raw: Mapping[str, Any], key: str, *, required: bool = False) -> dict[str, Any]:
    """Return config section ``key`` as a dict, validating its type."""
    value = raw.get(key)
    if value is None:
        if required:
            raise ConfigError(f"config section '{key}' is required")
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"config section '{key}' must be a mapping")
    return dict(value)


def _opt_int(value: Any, what: str) -> int | None:
    """Coerce an optional integer, or raise :class:`ConfigError`."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} must be an integer, got {value!r}") from exc


def _opt_float(value: Any, what: str) -> float | None:
    """Coerce an optional float, or raise :class:`ConfigError`."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} must be a number, got {value!r}") from exc


def _apply_override(raw: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``raw['a']['b'] = value`` for a dotted key ``'a.b'``."""
    parts = dotted.split(".")
    node: dict[str, Any] = raw
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            raise ConfigError(
                f"cannot apply override {dotted!r}: '{part}' is not a section"
            )
        node = child
    node[parts[-1]] = value


def llm_env() -> dict[str, str]:
    """Return the model-endpoint settings from the environment (key excluded).

    Raises
    ------
    ConfigError
        If a required variable is missing. Checked before any agent launches so a
        misconfigured endpoint fails in seconds rather than after provisioning
        containers.
    """
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    api = os.environ.get("LLM_API", "chat_completions").strip()

    missing = [n for n, v in (("LLM_BASE_URL", base_url), ("LLM_MODEL", model)) if not v]
    if not os.environ.get("LLM_API_KEY"):
        missing.append("LLM_API_KEY")
    if missing:
        raise ConfigError(
            "model endpoint is not configured; set: " + ", ".join(missing) + "\n"
            "The model is chosen entirely by environment variables so that no "
            "config file ever contains a credential:\n"
            "  LLM_BASE_URL   endpoint URL\n"
            "  LLM_MODEL      model id sent to the endpoint\n"
            "  LLM_API        'chat_completions' (default), 'responses', or\n"
            "                 'anthropic_messages'\n"
            "  LLM_API_KEY    the credential (never recorded in run outputs)"
        )
    if api not in LLM_API_STYLES:
        raise ConfigError(
            "LLM_API must be one of " + ", ".join(repr(s) for s in LLM_API_STYLES)
            + f", got {api!r}"
        )
    return {"base_url": base_url, "model": model, "api": api}


def validate_paths(cfg: MineConfig) -> list[str]:
    """Return human-readable problems with on-disk prerequisites.

    Checks the repo exists, is a git repository, and is not shallow, and that the
    container image exists. Returns an empty list when everything is in order, so
    callers can present all problems at once instead of one per run attempt.
    """
    problems: list[str] = []

    repo = cfg.codebase.repo
    if not repo.exists():
        problems.append(f"codebase.repo does not exist: {repo}")
    elif not (repo / ".git").exists():
        problems.append(f"codebase.repo is not a git repository (no .git): {repo}")
    else:
        import subprocess

        shallow = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
        )
        if shallow.returncode == 0 and shallow.stdout.strip() == "true":
            problems.append(
                f"codebase.repo is a shallow clone: {repo}\n"
                "    Starting-state construction needs full history "
                "(`git fetch --unshallow`)."
            )

    if not cfg.container.image.exists():
        problems.append(f"container.image does not exist: {cfg.container.image}")

    return problems
