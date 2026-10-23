"""Task management for ABC Bench.

Provides the Task class for loading and validating grouped benchmark task
definitions. Tasks live inside *task groups*, which are discovered and resolved
by :class:`abc_bench.groups.GroupResolver` (see :mod:`abc_bench.groups`).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from .groups import GroupResolver

#: Default filename for a task group's runtime container template.
DEFAULT_CONTAINER_TEMPLATE = "Dockerfile.jinja2"


@dataclass(frozen=True)
class TaskGroup:
    """Represents a benchmark task group."""

    group_id: str
    group_dir: Path
    metadata: dict

    @property
    def canonical_name(self) -> str:
        """Return the group identifier used in task paths."""
        return self.group_id


class Task:
    """Represents a ABC Bench task.

    A task belongs to a task group and defines a benchmark problem with a
    workspace for the agent, a prompt describing the problem, and hidden
    verification tests.

    Attributes
    ----------
    task_id : str
        Short task identifier (directory name under a task group)
    group_id : str
        Task group identifier
    task_ref : str
        Canonical ``group/task`` task reference
    group_dir : Path
        Full path to the (resolved) task group directory
    task_dir : Path
        Full path to task directory
    group_metadata : dict
        Parsed group metadata from ``group.yaml``

    Examples
    --------
    >>> task = Task("smoke_test/hello-world-python", resolver)  # doctest: +SKIP
    >>> task.task_ref
    'smoke_test/hello-world-python'
    """

    def __init__(
        self,
        task_name: str,
        resolver: GroupResolver,
        task_group: str | None = None,
    ):
        """Initialize a task.

        Parameters
        ----------
        task_name : str
            Task identifier. Accepts either ``task-id`` or ``group/task-id``.
        resolver : GroupResolver
            Resolver used to locate task groups.
        task_group : str, optional
            Explicit task group to use when ``task_name`` is not canonical.

        Raises
        ------
        FileNotFoundError
            If the task directory is not found
        ValueError
            If the task name is ambiguous or group metadata is invalid
        """
        self.resolver = resolver

        self.group_id, self.task_id, self.group_dir, self.task_dir = self._resolve_task(
            task_name,
            task_group,
        )
        self.task_ref = f"{self.group_id}/{self.task_id}"
        self.group_metadata = self._load_group_metadata(self.group_id, self.group_dir)

    @property
    def build_env_script(self) -> Path:
        """Path to build_env.sh script."""
        return self.task_dir / "build_env.sh"

    @property
    def prompt_file(self) -> Path:
        """Path to prompt.md file."""
        return self.task_dir / "prompt.md"

    @property
    def group_prompt_file(self) -> Path | None:
        """Path to the task group's shared ``group_prompt.md``, if present.

        This optional file lets a task group inject shared, group-wide guidance
        (for example, how to build and test the group's software stack) into the
        agent's prompt between the harness system instructions and the per-task
        prompt. Returns ``None`` when the group does not define one, so the
        prompt layer is fully backward-compatible.
        """
        candidate = self.group_dir / "group_prompt.md"
        return candidate if candidate.is_file() else None

    @property
    def workspace_dir(self) -> Path:
        """Path to workspace directory."""
        return self.task_dir / "workspace"

    @property
    def verification_dir(self) -> Path:
        """Path to verification directory."""
        return self.task_dir / "verification"

    @property
    def verify_script(self) -> Path:
        """Path to verify_solution.sh script."""
        return self.verification_dir / "verify_solution.sh"

    @property
    def group(self) -> TaskGroup:
        """Return the task group wrapper."""
        return TaskGroup(
            group_id=self.group_id,
            group_dir=self.group_dir,
            metadata=self.group_metadata,
        )

    def validate(self) -> list[str]:
        """Validate that all required task components exist."""
        return self._task_dir_errors(self.task_dir)

    # -- discovery (resolver-based) -------------------------------------------

    @staticmethod
    def list_task_groups(resolver: GroupResolver) -> list[str]:
        """List all available task groups across the resolver's search roots."""
        return resolver.list_group_ids()

    @staticmethod
    def list_tasks(resolver: GroupResolver) -> list[str]:
        """List all available task references as ``group/task``."""
        task_refs: list[str] = []
        for group_id in resolver.list_group_ids():
            try:
                group_dir = resolver.resolve_group_dir(group_id)
            except FileNotFoundError:
                continue
            for task_dir in sorted(group_dir.iterdir()):
                if not task_dir.is_dir() or task_dir.name.startswith("."):
                    continue
                if task_dir.name == "__pycache__":
                    continue
                if Task._task_dir_errors(task_dir):
                    continue
                task_refs.append(f"{group_id}/{task_dir.name}")
        return task_refs

    @staticmethod
    def list_tasks_by_group(resolver: GroupResolver) -> dict[str, list[str]]:
        """Return available task ids keyed by task group."""
        tasks: dict[str, list[str]] = {}
        for task_ref in Task.list_tasks(resolver):
            group_id, task_id = task_ref.split("/", 1)
            tasks.setdefault(group_id, []).append(task_id)
        return {group_id: sorted(task_ids) for group_id, task_ids in sorted(tasks.items())}

    @staticmethod
    def match_task_refs(
        patterns: list[str],
        resolver: GroupResolver,
    ) -> tuple[list[str], list[str]]:
        """Match canonical ``group/task`` refs against glob patterns.

        Returns ``(matched_task_refs, unmatched_patterns)`` where task refs are
        sorted and deduplicated. Matching is performed only against canonical
        refs returned by :meth:`list_tasks`.
        """
        if not patterns:
            return [], []

        task_refs = Task.list_tasks(resolver)
        matches: set[str] = set()
        unmatched_patterns: list[str] = []

        for pattern in patterns:
            pattern_matches = [task_ref for task_ref in task_refs if fnmatchcase(task_ref, pattern)]
            if pattern_matches:
                matches.update(pattern_matches)
            else:
                unmatched_patterns.append(pattern)

        return sorted(matches), unmatched_patterns

    # -- resolution -----------------------------------------------------------

    def _resolve_task(
        self,
        task_name: str,
        task_group: str | None,
    ) -> tuple[str, str, Path, Path]:
        """Resolve a task name to ``(group_id, task_id, group_dir, task_dir)``."""
        if "/" in task_name:
            group_id, task_id = task_name.split("/", 1)
            if task_group is not None and task_group != group_id:
                raise ValueError(
                    f"Task group mismatch: --task specifies '{group_id}' but --task-group is '{task_group}'"
                )
            group_dir = self.resolver.resolve_group_dir(group_id)
            task_dir = group_dir / task_id
            if not task_dir.exists():
                raise FileNotFoundError(f"Task directory not found: {task_dir}")
            errors = self._task_dir_errors(task_dir)
            if errors:
                raise ValueError(
                    f"Task directory is not a valid task: {task_dir}\n" + "\n".join(errors)
                )
            return group_id, task_id, group_dir, task_dir

        if task_group is not None:
            group_dir = self.resolver.resolve_group_dir(task_group)
            task_dir = group_dir / task_name
            if not task_dir.exists():
                raise FileNotFoundError(f"Task directory not found: {task_dir}")
            errors = self._task_dir_errors(task_dir)
            if errors:
                raise ValueError(
                    f"Task directory is not a valid task: {task_dir}\n" + "\n".join(errors)
                )
            return task_group, task_name, group_dir, task_dir

        matches: list[tuple[str, Path, Path]] = []
        for group_id in self.resolver.list_group_ids():
            try:
                group_dir = self.resolver.resolve_group_dir(group_id)
            except FileNotFoundError:
                continue
            task_dir = group_dir / task_name
            if task_dir.exists() and not self._task_dir_errors(task_dir):
                matches.append((group_id, group_dir, task_dir))

        if not matches:
            raise FileNotFoundError(f"Task '{task_name}' not found in any task group")

        if len(matches) > 1:
            group_list = ", ".join(group_id for group_id, _, _ in matches)
            raise ValueError(
                f"Task '{task_name}' is ambiguous across groups: {group_list}. Use --task-group or a canonical group/task path."
            )

        group_id, group_dir, task_dir = matches[0]
        return group_id, task_name, group_dir, task_dir

    def _load_group_metadata(self, group_id: str, group_dir: Path) -> dict:
        """Load and validate group metadata."""
        metadata_path = group_dir / "group.yaml"
        if not metadata_path.exists():
            raise FileNotFoundError(f"Task group metadata not found: {metadata_path}")

        with open(metadata_path) as f:
            data = yaml.safe_load(f) or {}

        declared_id = data.get("id")
        if declared_id != group_id:
            raise ValueError(
                f"Task group metadata id mismatch in {metadata_path}: expected '{group_id}', found '{declared_id}'"
            )
        return data

    @staticmethod
    def _task_dir_errors(task_dir: Path) -> list[str]:
        """Return structural validation errors for a task directory.

        A task must ship an expanded ``workspace/`` on disk, a ``prompt.md``, a
        ``build_env.sh``, and a ``verification/verify_solution.sh``.
        """
        errors: list[str] = []

        build_env_script = task_dir / "build_env.sh"
        prompt_file = task_dir / "prompt.md"
        workspace_dir = task_dir / "workspace"
        verification_dir = task_dir / "verification"
        verify_script = verification_dir / "verify_solution.sh"

        if not build_env_script.exists():
            errors.append(f"Missing build_env.sh: {build_env_script}")

        if not prompt_file.exists():
            errors.append(f"Missing prompt.md: {prompt_file}")

        if not workspace_dir.exists():
            errors.append(f"Missing workspace directory: {workspace_dir}")

        if not verification_dir.exists():
            errors.append(f"Missing verification directory: {verification_dir}")

        if not verify_script.exists():
            errors.append(f"Missing verify_solution.sh: {verify_script}")

        return errors
