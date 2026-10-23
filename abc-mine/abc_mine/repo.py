"""Git operations: commit enumeration, selection, worktrees, starting states.

Three responsibilities:

1. **Enumerate and select** commits from an arbitrary repository, with mechanical
   pre-filters that drop obviously-unusable commits before any model sees them.
2. **Isolate** each commit in its own worktree, so parallel workers never contend
   over one working tree.
3. **Build a starting state** — a repository that stops dead at a chosen commit —
   via the bundle-cutoff procedure described in :func:`build_cutoff_workspace`.

The bundle-cutoff procedure is the correctness keystone of the whole tool and is
carried over essentially unchanged from the original Truchas pipeline, where it
was validated against a real 1600-commit history. It is fully deterministic and
involves no agent.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

#: Length of the short hash used for item ids and summary filenames. 12 hex
#: characters is the conventional unambiguous abbreviation for repositories of
#: this size; it is also what the legacy pipeline used, so its artifacts remain
#: recognizable.
SHORT_SHA_LEN = 12


class GitError(RuntimeError):
    """Raised when a git invocation fails or a repository is unusable."""


class CutoffError(GitError):
    """Raised when the bundle-cutoff procedure or an integrity check fails."""


@dataclass(frozen=True)
class Commit:
    """One commit, as a unit of work.

    Attributes
    ----------
    sha : str
        Full 40-hex commit SHA.
    parent : str
        First-parent SHA; empty for the root commit. Only the first parent is
        retained: for a merge, the diff against the first parent is what
        corresponds to "the change this commit introduced".
    subject : str
        Commit subject line.
    author_date : str
        Author date, ISO-8601, for provenance and reporting.
    is_merge : bool
        Whether the commit has more than one parent.
    files_changed : int
        Number of files touched (populated by :func:`annotate`).
    diff_lines : int
        Added + deleted line count (populated by :func:`annotate`).
    """

    sha: str
    parent: str
    subject: str
    author_date: str = ""
    is_merge: bool = False
    files_changed: int = 0
    diff_lines: int = 0

    @property
    def short(self) -> str:
        """The abbreviated SHA used as this item's id."""
        return self.sha[:SHORT_SHA_LEN]

    @property
    def item_id(self) -> str:
        """Stable work-item identifier (the short SHA)."""
        return self.short


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run ``git -C repo <args>`` and return stdout.

    Raises
    ------
    GitError
        If ``check`` and the command exits non-zero. The message includes stderr,
        because a bare "git failed" is useless when debugging a sweep.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise GitError(
            f"git {' '.join(args)} failed in {repo} "
            f"(exit {exc.returncode}): {(exc.stderr or '').strip()}"
        ) from exc
    except OSError as exc:
        raise GitError(f"could not execute git in {repo}: {exc}") from exc
    return result.stdout


def require_repo(repo: Path) -> None:
    """Validate that ``repo`` is a complete, non-shallow git repository.

    Shallowness is checked explicitly because it breaks the cutoff procedure in a
    way that would otherwise surface much later, as a confusing integrity-check
    failure deep inside the build stage.
    """
    repo = Path(repo)
    if not repo.is_dir():
        raise GitError(f"repository path does not exist: {repo}")
    try:
        git(repo, "rev-parse", "--git-dir")
    except GitError as exc:
        raise GitError(f"not a git repository: {repo}") from exc
    if git(repo, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise GitError(
            f"repository is shallow: {repo}\n"
            "Full history is required to construct benchmark starting states. "
            "Run `git fetch --unshallow` in that checkout."
        )


# -- enumeration and selection ----------------------------------------------


def enumerate_commits(repo: Path, *, paths: Sequence[str] = ()) -> list[Commit]:
    """Enumerate commits, newest first.

    Parameters
    ----------
    repo : Path
        The repository to read.
    paths : sequence of str, optional
        Pathspecs; when given, only commits touching them are returned. Applied
        by git itself, so it is far cheaper than filtering afterwards.

    Notes
    -----
    Fields are read from a single NUL-delimited ``git log``, which is robust to
    subjects containing arbitrary punctuation, quotes, or newline-adjacent
    whitespace.
    """
    require_repo(repo)
    args = ["log", "--format=%H%x00%P%x00%s%x00%aI"]
    if paths:
        args += ["--", *paths]
    out = git(repo, *args)

    commits: list[Commit] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = line.split("\x00")
        if len(fields) < 2:
            continue
        sha = fields[0].strip()
        parents = fields[1].split() if len(fields) > 1 else []
        commits.append(
            Commit(
                sha=sha,
                parent=parents[0] if parents else "",
                subject=fields[2] if len(fields) > 2 else "",
                author_date=fields[3].strip() if len(fields) > 3 else "",
                is_merge=len(parents) > 1,
            )
        )
    return commits


def annotate(repo: Path, commits: Sequence[Commit]) -> list[Commit]:
    """Attach ``files_changed`` / ``diff_lines`` to each commit.

    Uses one ``git log --numstat`` pass over the whole set rather than one
    ``diff-tree`` per commit: on a history of a few thousand commits that is the
    difference between well under a second and tens of seconds.
    """
    if not commits:
        return []

    stats: dict[str, tuple[int, int]] = {}
    out = git(repo, "log", "--format=@@@%H", "--numstat", "--no-renames")
    current: str | None = None
    files = added = removed = 0
    for line in out.splitlines():
        if line.startswith("@@@"):
            if current:
                stats[current] = (files, added + removed)
            current = line[3:].strip()
            files = added = removed = 0
            continue
        if not line.strip() or current is None:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            files += 1
            # Binary files report '-' for both counts.
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                removed += int(parts[1])
    if current:
        stats[current] = (files, added + removed)

    annotated: list[Commit] = []
    for c in commits:
        n_files, n_lines = stats.get(c.sha, (0, 0))
        annotated.append(
            Commit(
                sha=c.sha,
                parent=c.parent,
                subject=c.subject,
                author_date=c.author_date,
                is_merge=c.is_merge,
                files_changed=n_files,
                diff_lines=n_lines,
            )
        )
    return annotated


@dataclass
class SelectionReport:
    """What selection did, and why — for the terminal and the manifest.

    Making each filter's toll explicit matters: "screening 180 of 1633 commits"
    is only reassuring if you can see that 1,200 were dropped as merges rather
    than silently lost to a misconfigured pathspec.
    """

    total: int = 0
    selected: int = 0
    dropped_merges: int = 0
    dropped_no_parent: int = 0
    dropped_too_many_files: int = 0
    dropped_too_many_lines: int = 0
    dropped_by_mode: int = 0

    def describe(self) -> str:
        """Render a human-readable summary of the funnel."""
        lines = [f"Enumerated {self.total} commit(s); selected {self.selected}."]
        for label, count in (
            ("merge commits", self.dropped_merges),
            ("root commit (no parent)", self.dropped_no_parent),
            ("exceeded max_files_changed", self.dropped_too_many_files),
            ("exceeded max_diff_lines", self.dropped_too_many_lines),
            (f"outside selection window", self.dropped_by_mode),
        ):
            if count:
                lines.append(f"  dropped {count:>6} — {label}")
        return "\n".join(lines)


def select(
    commits: Sequence[Commit],
    selection: Any,
    *,
    report: SelectionReport | None = None,
) -> list[Commit]:
    """Apply mechanical filters and the selection mode, preserving order.

    Filters run **before** the mode, so ``recent: 100`` means "100 usable
    commits", not "whatever survives of the newest 100" — which is what you
    actually want when sizing a validation slice.

    Commits without a parent are always dropped: a starting state is defined as
    "the repository at the parent commit", which the root commit does not have.
    """
    rep = report if report is not None else SelectionReport()
    rep.total = len(commits)

    kept: list[Commit] = []
    for c in commits:
        if selection.exclude_merges and c.is_merge:
            rep.dropped_merges += 1
            continue
        if not c.parent:
            rep.dropped_no_parent += 1
            continue
        if selection.max_files_changed is not None and c.files_changed > selection.max_files_changed:
            rep.dropped_too_many_files += 1
            continue
        if selection.max_diff_lines is not None and c.diff_lines > selection.max_diff_lines:
            rep.dropped_too_many_lines += 1
            continue
        kept.append(c)

    before_mode = len(kept)
    chosen = _apply_mode(kept, selection)
    rep.dropped_by_mode = before_mode - len(chosen)
    rep.selected = len(chosen)
    return chosen


def _apply_mode(commits: list[Commit], selection: Any) -> list[Commit]:
    """Apply the selection mode to an already-filtered, newest-first list."""
    mode = selection.mode
    n = len(commits)
    if n == 0 or mode == "all":
        return list(commits)

    if mode == "recent":
        return commits[: max(0, min(int(selection.count or n), n))]

    if mode == "sample":
        count = int(selection.count or n) or n
        stride = int(selection.stride) if selection.stride else max(1, n // max(1, count))
        sampled = [commits[i] for i in range(0, n, max(1, stride))]
        return sampled[:count] if selection.count else sampled

    if mode == "range":
        a, b = selection.range
        index = {c.sha: i for i, c in enumerate(commits)}

        def _resolve(ref: str) -> int:
            if ref in index:
                return index[ref]
            matches = [i for sha, i in index.items() if sha.startswith(ref)]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise GitError(
                    f"selection.range endpoint {ref!r} is not among the selected "
                    "commits (it may have been filtered out, or may not exist)"
                )
            raise GitError(f"selection.range endpoint {ref!r} is ambiguous")

        i, j = _resolve(a), _resolve(b)
        lo, hi = min(i, j), max(i, j)
        return commits[lo : hi + 1]

    raise GitError(f"unknown selection mode: {mode!r}")


# -- per-commit materials ----------------------------------------------------


def commit_diff(repo: Path, sha: str, *, max_bytes: int | None = None) -> tuple[str, bool]:
    """Return ``(diff_text, truncated)`` for the change ``sha`` introduced.

    Truncation is reported rather than hidden so the prompt can tell the agent its
    view is partial — an agent that believes it saw a whole diff may confidently
    mis-score a commit whose substance was cut off.
    """
    text = git(repo, "show", "--format=medium", sha)
    if max_bytes is None:
        return text, False
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    clipped = raw[:max_bytes].decode("utf-8", errors="replace")
    return clipped + f"\n\n[... diff truncated at {max_bytes} bytes ...]\n", True


def commit_message(repo: Path, sha: str) -> str:
    """Return the full commit message body for ``sha``.

    The message is the calibration point for how much a constructed task prompt
    should reveal, so the build stage needs it verbatim rather than paraphrased.
    """
    return git(repo, "show", "-s", "--format=%B", sha).strip()


def touched_files(repo: Path, sha: str) -> list[str]:
    """Return the paths touched by ``sha``."""
    out = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return [line.strip() for line in out.splitlines() if line.strip()]


def matches_any(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    """Return True if any path matches any glob pattern."""
    return any(fnmatch.fnmatch(p, pat) for p in paths for pat in patterns)


@contextmanager
def worktree(repo: Path, sha: str, dest: Path, *, detach: bool = True) -> Iterator[Path]:
    """Yield a worktree of ``repo`` checked out at ``sha``, then remove it.

    Each work item gets its own worktree so parallel workers never fight over one
    working tree (and so an agent that dirties its tree cannot corrupt another's).
    Stale registrations at ``dest`` are cleared first, and cleanup is best-effort
    on the way out — an agent may well leave build products behind, and that must
    not fail the job that just succeeded.
    """
    repo, dest = Path(repo), Path(dest)

    if dest.exists():
        _quiet(repo, "worktree", "remove", "--force", str(dest))
        subprocess.run(["rm", "-rf", str(dest)], check=False)
    _quiet(repo, "worktree", "prune")

    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if detach:
        args.append("--detach")
    git(repo, *args, str(dest), sha)
    try:
        yield dest
    finally:
        _quiet(repo, "worktree", "remove", "--force", str(dest))
        if dest.exists():
            subprocess.run(["rm", "-rf", str(dest)], check=False)
        _quiet(repo, "worktree", "prune")


def _quiet(repo: Path, *args: str) -> None:
    """Run a git command, ignoring failure and output."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def checkout_detached(tree: Path, sha: str) -> None:
    """Force-checkout ``sha`` detached in an existing worktree.

    Discards local changes, so the result is a clean snapshot. Used by the build
    stage to materialize the post-change tree for its PASS-side polarity check.
    """
    git(Path(tree), "checkout", "--detach", "--force", sha)


# -- deterministic starting state (bundle cutoff) ----------------------------


@dataclass(frozen=True)
class CutoffResult:
    """Outcome of :func:`build_cutoff_workspace`.

    Attributes
    ----------
    cutoff_sha : str
        The resolved commit the workspace stops at (its ``HEAD``).
    object_count : int
        Objects physically present in the result's object database — recorded in
        the manifest as evidence the cutoff was exact.
    """

    cutoff_sha: str
    object_count: int


def build_cutoff_workspace(
    repo: Path,
    cutoff: str,
    dest: Path,
    *,
    forbidden_sha: str | None = None,
) -> CutoffResult:
    """Build a fresh repository at ``dest`` whose history stops at ``cutoff``.

    This is how a benchmark's starting state is produced, and it is deliberately
    **agent-free and deterministic**: an agent asked to "remove the change" would
    introduce exactly the kind of subtle, unauditable variation a benchmark
    cannot tolerate.

    Procedure: a self-contained bundle of precisely the objects reachable from
    ``cutoff`` is created from the source, then fetched into a brand-new
    ``git init`` repository. The result has

    - working tree and ``HEAD`` at ``cutoff``;
    - genuine ancestor history, so ``git log`` / ``blame`` / ``show`` behave
      normally — an agent solving the task sees a real repository, not a
      history-less snapshot;
    - **no** descendant objects, so the reference solution cannot be recovered
      from the workspace;
    - **no** configured remote, so later history cannot be fetched back;
    - a real ``.git`` **directory** — unlike a ``git worktree``, whose ``.git`` is
      a pointer file that dangles once copied, which matters because abc-bench
      copies the workspace verbatim.

    Every one of those properties is then *asserted* below rather than assumed.
    The negative checks are the important ones: a workspace that leaked
    post-cutoff objects would silently hand solvers the answer, and no test of the
    emitted benchmark would reveal it.

    Parameters
    ----------
    repo : Path
        Trusted, complete source repository.
    cutoff : str
        Revision to stop at (typically the parent of the change commit).
    dest : Path
        Destination directory; must not already exist.
    forbidden_sha : str, optional
        A known descendant (normally the change commit) asserted to be absent.

    Raises
    ------
    CutoffError
        If the source is shallow, ``dest`` exists, or any integrity check fails.
    """
    repo, dest = Path(repo), Path(dest)

    try:
        require_repo(repo)
    except GitError as exc:
        raise CutoffError(str(exc)) from exc

    try:
        cutoff_oid = git(repo, "rev-parse", "--verify", f"{cutoff}^{{commit}}").strip()
    except GitError as exc:
        raise CutoffError(f"cannot resolve cutoff revision to a commit: {cutoff!r}") from exc

    if dest.exists():
        raise CutoffError(f"destination already exists: {dest}")

    # A private ref namespace keyed by PID: `git bundle create` requires a named
    # ref (bundling a bare OID can be refused as "empty"), and parallel workers
    # must not collide on it.
    tmpref = f"refs/abc-mine/cutoff-{os.getpid()}"

    with tempfile.TemporaryDirectory(prefix="lbm-cutoff-") as td:
        bundle = Path(td) / "cutoff.bundle"

        expected_raw = git(repo, "rev-list", "--objects", cutoff_oid)
        expected = sorted(
            {line.split(" ", 1)[0] for line in expected_raw.splitlines() if line.strip()}
        )

        if subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", tmpref],
            check=False,
        ).returncode == 0:
            raise CutoffError(f"temporary ref already exists: {tmpref}")

        try:
            git(repo, "update-ref", tmpref, cutoff_oid)
            git(repo, "bundle", "create", str(bundle), tmpref)
        finally:
            _quiet(repo, "update-ref", "-d", tmpref)

        dest.parent.mkdir(parents=True, exist_ok=True)
        git(dest.parent, "init", "--quiet", str(dest))
        git(dest, "fetch", "--quiet", str(bundle), tmpref)
        try:
            git(dest, "switch", "--quiet", "--create", "main", "FETCH_HEAD")
        except GitError:
            # `git switch` predates some deployed gits; fall back.
            git(dest, "checkout", "--quiet", "-b", "main", "FETCH_HEAD")

    # -- integrity checks (all fail hard) ---------------------------------
    head = git(dest, "rev-parse", "HEAD").strip()
    if head != cutoff_oid:
        raise CutoffError(f"HEAD {head} != requested cutoff {cutoff_oid}")

    fsck = subprocess.run(
        ["git", "-C", str(dest), "fsck", "--full", "--no-reflogs"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if fsck.returncode != 0:
        raise CutoffError(f"git fsck failed on the cutoff workspace: {fsck.stderr.strip()}")

    if git(dest, "remote").strip():
        raise CutoffError(f"cutoff workspace unexpectedly has a remote: {dest}")

    # Enumerate every object physically present — stronger than `git log --all`,
    # which would miss unreachable-but-present descendant objects.
    actual_raw = git(dest, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    actual = sorted({line.strip() for line in actual_raw.splitlines() if line.strip()})
    if actual != expected:
        raise CutoffError(
            "cutoff workspace object database is not the exact expected set "
            f"(expected {len(expected)} objects, found {len(actual)})"
        )

    if forbidden_sha:
        present = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", f"{forbidden_sha}^{{commit}}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if present.returncode == 0:
            raise CutoffError(
                f"cutoff workspace contains the forbidden descendant commit "
                f"{forbidden_sha}; the starting state would leak the solution"
            )

    return CutoffResult(cutoff_sha=cutoff_oid, object_count=len(actual))
