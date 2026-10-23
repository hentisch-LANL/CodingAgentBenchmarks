"""Task-group discovery and resolution for ABC Bench.

A *task group* is the discrete, installable unit of benchmarks. It is a
directory (or a squashfs image of one) that contains a ``group.yaml``, an
optional ``group_prompt.md``, a ``Dockerfile.jinja2`` describing the group's
runtime container layer, and one subdirectory per task.

Groups are discovered from a prioritized set of *search roots*. A search root
is a directory that *contains* task groups (``<root>/<group>/`` or
``<root>/<group>.sqfs``), analogous to a directory on the shell ``PATH``
containing executables. Priority, highest first:

1. an explicit ``--task-dir`` supplied on the command line,
2. the colon-separated ``TASK_PATH`` environment variable (left-to-right), and
3. the installed root ``~/.abc-bench/tasks`` (populated by ``abc-bench install``).

The first root that provides a given group id wins; lower-priority roots that
also define it are shadowed (with a warning). Squashfs groups are materialized
to a temporary directory on first resolution and reused within the process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import squashfs

#: Environment variable holding colon-separated task-group search roots.
TASK_PATH_ENV = "TASK_PATH"

#: Installed task-group root, populated by ``abc-bench install``.
INSTALLED_TASKS_ROOT = Path("~/.abc-bench/tasks")


def installed_tasks_root() -> Path:
    """Return the resolved installed task-group root."""
    return INSTALLED_TASKS_ROOT.expanduser()


def _split_task_path(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(entry).expanduser() for entry in value.split(os.pathsep) if entry]


class GroupResolver:
    """Resolve task-group ids to concrete group directories.

    Parameters
    ----------
    task_dir : Path, optional
        Highest-priority search root from the CLI ``--task-dir`` flag.
    task_path : str, optional
        Colon-separated search roots. Defaults to the ``TASK_PATH`` environment
        variable.
    installed_root : Path, optional
        Lowest-priority search root. Defaults to ``~/.abc-bench/tasks``.
    warn : bool, optional
        Emit a warning when a group id is shadowed by a higher-priority root.
    """

    def __init__(
        self,
        task_dir: Path | None = None,
        task_path: str | None = None,
        installed_root: Path | None = None,
        warn: bool = True,
    ):
        if task_path is None:
            task_path = os.environ.get(TASK_PATH_ENV)
        if installed_root is None:
            installed_root = installed_tasks_root()

        roots: list[Path] = []
        if task_dir is not None:
            roots.append(Path(task_dir).expanduser())
        roots.extend(_split_task_path(task_path))
        roots.append(Path(installed_root).expanduser())

        # De-duplicate while preserving priority order.
        seen: set[Path] = set()
        self.search_roots: list[Path] = []
        for root in roots:
            resolved = root.resolve() if root.exists() else root
            if resolved in seen:
                continue
            seen.add(resolved)
            self.search_roots.append(root)

        self.warn = warn
        # Cache of resolved (materialized) group directories by group id.
        self._resolved: dict[str, Path] = {}

    # -- discovery ------------------------------------------------------------

    def _group_entries_in(self, root: Path) -> dict[str, Path]:
        """Map group id -> source path (dir or .sqfs) for one search root."""
        entries: dict[str, Path] = {}
        if not root.is_dir():
            return entries
        for child in sorted(root.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir() and (child / "group.yaml").is_file():
                entries.setdefault(child.name, child)
            elif squashfs.is_squashfs_artifact(child):
                group_id = child.name[: -len(squashfs.SQUASHFS_SUFFIX)]
                entries.setdefault(group_id, child)
        return entries

    def _source_for(self, group_id: str) -> Path | None:
        """Return the winning source path for a group id, honoring priority."""
        winner: Path | None = None
        shadowed: list[Path] = []
        for root in self.search_roots:
            source = self._group_entries_in(root).get(group_id)
            if source is None:
                continue
            if winner is None:
                winner = source
            else:
                shadowed.append(source)
        if winner is not None and shadowed and self.warn:
            shadow_list = ", ".join(str(path) for path in shadowed)
            print(
                f"warning: task group {group_id!r} resolved to {winner}; "
                f"shadowing {shadow_list}",
                file=sys.stderr,
            )
        return winner

    def list_group_ids(self) -> list[str]:
        """Return all discoverable group ids across search roots (deduped)."""
        group_ids: list[str] = []
        seen: set[str] = set()
        for root in self.search_roots:
            for group_id in self._group_entries_in(root):
                if group_id not in seen:
                    seen.add(group_id)
                    group_ids.append(group_id)
        return sorted(group_ids)

    # -- resolution -----------------------------------------------------------

    def resolve_group_dir(self, group_id: str) -> Path:
        """Resolve a group id to a concrete, directory-backed group path.

        Squashfs images are materialized to a temporary directory (cached for
        the lifetime of the process).

        Raises
        ------
        FileNotFoundError
            If no search root provides the group.
        """
        if group_id in self._resolved:
            return self._resolved[group_id]

        source = self._source_for(group_id)
        if source is None:
            roots = ", ".join(str(root) for root in self.search_roots) or "<none>"
            raise FileNotFoundError(
                f"task group {group_id!r} not found in any search root ({roots})"
            )

        if squashfs.is_squashfs_artifact(source):
            group_dir = squashfs.materialize_group(source)
        else:
            group_dir = source.resolve()

        self._resolved[group_id] = group_dir
        return group_dir

    def has_group(self, group_id: str) -> bool:
        """Return True if any search root provides the group."""
        return self._source_for(group_id) is not None
