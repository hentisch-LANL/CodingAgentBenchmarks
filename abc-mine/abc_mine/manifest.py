"""Run manifest: provenance, configuration locking, and drift detection.

At ``init`` a run writes ``<run_dir>/run.json``: a strong record of *what this
run is* — the resolved configuration, the codebase's git identity, the container
image digest, tool versions, the model endpoint and model id, the rubric in
force, and any active prompt overrides. Every subsequent stage compares its own
resolved configuration against that record and **refuses to continue on drift**
unless explicitly allowed.

Why drift detection matters
---------------------------
A screening sweep over a large history is not atomic; it may span several Slurm
allocations across days. Without a lock it is entirely possible to screen half a
history at one set of rubric weights and the other half at another, then rank the
union as though the scores were comparable. They would not be, and nothing in the
output would say so. The lock makes that failure loud instead of silent.

Provenance is not reproducibility
---------------------------------
This is worth stating plainly, because the two are easy to conflate. The manifest
records *what was used*; it cannot make a run reproducible. Agents are
nondeterministic, and a model id behind an endpoint can be repointed without the
id changing. So:

- Re-running a completed run will **not** reproduce it byte-for-byte.
- Per-item records carry their own model id and timestamp, so a resumed run's
  provenance is *auditable at item granularity* — you can always tell which items
  were judged when, and under what settings.

Treat the manifest as an audit trail, not a guarantee of determinism.

Severity model
--------------
Not every change is equally dangerous, so drift is classified:

``critical``
    Changes that invalidate comparisons between already-finished and future items
    — the rubric, the codebase, commit selection, prompt content. Blocks.
``warning``
    Changes that affect throughput or retention but not comparability —
    parallelism, worker count, log retention. Recorded, never blocks.
``exempt``
    Values that legitimately rotate between allocations — the endpoint URL, the
    scratch directory. Noted only.

The model id is deliberately **critical**: screening half a history with a cheap
model and half with a frontier model produces incomparable scores, which is
exactly the failure the lock exists to prevent.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .config import MineConfig
from .prompts import PromptPack

#: Manifest filename, written at the run root.
MANIFEST_NAME = "run.json"

#: Per-stage attempt log. Append-only JSON Lines.
ATTEMPTS_NAME = "attempts.jsonl"

#: Config keys whose change blocks a resume (see module docstring).
CRITICAL_KEYS = (
    "codebase.repo",
    "codebase.name",
    "codebase.source_mount",
    "selection",
    "rubric",
    "prompts.pack_digest",
    "prompts.overrides",
    "toolchain.configure",
    "toolchain.build",
    "toolchain.test",
    "toolchain.test_one",
    "toolchain.env",
    "model.id",
    "model.api",
)

#: Config keys whose change is recorded but never blocks.
WARNING_KEYS = (
    "container.image",
    "container.image_digest",
    "execution.parallel",
    "execution.workers",
    "execution.max_attempts",
    "execution.timeout_seconds",
    "execution.keep_logs",
    "execution.sandboxed",
    "rank",
    "toolchain.full_build_seconds",
    "toolchain.targeted_test_seconds",
)

#: Keys that legitimately vary between allocations; noted only.
EXEMPT_KEYS = (
    "model.base_url",
    "execution.scratch_dir",
)


class ManifestError(RuntimeError):
    """Raised on a missing, unreadable, or drift-conflicting run manifest."""


@dataclass(frozen=True)
class Drift:
    """One detected configuration difference.

    Attributes
    ----------
    key : str
        Dotted config key.
    old, new : Any
        The locked value and the current value.
    severity : str
        ``critical``, ``warning``, or ``exempt``.
    """

    key: str
    old: Any
    new: Any
    severity: str

    def describe(self) -> str:
        """Return a one-line ``key: old -> new`` rendering."""
        return f"{self.key}: {_brief(self.old)}  ->  {_brief(self.new)}"


def _brief(value: Any, limit: int = 72) -> str:
    """Render a value compactly for a drift report."""
    if value is None:
        return "(unset)"
    text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Return the SHA-256 of a file, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def digest_container(image: Path) -> str:
    """Return a cheap but change-sensitive identity for a container image.

    A full hash of a multi-gigabyte SquashFS on every stage start would be
    wasteful, so for regular files this records ``size:mtime_ns`` — enough to
    notice a rebuilt or swapped image. Unpacked image *directories* are
    summarized by their top-level entries. Small images (<64 MiB) get a real
    SHA-256, since it is cheap enough to be worth the precision.
    """
    image = Path(image)
    try:
        if image.is_dir():
            names = sorted(p.name for p in image.iterdir())
            return "dir:" + hashlib.sha256("\n".join(names).encode()).hexdigest()[:32]
        stat = image.stat()
        if stat.st_size < (64 << 20):
            return "sha256:" + _sha256_file(image)
        return f"size-mtime:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError as exc:
        return f"unavailable:{exc.__class__.__name__}"


def digest_prompt_pack(pack: PromptPack) -> str:
    """Return a content digest over every slot and override in a pack.

    Content-based rather than path-based: editing a slot file in place must count
    as drift, since it changes what every subsequent agent is told. Returns
    ``"none"`` for an empty pack so a stock run has a stable marker.
    """
    if not pack.slots and not pack.overrides:
        return "none"
    h = hashlib.sha256()
    for key in sorted(pack.slots):
        h.update(f"slot:{key}\0".encode())
        h.update(pack.slots[key].encode("utf-8"))
        h.update(b"\0")
    for name in sorted(pack.overrides):
        h.update(f"override:{name}\0".encode())
        h.update(pack.overrides[name].encode("utf-8"))
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def git_identity(repo: Path) -> dict[str, Any]:
    """Return the git identity of ``repo``: HEAD, commit count, and remote."""
    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            )
            return out.stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            return ""

    return {
        "head": _git("rev-parse", "HEAD"),
        "head_subject": _git("log", "-1", "--format=%s"),
        "commit_count": _git("rev-list", "--count", "HEAD"),
        "remote": _git("config", "--get", "remote.origin.url"),
        "describe": _git("describe", "--tags", "--always", "--dirty"),
    }


def tool_versions() -> dict[str, str]:
    """Return versions of the external tools whose behaviour affects a run."""
    def _version(cmd: list[str]) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            text = (out.stdout or out.stderr).strip()
            return text.splitlines()[0] if text else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    return {
        "python": platform.python_version(),
        "git": _version(["git", "--version"]),
        "ch_run": _version(["ch-run", "--version"]),
        "abc_mine": __version__,
    }


def config_fingerprint(
    cfg: MineConfig, pack: PromptPack, model: Mapping[str, str]
) -> dict[str, Any]:
    """Build the flat, comparable fingerprint that drift detection operates on.

    Flat dotted keys (rather than the nested config) keep comparison and
    reporting simple, and make the severity tables above directly indexable.
    Deliberately excluded: the API key (never recorded anywhere) and ``run.id`` /
    ``data_root`` (identity of the run, not settings within it).
    """
    tc = cfg.toolchain
    return {
        "codebase.name": cfg.codebase.name,
        "codebase.repo": str(cfg.codebase.repo),
        "codebase.source_mount": cfg.codebase.source_mount,
        "container.image": str(cfg.container.image),
        "container.image_digest": digest_container(cfg.container.image),
        "toolchain.configure": tc.configure,
        "toolchain.build": tc.build,
        "toolchain.test": tc.test,
        "toolchain.test_one": tc.test_one,
        "toolchain.env": dict(sorted(tc.env.items())),
        "toolchain.full_build_seconds": tc.full_build_seconds,
        "toolchain.targeted_test_seconds": tc.targeted_test_seconds,
        "selection": {
            "mode": cfg.selection.mode,
            "count": cfg.selection.count,
            "stride": cfg.selection.stride,
            "range": list(cfg.selection.range) if cfg.selection.range else None,
            "paths": list(cfg.selection.paths),
            "exclude_merges": cfg.selection.exclude_merges,
            "max_files_changed": cfg.selection.max_files_changed,
            "max_diff_lines": cfg.selection.max_diff_lines,
        },
        "rubric": {
            "axes": [
                {"key": a.key, "weight": a.weight, "gate": a.gate} for a in cfg.rubric.axes
            ],
            "task_types": list(cfg.rubric.task_types),
        },
        "prompts.pack": str(cfg.prompts.pack) if cfg.prompts.pack else None,
        "prompts.pack_digest": digest_prompt_pack(pack),
        "prompts.overrides": dict(sorted(pack.override_digests.items())),
        "execution.parallel": cfg.execution.parallel,
        "execution.workers": cfg.execution.workers,
        "execution.max_attempts": cfg.execution.max_attempts,
        "execution.timeout_seconds": cfg.execution.timeout_seconds,
        "execution.scratch_dir": cfg.execution.scratch_dir,
        "execution.keep_logs": cfg.execution.keep_logs,
        "execution.sandboxed": cfg.execution.sandboxed,
        "rank": {
            "method": cfg.rank.method,
            "output": cfg.rank.output,
            "max_prompt_tokens": cfg.rank.max_prompt_tokens,
            "max_candidates": cfg.rank.max_candidates,
            "interleaving": {
                "groups": cfg.rank.interleaving.groups,
                "seed": cfg.rank.interleaving.seed,
            },
        },
        "model.base_url": model.get("base_url", ""),
        "model.id": model.get("model", ""),
        "model.api": model.get("api", ""),
    }


@dataclass
class Manifest:
    """A loaded or freshly created run manifest."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    # -- accessors ---------------------------------------------------------

    @property
    def run_id(self) -> str:
        """The run id this manifest belongs to."""
        return str(self.data.get("run", {}).get("id", ""))

    @property
    def fingerprint(self) -> dict[str, Any]:
        """The locked configuration fingerprint."""
        return dict(self.data.get("config", {}))

    @property
    def created_at(self) -> str:
        """ISO-8601 creation timestamp."""
        return str(self.data.get("run", {}).get("created_at", ""))

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        """Write the manifest atomically.

        The temporary file name includes the pid so concurrent writers cannot
        collide. A single shared ``run.json.tmp`` is not safe under a parallel
        launcher: every rank would write the same path and then rename it, the first
        rename would succeed, and the rest would fail with ``FileNotFoundError``
        because their temp file had already been moved out from under them.

        Writers should normally be limited to rank 0 (see :meth:`record_stage`);
        the per-pid name makes the operation safe regardless.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp guarantees a name no other writer holds -- unlike a pid-derived
        # name, which threads within one process would share.
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f"{self.path.name}.tmp."
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.write("\n")
            tmp.replace(self.path)
        finally:
            # A failed write must not leave scratch beside the manifest.
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def record_stage(self, stage: str, info: Mapping[str, Any]) -> None:
        """Append a stage-invocation record and persist immediately.

        Every stage start is logged with its timestamp, host, Slurm job id, and the
        resource environment it ran in (see :func:`resource_environment`), so a run
        assembled from several allocations can be reconstructed after the fact.
        """
        record = {
            "stage": stage,
            "at": _now(),
            "host": socket.gethostname(),
            "slurm_job_id": os.environ.get("SLURM_JOB_ID", ""),
            "resources": resource_environment(),
            **dict(info),
        }
        self.data.setdefault("stages", []).append(record)

        # Only one process persists the manifest. Under a parallel launcher every
        # rank reaches this point, and N ranks appending N records for what is one
        # logical invocation would both corrupt the history and serialize them all
        # through the same file on a shared filesystem. The in-memory record is kept
        # on every rank so nothing downstream sees a different object.
        if launcher_rank() == 0:
            self.save()


def launcher_rank() -> int:
    """Return this process's rank under a parallel launcher, or 0 if not launched.

    Read from the launcher's environment rather than by importing mpi4py: this is
    consulted on paths where MPI may be unavailable or broken, and it must work
    before MPI is initialized.
    """
    for var in ("SLURM_PROCID", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "MV2_COMM_WORLD_RANK"):
        value = os.environ.get(var)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                continue
    return 0


#: Environment variables describing the compute resources a stage was given. These
#: are recorded per stage invocation but deliberately NOT part of the configuration
#: fingerprint -- see :func:`resource_environment`.
RESOURCE_ENV_VARS = (
    "SLURM_JOB_ID",
    "SLURM_JOB_NODELIST",
    "SLURM_CPUS_PER_TASK",
    "SLURM_NTASKS",
    "SLURM_JOB_NUM_NODES",
    "SLURM_MEM_PER_NODE",
    "SLURM_MEM_PER_CPU",
    "BUILD_JOBS",
    "OMP_NUM_THREADS",
)


def resource_environment() -> dict[str, str]:
    """Capture the compute resources a stage invocation was given.

    Why this is recorded but **not** gated on
    ----------------------------------------
    Resource settings -- how many CPUs, how many concurrent workers, how wide each
    build may be -- change how *fast* a run goes, not *what it decides*. Screening
    the same commit with ``BUILD_JOBS=8`` or ``BUILD_JOBS=224`` should produce the
    same verdict, so treating a change as drift would block resumes for no reason:
    you would be unable to continue a sweep on a differently-sized allocation, which
    is precisely what resumability is for.

    They are still worth recording, because they are not entirely inert. A build's
    measured duration depends on its width, and that duration reaches the agents:
    ``toolchain.full_build_seconds`` is rendered into every prompt, and the rubric's
    ``verify_cost`` axis asks agents to weigh verification cost against it. So a
    figure measured with the node's full core count and one measured under a modest
    cgroup will bias that axis differently -- not by changing the rules, but by
    changing what "expensive" looks like.

    That makes resources a *provenance* concern rather than a correctness gate. The
    measured timings themselves already sit in the fingerprint at warning severity,
    so a changed measurement is reported; this records the conditions that produced
    it, so the report can be interpreted.

    Only variables that are set are included, so the record stays honest about what
    was actually known rather than implying defaults.
    """
    return {
        name: os.environ[name] for name in RESOURCE_ENV_VARS if os.environ.get(name)
    }


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create(
    cfg: MineConfig,
    pack: PromptPack,
    model: Mapping[str, str],
    *,
    notes: str = "",
) -> Manifest:
    """Create and write a fresh run manifest.

    Raises
    ------
    ManifestError
        If a manifest already exists for this run. Re-initializing would discard
        the lock that makes existing outputs interpretable, so it must be an
        explicit act (delete the run directory, or choose a new ``run.id``).
    """
    path = cfg.run_dir / MANIFEST_NAME
    if path.exists():
        raise ManifestError(
            f"a run manifest already exists: {path}\n"
            f"Run '{cfg.run_id}' is already initialized; use the stage commands "
            f"to continue it. To start over, choose a different run.id or remove "
            f"{cfg.run_dir}."
        )

    manifest = Manifest(
        path=path,
        data={
            "schema": 1,
            "run": {
                "id": cfg.run_id,
                "created_at": _now(),
                "created_by": _whoami(),
                "created_on": socket.gethostname(),
                "config_source": str(cfg.source_path) if cfg.source_path else "",
                "notes": notes,
            },
            "codebase": {
                "name": cfg.codebase.name,
                "repo": str(cfg.codebase.repo),
                "git": git_identity(cfg.codebase.repo),
            },
            "container": {
                "image": str(cfg.container.image),
                "digest": digest_container(cfg.container.image),
            },
            "tools": tool_versions(),
            "config": config_fingerprint(cfg, pack, model),
            "prompt_overrides_active": sorted(pack.overrides),
            "stages": [],
        },
    )
    manifest.save()
    return manifest


def _whoami() -> str:
    """Return the invoking username, tolerating environments without one."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser can fail in odd containers
        return os.environ.get("USER", "unknown")


def load(cfg: MineConfig) -> Manifest:
    """Load the run manifest for ``cfg``.

    Raises
    ------
    ManifestError
        If it is missing (the run was never initialized) or unparseable.
    """
    path = cfg.run_dir / MANIFEST_NAME
    if not path.is_file():
        # Name the actual config file rather than a placeholder: the message is
        # most often read from a job log, where the reader may not know which
        # config the job used.
        config = cfg.source_path or "<config.yaml>"
        hint = f"abc-mine init --config {config}"
        detail = ""
        if cfg.run_dir.exists():
            # The directory exists but the manifest does not -- so either init was
            # never run, or the manifest was removed while other outputs remain.
            # Worth distinguishing, because the second case means existing results
            # were produced under settings that are no longer recorded anywhere.
            leftovers = sorted(
                child.name for child in cfg.run_dir.iterdir() if child.name != MANIFEST_NAME
            )
            if leftovers:
                detail = (
                    f"\n{cfg.run_dir} exists and contains {', '.join(leftovers[:6])}"
                    f"{', ...' if len(leftovers) > 6 else ''}, but no {MANIFEST_NAME}.\n"
                    "Re-initializing is safe and will not touch those outputs, but note\n"
                    "that whatever produced them was not recorded, so they cannot be\n"
                    "checked for configuration drift against this run."
                )
        raise ManifestError(
            f"run '{cfg.run_id}' is not initialized (no {MANIFEST_NAME} at {path})."
            f"{detail}\n\nInitialize it once, then re-submit:\n    {hint}"
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ManifestError(f"run manifest is not valid JSON: {path} ({exc})") from exc
    return Manifest(path=path, data=data)


def detect_drift(
    manifest: Manifest,
    cfg: MineConfig,
    pack: PromptPack,
    model: Mapping[str, str],
) -> list[Drift]:
    """Compare the current configuration against the locked fingerprint.

    Returns every difference found, each classified by severity. An empty list
    means the current invocation matches the run's lock exactly.
    """
    locked = manifest.fingerprint
    current = config_fingerprint(cfg, pack, model)

    drifts: list[Drift] = []
    for key in sorted(set(locked) | set(current)):
        old, new = locked.get(key), current.get(key)
        if old == new:
            continue
        drifts.append(Drift(key=key, old=old, new=new, severity=_severity(key)))
    return drifts


def _severity(key: str) -> str:
    """Classify a dotted config key by drift severity."""
    for prefix in CRITICAL_KEYS:
        if key == prefix or key.startswith(prefix + "."):
            return "critical"
    for prefix in WARNING_KEYS:
        if key == prefix or key.startswith(prefix + "."):
            return "warning"
    for prefix in EXEMPT_KEYS:
        if key == prefix or key.startswith(prefix + "."):
            return "exempt"
    # Unknown keys are treated as critical: a field added in a future version
    # should fail closed rather than slip through unnoticed.
    return "critical"


def format_drift_report(drifts: Iterable[Drift], *, allowed: bool) -> str:
    """Render a drift report for the terminal, grouped by severity."""
    drifts = list(drifts)
    if not drifts:
        return "Configuration matches the run manifest."

    by_severity: dict[str, list[Drift]] = {}
    for d in drifts:
        by_severity.setdefault(d.severity, []).append(d)

    lines: list[str] = []
    critical = by_severity.get("critical", [])
    if critical:
        lines.append(
            "CONFIGURATION DRIFT (critical) — these change how items are judged,"
        )
        lines.append("so existing and future results would not be comparable:")
        lines += [f"    {d.describe()}" for d in critical]
        lines.append("")
    for severity, title in (
        ("warning", "Configuration changes (execution only; results stay comparable)"),
        ("exempt", "Configuration changes (expected to vary between allocations)"),
    ):
        items = by_severity.get(severity, [])
        if items:
            lines.append(f"{title}:")
            lines += [f"    {d.describe()}" for d in items]
            lines.append("")

    if critical and not allowed:
        lines += [
            "Refusing to continue. Either restore the settings above, start a new",
            "run (a fresh run.id keeps each set of results internally consistent),",
            "or, if you understand that this run's outputs will be a mixture,",
            "re-invoke with --allow-config-drift.",
        ]
    elif critical:
        lines.append(
            "--allow-config-drift given: continuing with a MIXED run. Per-item "
            "records retain their own model id and timestamp."
        )
    return "\n".join(lines).rstrip()


def enforce(
    manifest: Manifest,
    cfg: MineConfig,
    pack: PromptPack,
    model: Mapping[str, str],
    *,
    allow_drift: bool = False,
) -> list[Drift]:
    """Check for drift and raise on unapproved critical drift.

    Also records approved critical drift into the manifest, so a mixed run
    carries permanent evidence of *when* it became mixed and *what* changed —
    which is the whole point of allowing the override at all.

    Raises
    ------
    ManifestError
        If critical drift is present and ``allow_drift`` is False.
    """
    drifts = detect_drift(manifest, cfg, pack, model)
    critical = [d for d in drifts if d.severity == "critical"]

    if critical and not allow_drift:
        raise ManifestError(format_drift_report(drifts, allowed=False))

    if critical:
        accepted = manifest.data.setdefault("accepted_drift", [])
        accepted.append(
            {
                "at": _now(),
                "changes": [
                    {"key": d.key, "from": d.old, "to": d.new} for d in critical
                ],
            }
        )
        # Re-lock to the accepted state so the *next* invocation is compared
        # against what is actually in force now, not the original settings.
        manifest.data["config"] = config_fingerprint(cfg, pack, model)
        # Rank 0 alone persists: N ranks would otherwise append N identical
        # acceptance records for one logical decision.
        if launcher_rank() == 0:
            manifest.save()

    return drifts


# -- attempt tracking --------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """One recorded attempt at one work item."""

    item_id: str
    status: str
    at: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def record_attempt(
    path: Path,
    item_id: str,
    status: str,
    *,
    detail: str = "",
    model_id: str = "",
    **extra: Any,
) -> None:
    """Append one attempt record to an append-only JSON-Lines log.

    This closes a real gap in the original pipeline: a failed screen wrote
    *nothing at all*, so "never attempted" and "attempted and failed five times"
    were indistinguishable, retries were unbounded across resumes, and the
    transcripts had already been deleted. Recording attempts makes failure
    countable (so :func:`attempt_counts` can enforce ``max_attempts``) and
    diagnosable.

    Concurrency: each call writes a single complete line in ``O_APPEND`` mode,
    which POSIX makes atomic for small writes, so parallel workers on a shared
    filesystem never interleave partial lines.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "item_id": item_id,
        "status": status,
        "at": _now(),
        "detail": detail,
        "model_id": model_id,
        **extra,
    }
    with open(path, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_attempts(path: Path) -> list[dict[str, Any]]:
    """Read every attempt record, skipping malformed lines.

    Malformed lines are skipped rather than fatal: a log truncated by a killed
    job should not prevent the resume it exists to support.
    """
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def attempt_counts(path: Path, *, status: str | None = None) -> dict[str, int]:
    """Count attempts per item, optionally filtered to one ``status``."""
    counts: dict[str, int] = {}
    for record in read_attempts(path):
        if status is not None and record.get("status") != status:
            continue
        item = str(record.get("item_id", ""))
        if item:
            counts[item] = counts.get(item, 0) + 1
    return counts


def exhausted_items(path: Path, max_attempts: int) -> dict[str, int]:
    """Return items whose failed-attempt count has reached ``max_attempts``.

    Used by the stages to skip items that keep failing, instead of burning the
    whole budget re-attempting one pathological commit on every resume.
    """
    failures = attempt_counts(path, status="failed")
    return {item: n for item, n in failures.items() if n >= max_attempts}
