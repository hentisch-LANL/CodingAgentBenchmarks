"""Container management for ABC Bench.

Thin adapter over :class:`automaton.ContainerManager`. ABC Bench owns the
on-disk layout of container templates. The *base* and *harness* layers live in
the package/data root (``containers/base.Dockerfile.jinja2`` and
``containers/harnesses/``). The *runtime* ("third") layer is a **task group**,
and its container template lives inside the resolved task-group directory
(``<group_dir>/Dockerfile.jinja2`` by default), so a group is a fully
self-contained, relocatable unit.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from automaton import ContainerInfo, ContainerLayout, ContainerRef
from automaton import ContainerManager as _AutomatonContainerManager
from automaton import Harness

from .groups import GroupResolver
from .task import DEFAULT_CONTAINER_TEMPLATE, Task

__all__ = ["ContainerManager", "ContainerRef", "ContainerInfo"]

# Keep benchmark-owned images and storage separate from the shared engine.
ABC_BENCH_IMAGE_PREFIX = "abc-bench"


def _group_template_path(group_dir: Path, metadata: dict) -> Path:
    """Return the group's runtime container template path.

    Honors an optional ``container_template`` in ``group.yaml`` interpreted
    relative to the group directory; defaults to ``Dockerfile.jinja2``.
    """
    template_name = metadata.get("container_template") or DEFAULT_CONTAINER_TEMPLATE
    return (group_dir / template_name).resolve()


def _abc_bench_layout(package_root: Path, resolver: GroupResolver) -> ContainerLayout:
    """Build the Automaton container layout for ABC Bench.

    Parameters
    ----------
    package_root : Path
        Root containing ``containers/`` and ``harnesses/`` (base + harness
        layers are ABC Bench-owned, not benchmark-specific).
    resolver : GroupResolver
        Resolver used to locate task-group directories (runtime layer).
    """
    package_root = Path(package_root).resolve()

    def harness_template_path(template_name: str) -> Path:
        return package_root / "containers" / "harnesses" / f"{template_name}.Dockerfile.jinja2"

    def list_harnesses() -> list[str]:
        harnesses_dir = package_root / "harnesses"
        if not harnesses_dir.exists():
            return []
        return sorted(
            d.name
            for d in harnesses_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    def load_harness(harness_id: str) -> Harness:
        return Harness(harness_id, package_root / "harnesses" / harness_id)

    def list_runtime_layers() -> list[str]:
        return resolver.list_group_ids()

    def runtime_layer_metadata(group_id: str) -> dict:
        group_dir = resolver.resolve_group_dir(group_id)
        group_file = group_dir / "group.yaml"
        if not group_file.exists():
            raise FileNotFoundError(f"Task group metadata not found: {group_file}")
        with open(group_file) as f:
            data = yaml.safe_load(f) or {}
        if data.get("id") != group_id:
            raise ValueError(f"Task group metadata id mismatch in {group_file}")
        # Force the runtime template lookup to key on the group id; the actual
        # template path resolution (below) reads the group dir directly.
        data = dict(data)
        data["container_template"] = group_id
        return data

    def runtime_template_path(template_name: str) -> Path:
        # ``template_name`` is the group id (see runtime_layer_metadata).
        group_dir = resolver.resolve_group_dir(template_name)
        group_file = group_dir / "group.yaml"
        with open(group_file) as f:
            metadata = yaml.safe_load(f) or {}
        return _group_template_path(group_dir, metadata)

    return ContainerLayout(
        base_template_path=package_root / "containers" / "base.Dockerfile.jinja2",
        harness_template_path=harness_template_path,
        list_harnesses=list_harnesses,
        load_harness=load_harness,
        list_runtime_layers=list_runtime_layers,
        runtime_layer_metadata=runtime_layer_metadata,
        runtime_template_path=runtime_template_path,
    )


class ContainerManager(_AutomatonContainerManager):
    """ABC Bench container manager.

    Wraps :class:`automaton.ContainerManager` with the ABC Bench layout: base
    and harness layers from ``package_root``; runtime (task-group) layer from
    the resolved group directory.

    Parameters
    ----------
    package_root : Path
        Root containing ``containers/`` and ``harnesses/``.
    resolver : GroupResolver
        Resolver used to locate task-group directories.
    """

    def __init__(self, package_root: Path, resolver: GroupResolver):
        self.package_root = Path(package_root).resolve()
        self.resolver = resolver
        container_base = os.environ.get("ABC_BENCH_CONTAINER_BASE")
        cache_root = (
            Path(container_base).expanduser().resolve()
            if container_base
            else Path("/var/tmp") / f"{ABC_BENCH_IMAGE_PREFIX}-{os.getuid()}"
        )
        super().__init__(
            _abc_bench_layout(self.package_root, resolver),
            cache_root=cache_root,
            image_prefix=ABC_BENCH_IMAGE_PREFIX,
        )
