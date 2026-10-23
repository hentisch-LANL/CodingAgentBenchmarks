"""Tests for :mod:`abc_mine.repo`, against REAL git repositories.

Nothing here is mocked: the module's whole job is to talk to git correctly, and a
mock of git would test the mock.

The most important test in this file is
:func:`test_cutoff_workspace_has_no_descendant_objects`. The bundle-cutoff
procedure produces a benchmark's *starting state*. If it leaked post-cutoff
objects, every emitted benchmark would silently hand solvers the reference
solution, and no test of the emitted task would reveal it. The negative
assertions (no forbidden sha, no remote) are therefore the point of the exercise,
not decoration.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from abc_mine import repo as R
from abc_mine.config import SelectionConfig


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not available"
)


# -- fixtures ----------------------------------------------------------------


def _run(cwd, *args, env=None):
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    return subprocess.run(
        args, cwd=str(cwd), check=True, text=True, env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "git", "init", "--quiet", "-b", "main")
    _run(path, "git", "config", "user.email", "test@example.invalid")
    _run(path, "git", "config", "user.name", "Test Author")
    _run(path, "git", "config", "commit.gpgsign", "false")
    return path


#: Commit timestamps are pinned and strictly increasing so ``git log`` order is
#: fully deterministic. Without this, several commits made in the same second
#: could be reported in an arbitrary order and the newest-first assertions here
#: would be flaky rather than meaningful.
_CLOCK = {"n": 0}


def _dated_env():
    _CLOCK["n"] += 60
    stamp = f"{1600000000 + _CLOCK['n']} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def _commit(path, message, files: dict[str, str]):
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _run(path, "git", "add", "-A")
    _run(path, "git", "commit", "--quiet", "-m", message, env=_dated_env())
    return _run(path, "git", "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A small linear-plus-merge history.

    Layout (newest last):

    * root                 -- README only (no parent: always dropped)
    * small_a              -- one file, few lines
    * big                  -- 12 files (used for max_files_changed)
    * fat                  -- one file, ~200 lines (used for max_diff_lines)
    * side (on branch)     -- one file
    * merge                -- merge commit (two parents)
    * tip                  -- one file
    """
    path = _init(tmp_path / "src")
    shas = {}
    shas["root"] = _commit(path, "Initial commit", {"README.md": "hello\n"})
    shas["small_a"] = _commit(path, "Add module a", {"a.txt": "a1\na2\n"})
    shas["big"] = _commit(
        path, "Touch many files", {f"many/f{i}.txt": f"content {i}\n" for i in range(12)}
    )
    shas["fat"] = _commit(
        path, "Add a fat file", {"fat.txt": "".join(f"line {i}\n" for i in range(200))}
    )

    # A side branch merged back in, producing a genuine two-parent commit.
    _run(path, "git", "checkout", "--quiet", "-b", "side")
    shas["side"] = _commit(path, "Side work", {"side.txt": "s\n"})
    _run(path, "git", "checkout", "--quiet", "main")
    shas["main_only"] = _commit(path, "Main work", {"main.txt": "m\n"})
    _run(path, "git", "merge", "--quiet", "--no-ff", "-m", "Merge side into main", "side",
         env=_dated_env())
    shas["merge"] = _run(path, "git", "rev-parse", "HEAD").strip()
    shas["tip"] = _commit(path, "Final touch", {"tip.txt": "t\n"})

    return path, shas


# -- require_repo() ----------------------------------------------------------


def test_require_repo_accepts_a_real_repo(repo):
    path, _ = repo
    R.require_repo(path)  # must not raise


def test_require_repo_raises_for_a_non_repo_directory(tmp_path):
    """Every later stage assumes a usable repository; failing here keeps the
    error close to its cause."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(R.GitError, match="not a git repository"):
        R.require_repo(plain)


def test_require_repo_raises_for_a_missing_path(tmp_path):
    with pytest.raises(R.GitError, match="does not exist"):
        R.require_repo(tmp_path / "nope")


def test_git_helper_raises_gitError_on_failure(repo):
    path, _ = repo
    with pytest.raises(R.GitError):
        R.git(path, "rev-parse", "--verify", "no-such-ref")


# -- enumerate_commits() -----------------------------------------------------


def test_enumerate_commits_is_newest_first(repo):
    """Order matters: ``recent`` and ``range`` both index into this list."""
    path, shas = repo
    commits = R.enumerate_commits(path)
    assert commits[0].sha == shas["tip"]
    assert commits[-1].sha == shas["root"]
    assert len(commits) == 8


def test_enumerate_commits_records_subjects(repo):
    path, shas = repo
    by_sha = {c.sha: c for c in R.enumerate_commits(path)}
    assert by_sha[shas["tip"]].subject == "Final touch"
    assert by_sha[shas["root"]].subject == "Initial commit"
    assert by_sha[shas["merge"]].subject == "Merge side into main"


def test_enumerate_commits_records_first_parent_only(repo):
    """A merge's first parent is what "the change this commit introduced" is
    diffed against; keeping only that is deliberate."""
    path, shas = repo
    by_sha = {c.sha: c for c in R.enumerate_commits(path)}
    assert by_sha[shas["tip"]].parent == shas["merge"]
    assert by_sha[shas["small_a"]].parent == shas["root"]
    assert by_sha[shas["merge"]].parent == shas["main_only"]


def test_root_commit_has_no_parent(repo):
    path, shas = repo
    by_sha = {c.sha: c for c in R.enumerate_commits(path)}
    assert by_sha[shas["root"]].parent == ""


def test_merge_flag_is_set_only_for_merges(repo):
    path, shas = repo
    by_sha = {c.sha: c for c in R.enumerate_commits(path)}
    assert by_sha[shas["merge"]].is_merge is True
    for key in ("root", "small_a", "big", "fat", "side", "tip"):
        assert by_sha[shas[key]].is_merge is False


def test_enumerate_commits_records_author_dates(repo):
    path, _ = repo
    for c in R.enumerate_commits(path):
        assert c.author_date  # ISO-8601 from %aI
        assert "T" in c.author_date


def test_enumerate_commits_honours_pathspecs(repo):
    """Pathspec filtering is applied by git itself, so it is far cheaper than
    filtering afterwards."""
    path, shas = repo
    commits = R.enumerate_commits(path, paths=["fat.txt"])
    assert [c.sha for c in commits] == [shas["fat"]]


def test_short_and_item_id(repo):
    path, shas = repo
    commit = R.enumerate_commits(path)[0]
    assert commit.short == shas["tip"][: R.SHORT_SHA_LEN]
    assert commit.item_id == commit.short
    assert len(commit.short) == 12


# -- annotate() --------------------------------------------------------------


def test_annotate_fills_plausible_counts(repo):
    path, shas = repo
    annotated = {c.sha: c for c in R.annotate(path, R.enumerate_commits(path))}

    assert annotated[shas["big"]].files_changed == 12
    assert annotated[shas["big"]].diff_lines == 12  # one added line per file

    assert annotated[shas["small_a"]].files_changed == 1
    assert annotated[shas["small_a"]].diff_lines == 2

    assert annotated[shas["fat"]].files_changed == 1
    assert annotated[shas["fat"]].diff_lines == 200


def test_annotate_preserves_all_other_fields(repo):
    path, _ = repo
    original = R.enumerate_commits(path)
    annotated = R.annotate(path, original)
    assert len(annotated) == len(original)
    for a, o in zip(annotated, original):
        assert (a.sha, a.parent, a.subject, a.author_date, a.is_merge) == (
            o.sha, o.parent, o.subject, o.author_date, o.is_merge
        )


def test_annotate_on_an_empty_list_is_empty(repo):
    path, _ = repo
    assert R.annotate(path, []) == []


# -- select() ----------------------------------------------------------------


def _annotated(path):
    return R.annotate(path, R.enumerate_commits(path))


def test_select_drops_merges_when_configured(repo):
    """A merge's "change" is rarely a coherent authored task and its
    diff-to-first-parent is misleading, so merges are excluded by default."""
    path, shas = repo
    report = R.SelectionReport()
    chosen = R.select(_annotated(path), SelectionConfig(exclude_merges=True), report=report)
    assert shas["merge"] not in {c.sha for c in chosen}
    assert report.dropped_merges == 1


def test_select_keeps_merges_when_asked(repo):
    path, shas = repo
    report = R.SelectionReport()
    chosen = R.select(_annotated(path), SelectionConfig(exclude_merges=False), report=report)
    assert shas["merge"] in {c.sha for c in chosen}
    assert report.dropped_merges == 0


def test_select_always_drops_the_root_commit(repo):
    """A starting state is "the repository at the parent commit", which the root
    commit does not have. It is dropped even with every other filter disabled."""
    path, shas = repo
    report = R.SelectionReport()
    chosen = R.select(_annotated(path), SelectionConfig(exclude_merges=False), report=report)
    assert shas["root"] not in {c.sha for c in chosen}
    assert report.dropped_no_parent == 1


def test_select_drops_oversized_commits_by_file_count(repo):
    """Mechanical pre-filters run in Python; paying an agent to score a
    1000-file commit 0 is pure waste."""
    path, shas = repo
    report = R.SelectionReport()
    chosen = R.select(_annotated(path), SelectionConfig(max_files_changed=5), report=report)
    kept = {c.sha for c in chosen}
    assert shas["big"] not in kept
    assert shas["small_a"] in kept
    assert report.dropped_too_many_files == 1


def test_select_drops_oversized_commits_by_diff_lines(repo):
    path, shas = repo
    report = R.SelectionReport()
    chosen = R.select(_annotated(path), SelectionConfig(max_diff_lines=50), report=report)
    kept = {c.sha for c in chosen}
    assert shas["fat"] not in kept
    assert shas["small_a"] in kept
    assert report.dropped_too_many_lines == 1


def test_select_mode_recent_counts_usable_commits(repo):
    """ORDERING INVARIANT: filters run BEFORE the mode, so ``recent: 2`` means
    "2 usable commits", not "whatever survives of the newest 2". That is what you
    actually want when sizing a validation slice."""
    path, shas = repo
    chosen = R.select(
        _annotated(path),
        SelectionConfig(mode="recent", count=2, exclude_merges=True),
    )
    # Newest-first usable order is: tip, main_only (merge and root filtered out).
    assert [c.sha for c in chosen] == [shas["tip"], shas["main_only"]]
    assert len(chosen) == 2  # a naive "newest 2 then filter" would give 1


def test_select_mode_recent_beyond_the_history_is_clamped(repo):
    path, _ = repo
    chosen = R.select(_annotated(path), SelectionConfig(mode="recent", count=1000))
    assert len(chosen) == 6  # 8 commits - root - merge


def test_select_mode_all_keeps_every_usable_commit(repo):
    path, shas = repo
    report = R.SelectionReport()
    chosen = R.select(_annotated(path), SelectionConfig(mode="all"), report=report)
    assert [c.sha for c in chosen] == [
        shas["tip"], shas["main_only"], shas["side"], shas["fat"], shas["big"],
        shas["small_a"],
    ]
    assert report.dropped_by_mode == 0


def test_select_mode_range_is_inclusive_of_both_endpoints(repo):
    path, shas = repo
    chosen = R.select(
        _annotated(path),
        SelectionConfig(mode="range", range=(shas["fat"], shas["small_a"])),
    )
    assert [c.sha for c in chosen] == [shas["fat"], shas["big"], shas["small_a"]]


def test_select_mode_range_accepts_endpoints_in_either_order(repo):
    path, shas = repo
    forward = R.select(
        _annotated(path),
        SelectionConfig(mode="range", range=(shas["fat"], shas["small_a"])),
    )
    backward = R.select(
        _annotated(path),
        SelectionConfig(mode="range", range=(shas["small_a"], shas["fat"])),
    )
    assert [c.sha for c in forward] == [c.sha for c in backward]


def test_select_mode_range_resolves_abbreviated_shas(repo):
    path, shas = repo
    chosen = R.select(
        _annotated(path),
        SelectionConfig(mode="range", range=(shas["fat"][:8], shas["big"][:8])),
    )
    assert [c.sha for c in chosen] == [shas["fat"], shas["big"]]


def test_select_mode_range_rejects_an_endpoint_that_was_filtered_out(repo):
    """The root commit is always filtered out, so naming it as an endpoint must
    be a clear error rather than a silently empty range."""
    path, shas = repo
    with pytest.raises(R.GitError, match="not among the selected commits"):
        R.select(
            _annotated(path),
            SelectionConfig(mode="range", range=(shas["tip"], shas["root"])),
        )


def test_select_mode_sample_with_stride(repo):
    path, shas = repo
    chosen = R.select(_annotated(path), SelectionConfig(mode="sample", stride=2))
    usable = [c.sha for c in R.select(_annotated(path), SelectionConfig(mode="all"))]
    assert [c.sha for c in chosen] == usable[::2]


def test_select_mode_sample_with_count_caps_the_result(repo):
    path, _ = repo
    chosen = R.select(_annotated(path), SelectionConfig(mode="sample", count=2))
    assert len(chosen) <= 2


def test_select_report_counters_add_up(repo):
    """The funnel must be fully accounted for: "screening 5 of 7" is only
    reassuring if you can see where the other 2 went."""
    path, _ = repo
    report = R.SelectionReport()
    R.select(
        _annotated(path),
        SelectionConfig(mode="recent", count=2, max_files_changed=5, max_diff_lines=50),
        report=report,
    )
    dropped = (
        report.dropped_merges
        + report.dropped_no_parent
        + report.dropped_too_many_files
        + report.dropped_too_many_lines
        + report.dropped_by_mode
    )
    assert report.total == 8
    assert report.selected + dropped == report.total
    assert report.selected == 2


def test_select_report_describe_mentions_each_nonzero_filter(repo):
    path, _ = repo
    report = R.SelectionReport()
    R.select(
        _annotated(path),
        SelectionConfig(max_files_changed=5, max_diff_lines=50),
        report=report,
    )
    text = report.describe()
    assert "Enumerated 8" in text
    assert "merge commits" in text
    assert "root commit" in text
    assert "max_files_changed" in text
    assert "max_diff_lines" in text


def test_select_on_an_empty_input_is_empty():
    assert R.select([], SelectionConfig(mode="recent", count=5)) == []


def test_select_unknown_mode_raises():
    """``SelectionConfig`` normally guards this, but ``select`` accepts any
    duck-typed object, so it must fail loudly too."""

    class Loose:
        mode = "telepathy"
        exclude_merges = False
        max_files_changed = None
        max_diff_lines = None
        count = None
        stride = None
        range = None

    commit = R.Commit(sha="a" * 40, parent="b" * 40, subject="x")
    with pytest.raises(R.GitError, match="unknown selection mode"):
        R.select([commit], Loose())


# -- per-commit materials ----------------------------------------------------


def test_commit_diff_returns_the_full_diff_when_untruncated(repo):
    path, shas = repo
    text, truncated = R.commit_diff(path, shas["small_a"])
    assert truncated is False
    assert "a.txt" in text
    assert "a1" in text


def test_commit_diff_truncates_and_reports_it(repo):
    """TRUTHFULNESS: truncation is reported rather than hidden, so the prompt can
    tell the agent its view is partial. An agent that believes it saw a whole
    diff will confidently mis-score a commit whose substance was cut off."""
    path, shas = repo
    text, truncated = R.commit_diff(path, shas["fat"], max_bytes=200)
    assert truncated is True
    assert "diff truncated at 200 bytes" in text
    full, _ = R.commit_diff(path, shas["fat"])
    assert len(text) < len(full)


def test_commit_diff_max_bytes_larger_than_the_diff_is_not_truncated(repo):
    path, shas = repo
    _, truncated = R.commit_diff(path, shas["small_a"], max_bytes=10_000_000)
    assert truncated is False


def test_commit_message_returns_the_full_body(repo):
    """The build stage needs the message verbatim: it calibrates how much a
    constructed task prompt may reveal."""
    path = repo[0]
    sha = _commit(path, "Subject line\n\nBody paragraph one.\n\nBody two.\n", {"z.txt": "z\n"})
    body = R.commit_message(path, sha)
    assert body.splitlines()[0] == "Subject line"
    assert "Body paragraph one." in body
    assert "Body two." in body


def test_touched_files_lists_paths(repo):
    path, shas = repo
    assert R.touched_files(path, shas["small_a"]) == ["a.txt"]
    assert sorted(R.touched_files(path, shas["big"])) == sorted(
        f"many/f{i}.txt" for i in range(12)
    )


def test_matches_any():
    assert R.matches_any(["src/a.f90"], ["src/*.f90"]) is True
    assert R.matches_any(["src/a.f90"], ["docs/*"]) is False
    assert R.matches_any([], ["*"]) is False


# -- worktree() --------------------------------------------------------------


def test_worktree_yields_a_tree_at_the_requested_commit_and_cleans_up(repo, tmp_path):
    """Each work item gets its own worktree so parallel workers never contend,
    and cleanup must happen even though agents leave build products behind."""
    path, shas = repo
    dest = tmp_path / "wt"
    with R.worktree(path, shas["small_a"], dest) as tree:
        assert tree == dest
        assert (tree / "a.txt").read_text() == "a1\na2\n"
        assert not (tree / "tip.txt").exists()  # really at small_a, not HEAD
        head = _run(tree, "git", "rev-parse", "HEAD").strip()
        assert head == shas["small_a"]
    assert not dest.exists()


def test_worktree_cleanup_survives_leftover_files(repo, tmp_path):
    path, shas = repo
    dest = tmp_path / "wt"
    with R.worktree(path, shas["tip"], dest) as tree:
        (tree / "build-artifact.o").write_text("junk\n")
    assert not dest.exists()
    assert "wt" not in _run(path, "git", "worktree", "list")


def test_worktree_reuses_a_stale_destination(repo, tmp_path):
    path, shas = repo
    dest = tmp_path / "wt"
    dest.mkdir()
    (dest / "stale.txt").write_text("left over\n")
    with R.worktree(path, shas["tip"], dest) as tree:
        assert (tree / "tip.txt").exists()
    assert not dest.exists()


def test_checkout_detached_moves_an_existing_worktree(repo, tmp_path):
    """The build stage uses this to materialize the post-change tree for its
    PASS-side polarity check."""
    path, shas = repo
    dest = tmp_path / "wt"
    with R.worktree(path, shas["small_a"], dest) as tree:
        R.checkout_detached(tree, shas["tip"])
        assert _run(tree, "git", "rev-parse", "HEAD").strip() == shas["tip"]
        assert (tree / "tip.txt").exists()


# -- build_cutoff_workspace() -----------------------------------------------


def test_cutoff_workspace_head_is_exactly_the_cutoff(repo, tmp_path):
    path, shas = repo
    dest = tmp_path / "ws"
    result = R.build_cutoff_workspace(path, shas["fat"], dest)
    assert result.cutoff_sha == shas["fat"]
    assert _run(dest, "git", "rev-parse", "HEAD").strip() == shas["fat"]
    assert (dest / "fat.txt").exists()


def test_cutoff_workspace_git_is_a_real_directory(repo, tmp_path):
    """``.git`` must be a real DIRECTORY, not a worktree pointer file. abc-bench
    copies the workspace verbatim at grading time, and a pointer file would
    dangle the moment it was copied."""
    path, shas = repo
    dest = tmp_path / "ws"
    R.build_cutoff_workspace(path, shas["fat"], dest)
    git_path = dest / ".git"
    assert git_path.exists()
    assert git_path.is_dir(), f".git must be a directory, is a file: {git_path}"
    assert (git_path / "objects").is_dir()


def test_cutoff_workspace_has_no_remote(repo, tmp_path):
    """LEAK PREVENTION: with no remote configured, later history cannot simply be
    fetched back by a solver (or by a well-meaning build_env.sh)."""
    path, shas = repo
    dest = tmp_path / "ws"
    R.build_cutoff_workspace(path, shas["fat"], dest)
    assert _run(dest, "git", "remote").strip() == ""


def test_cutoff_workspace_retains_real_ancestor_history(repo, tmp_path):
    """The agent solving the task must see a real repository, not a history-less
    snapshot: ``git log`` / ``blame`` / ``show`` have to behave normally."""
    path, shas = repo
    dest = tmp_path / "ws"
    R.build_cutoff_workspace(path, shas["fat"], dest)
    log = _run(dest, "git", "log", "--format=%H").split()
    assert len(log) > 1
    assert log[0] == shas["fat"]
    assert shas["root"] in log
    assert shas["small_a"] in log
    # blame works, which requires real ancestor objects
    assert _run(dest, "git", "blame", "--porcelain", "README.md")


def test_cutoff_workspace_has_no_descendant_objects(repo, tmp_path):
    """THE CENTRAL SAFETY TEST. A workspace that leaked post-cutoff objects would
    silently hand solvers the reference solution, and nothing about the emitted
    benchmark would reveal it. ``git cat-file -e`` is asked directly, because the
    object may be present but unreachable — which ``git log`` would miss."""
    path, shas = repo
    dest = tmp_path / "ws"
    forbidden = shas["tip"]
    result = R.build_cutoff_workspace(path, shas["fat"], dest, forbidden_sha=forbidden)

    probe = subprocess.run(
        ["git", "-C", str(dest), "cat-file", "-e", f"{forbidden}^{{commit}}"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert probe.returncode != 0, "the descendant commit leaked into the starting state"

    # Every descendant is absent, not just the declared forbidden one.
    for key in ("tip", "merge", "main_only", "side"):
        rc = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", f"{shas[key]}^{{commit}}"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode
        assert rc != 0, f"descendant {key} leaked into the starting state"

    # The file introduced after the cutoff must not exist in the tree either.
    assert not (dest / "tip.txt").exists()
    assert not (dest / "side.txt").exists()

    assert result.object_count > 0


def test_cutoff_workspace_object_count_is_recorded(repo, tmp_path):
    """The count is written into the manifest as evidence the cutoff was exact."""
    path, shas = repo
    result = R.build_cutoff_workspace(path, shas["fat"], tmp_path / "ws")
    assert result.object_count > 0
    listed = _run(
        tmp_path / "ws", "git", "cat-file", "--batch-all-objects",
        "--batch-check=%(objectname)",
    ).split()
    assert result.object_count == len(set(listed))


def test_cutoff_workspace_passes_fsck(repo, tmp_path):
    path, shas = repo
    dest = tmp_path / "ws"
    R.build_cutoff_workspace(path, shas["fat"], dest)
    assert subprocess.run(
        ["git", "-C", str(dest), "fsck", "--full", "--no-reflogs"],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def test_cutoff_workspace_refuses_an_existing_destination(repo, tmp_path):
    """Refusing rather than overwriting: silently reusing a directory could mix a
    previous run's objects into the starting state."""
    path, shas = repo
    dest = tmp_path / "ws"
    dest.mkdir()
    with pytest.raises(R.CutoffError, match="destination already exists"):
        R.build_cutoff_workspace(path, shas["fat"], dest)


def test_cutoff_workspace_refuses_an_unresolvable_cutoff(repo, tmp_path):
    path, _ = repo
    with pytest.raises(R.CutoffError, match="cannot resolve cutoff revision"):
        R.build_cutoff_workspace(path, "deadbeefdeadbeef", tmp_path / "ws")


def test_cutoff_workspace_refuses_a_non_repo_source(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(R.CutoffError):
        R.build_cutoff_workspace(plain, "HEAD", tmp_path / "ws")


def test_cutoff_workspace_from_a_parent_matches_the_change_commits_parent(repo, tmp_path):
    """The realistic call shape: build the starting state at the *parent* of the
    change commit, and assert the change itself is absent."""
    path, shas = repo
    change = shas["fat"]
    parent = shas["big"]
    dest = tmp_path / "ws"
    result = R.build_cutoff_workspace(path, parent, dest, forbidden_sha=change)
    assert result.cutoff_sha == parent
    assert not (dest / "fat.txt").exists()
    assert (dest / "many" / "f0.txt").exists()


def test_cutoff_workspace_leaves_no_temporary_ref_behind(repo, tmp_path):
    """The private ref namespace used for bundling must be cleaned up, or a
    second run in the same repository would refuse to start."""
    path, shas = repo
    R.build_cutoff_workspace(path, shas["fat"], tmp_path / "ws1")
    refs = _run(path, "git", "for-each-ref", "--format=%(refname)")
    assert "refs/abc-mine" not in refs
    # Proving it: a second cutoff in the same repo must also succeed.
    R.build_cutoff_workspace(path, shas["big"], tmp_path / "ws2")
