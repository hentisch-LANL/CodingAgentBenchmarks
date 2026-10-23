"""Container management for Automaton.

Manages layered Charliecloud container builds using base, harness, and
optional *runtime* templates, exposing built artifacts through path-like
container references.

Automaton is content-free: it does not know where templates live or what
runtime layers exist. Callers supply a :class:`ContainerLayout` describing:

- the base Dockerfile template,
- how to find a harness's Dockerfile template and enumerate harnesses,
- how to enumerate runtime layers and find their Dockerfile templates and
  metadata.

The third ("runtime") layer is fully generic here. A benchmark orchestrator
might back it with "task groups", but Automaton assigns no such meaning.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Sequence

from .harness import Harness


DEFAULT_IMAGE_PREFIX = "automaton"
BASE_CONTAINER_ID = "base"


@dataclass(frozen=True)
class ContainerLayout:
    """Describes where container templates and runtime layers come from.

    Automaton stays content-free by delegating all template discovery to the
    caller through this layout.

    Parameters
    ----------
    base_template_path : Path
        Path to the base Dockerfile template (rendered with ``{{ parent_image }}``).
    harness_template_path : Callable[[str], Path]
        Given a harness template name, return its Dockerfile template path.
    list_harnesses : Callable[[], list[str]]
        Return the available harness ids.
    load_harness : Callable[[str], Harness]
        Given a harness id, return a loaded :class:`~automaton.harness.Harness`.
        This is how the manager learns a harness's ``container_template``.
    list_runtime_layers : Callable[[], list[str]]
        Return the available runtime-layer ids (e.g. task-group ids).
    runtime_layer_metadata : Callable[[str], dict]
        Given a runtime-layer id, return its metadata dict. The manager reads
        the optional ``container_template`` key (defaults to the layer id).
    runtime_template_path : Callable[[str], Path]
        Given a runtime-layer template name, return its Dockerfile template path.
    """

    base_template_path: Path
    harness_template_path: Callable[[str], Path]
    list_harnesses: Callable[[], list[str]]
    load_harness: Callable[[str], Harness]
    list_runtime_layers: Callable[[], list[str]]
    runtime_layer_metadata: Callable[[str], dict]
    runtime_template_path: Callable[[str], Path]


@dataclass
class ContainerInfo:
    """Metadata about a buildable or built container artifact."""

    container_id: str
    canonical_ref: str
    aliases: tuple[str, ...]
    kind: str
    path: Path
    exists: bool
    size_bytes: int | None = None
    build_time: datetime | None = None

    def size_human(self) -> str:
        """Return human-readable size."""
        if self.size_bytes is None:
            return "unknown"
        size = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"


@dataclass(frozen=True)
class ContainerRef:
    """Normalized container reference.

    Kinds: ``base``, ``harness``, ``runtime``. For ``runtime``, ``group_id``
    holds the runtime-layer id.
    """

    kind: str
    harness_id: str | None = None
    group_id: str | None = None

    @property
    def display_ref(self) -> str:
        """Return the preferred shorthand container ref."""
        if self.kind == "base":
            return BASE_CONTAINER_ID
        if self.kind == "harness":
            assert self.harness_id is not None
            return self.harness_id
        assert self.harness_id is not None and self.group_id is not None
        return f"{self.harness_id}/{self.group_id}"

    @property
    def canonical_ref(self) -> str:
        """Return the explicit container ref including the base root."""
        if self.kind == "base":
            return BASE_CONTAINER_ID
        if self.kind == "harness":
            assert self.harness_id is not None
            return f"{BASE_CONTAINER_ID}/{self.harness_id}"
        assert self.harness_id is not None and self.group_id is not None
        return f"{BASE_CONTAINER_ID}/{self.harness_id}/{self.group_id}"

    @property
    def aliases(self) -> tuple[str, ...]:
        """Return accepted aliases for this ref."""
        aliases = [self.display_ref]
        if self.canonical_ref != self.display_ref:
            aliases.append(self.canonical_ref)
        return tuple(aliases)


class ContainerManager:
    """Manage layered Charliecloud container builds and lifecycle.

    Parameters
    ----------
    layout : ContainerLayout
        Describes template locations and runtime-layer discovery.
    cache_root : Path, optional
        Root for built artifacts and image storage. Defaults to the
        ``AUTOMATON_CONTAINER_BASE`` environment variable, else
        ``/var/tmp/automaton-{uid}``.
    image_prefix : str, optional
        Prefix for Charliecloud image names. Default ``"automaton"``.
    build_force : str | bool | None, optional
        Controls Charliecloud's ``--force`` unprivileged-build mode for
        ``ch-image build``. Charliecloud's auto-detection of when fakeroot is
        needed is unreliable across base images and storage dirs, which can
        cause ``RUN`` steps that run ``apt-get``/``rm -rf`` to fail with errors
        like ``can't mkdir: var/lib/apt/lists: File exists``. Passing
        ``--force`` up front makes this deterministic.

        - ``True`` (default): pass bare ``--force`` (Charliecloud picks method).
        - a string (e.g. ``"seccomp"``, ``"fakeroot"``): pass ``--force=<value>``.
        - ``False``/``None``: do not pass ``--force`` (legacy behavior).

        Overridable via the ``AUTOMATON_CH_FORCE`` environment variable, whose
        value is interpreted the same way (``"0"``/``"false"``/``"none"`` ->
        disabled, ``"1"``/``"true"`` -> bare ``--force``, any other non-empty
        string -> ``--force=<value>``).
    """

    def __init__(
        self,
        layout: ContainerLayout,
        cache_root: Path | None = None,
        image_prefix: str = DEFAULT_IMAGE_PREFIX,
        build_force: str | bool | None = True,
    ):
        self.layout = layout
        self.image_prefix = image_prefix
        self.build_force = self._resolve_build_force(build_force)
        if cache_root is not None:
            self.cache_root = Path(cache_root).expanduser().resolve()
        else:
            env_base = os.environ.get("AUTOMATON_CONTAINER_BASE")
            if env_base:
                self.cache_root = Path(env_base).expanduser().resolve()
            else:
                self.cache_root = Path("/var/tmp") / f"{image_prefix}-{os.getuid()}"
        self.container_root = self.cache_root / "containers"

    @staticmethod
    def _resolve_build_force(build_force: str | bool | None) -> str | bool:
        """Resolve the effective ``--force`` setting, honoring env override.

        Returns either ``False`` (do not pass ``--force``), ``True`` (pass bare
        ``--force``), or a ``str`` method name (pass ``--force=<method>``).
        """
        env_value = os.environ.get("AUTOMATON_CH_FORCE")
        if env_value is not None:
            normalized = env_value.strip().lower()
            if normalized in ("", "0", "false", "no", "none", "off"):
                return False
            if normalized in ("1", "true", "yes", "on"):
                return True
            return env_value.strip()
        if build_force is None:
            return False
        return build_force

    def _build_force_args(self) -> list[str]:
        """Return the ``--force`` argument list for ``ch-image build``."""
        if self.build_force is False:
            return []
        if self.build_force is True:
            return ["--force"]
        return [f"--force={self.build_force}"]

    # -- image naming ---------------------------------------------------------

    @property
    def base_image_name(self) -> str:
        return f"{self.image_prefix}-base"

    def intermediate_harness_image_name(self, harness_id: str) -> str:
        return f"{self.image_prefix}-harness-{harness_id}"

    def final_runtime_image_name(self, harness_id: str, group_id: str) -> str:
        return f"{self.image_prefix}-runtime-{harness_id}-{group_id}"

    # -- charliecloud availability -------------------------------------------

    @staticmethod
    def check_charliecloud_available() -> tuple[bool, str]:
        """Check if Charliecloud tools are available."""
        missing_tools = []
        for tool in ["ch-image", "ch-convert", "ch-run"]:
            result = subprocess.run(
                ["which", tool],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                missing_tools.append(tool)
        if missing_tools:
            error_msg = (
                f"Charliecloud is not available. Missing tools: {', '.join(missing_tools)}\n\n"
                f"Please install Charliecloud before building containers.\n"
                f"See: https://hpc.github.io/charliecloud/install.html\n\n"
                f"On many HPC systems, you can load it with:\n"
                f"  module load charliecloud"
            )
            return False, error_msg
        return True, ""

    @staticmethod
    def check_mpi_available() -> tuple[bool, str]:
        """Check if an MPI runtime is available (mpi4py can load libmpi)."""
        try:
            from mpi4py import MPI  # noqa: F401
        except ImportError:
            error_msg = (
                "No MPI implementation is loaded.\n\n"
                "Please make an MPI implementation available before running with --mpi.\n\n"
                "On many HPC systems, you can load one with:\n"
                "  module load openmpi"
            )
            return False, error_msg
        return True, ""

    @property
    def shared_image_storage(self) -> Path:
        """Shared Charliecloud builder storage for all Automaton images."""
        return self.cache_root / "ch-image-storage"

    # -- refs -----------------------------------------------------------------

    @staticmethod
    def _is_glob_pattern(pattern: str) -> bool:
        return any(token in pattern for token in ("*", "?", "["))

    @staticmethod
    def base_container_ref() -> ContainerRef:
        return ContainerRef(kind="base")

    @staticmethod
    def harness_container_ref(harness_id: str) -> ContainerRef:
        return ContainerRef(kind="harness", harness_id=harness_id)

    @staticmethod
    def runtime_container_ref(harness_id: str, group_id: str) -> ContainerRef:
        return ContainerRef(kind="runtime", harness_id=harness_id, group_id=group_id)

    def available_container_refs(self) -> list[ContainerRef]:
        """Return all valid container refs known from the layout."""
        refs = [self.base_container_ref()]
        harnesses = self.layout.list_harnesses()
        groups = self.layout.list_runtime_layers()
        for harness_id in harnesses:
            refs.append(self.harness_container_ref(harness_id))
        for harness_id in harnesses:
            for group_id in groups:
                refs.append(self.runtime_container_ref(harness_id, group_id))
        return refs

    def _validate_container_ref(self, ref: ContainerRef) -> ContainerRef:
        if ref.kind == "base":
            return ref
        assert ref.harness_id is not None
        harnesses = set(self.layout.list_harnesses())
        if ref.harness_id not in harnesses:
            raise ValueError(f"unknown harness/container layer: {ref.harness_id}")
        if ref.kind == "harness":
            return ref
        assert ref.group_id is not None
        groups = set(self.layout.list_runtime_layers())
        if ref.group_id not in groups:
            raise ValueError(f"unknown runtime layer: {ref.group_id}")
        return ref

    def resolve_container_ref(self, ref: str | ContainerRef) -> ContainerRef:
        """Normalize a user-supplied container ref."""
        if isinstance(ref, ContainerRef):
            return self._validate_container_ref(ref)
        text = ref.strip()
        if not text:
            raise ValueError("container ref cannot be empty")
        parts = text.split("/")
        if parts == [BASE_CONTAINER_ID]:
            return self.base_container_ref()
        if len(parts) == 1:
            return self._validate_container_ref(self.harness_container_ref(parts[0]))
        if len(parts) == 2:
            if parts[0] == BASE_CONTAINER_ID:
                return self._validate_container_ref(self.harness_container_ref(parts[1]))
            return self._validate_container_ref(self.runtime_container_ref(parts[0], parts[1]))
        if len(parts) == 3 and parts[0] == BASE_CONTAINER_ID:
            return self._validate_container_ref(self.runtime_container_ref(parts[1], parts[2]))
        raise ValueError(
            "invalid container ref. Use one of: 'base', '<harness>', 'base/<harness>', "
            "'<harness>/<runtime-layer>', or 'base/<harness>/<runtime-layer>'"
        )

    def get_container_path(self, ref: str | ContainerRef) -> Path:
        """Return the on-disk path for a built container artifact."""
        container_ref = self.resolve_container_ref(ref)
        if container_ref.kind == "base":
            return self.container_root / BASE_CONTAINER_ID
        if container_ref.kind == "harness":
            assert container_ref.harness_id is not None
            return self.container_root / f"harness__{container_ref.harness_id}"
        assert container_ref.harness_id is not None and container_ref.group_id is not None
        return self.container_root / f"runtime__{container_ref.harness_id}__{container_ref.group_id}"

    def get_container_info(self, ref: str | ContainerRef) -> ContainerInfo:
        """Get metadata for a buildable or built container."""
        container_ref = self.resolve_container_ref(ref)
        path = self.get_container_path(container_ref)
        exists = path.exists()
        size_bytes = None
        build_time = None
        if exists:
            size_bytes = self._safe_tree_size(path)
            build_time = datetime.fromtimestamp(path.stat().st_mtime)
        return ContainerInfo(
            container_id=container_ref.display_ref,
            canonical_ref=container_ref.canonical_ref,
            aliases=container_ref.aliases,
            kind=container_ref.kind,
            path=path,
            exists=exists,
            size_bytes=size_bytes,
            build_time=build_time,
        )

    @staticmethod
    def _safe_tree_size(path: Path) -> int:
        """Return tree size without dereferencing symlinks."""
        size_bytes = 0
        for entry in path.rglob("*"):
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
                size_bytes += entry_stat.st_size
        return size_bytes

    def list_containers(self, only_built: bool = False) -> dict[str, ContainerInfo]:
        """List all buildable containers keyed by shorthand ref."""
        container_infos: dict[str, ContainerInfo] = {}
        for ref in self.available_container_refs():
            info = self.get_container_info(ref)
            if only_built and not info.exists:
                continue
            container_infos[info.container_id] = info
        return container_infos

    def find_container_infos(self, pattern: str, only_built: bool = False) -> list[ContainerInfo]:
        """Find containers by exact ref or glob pattern."""
        infos = [self.get_container_info(ref) for ref in self.available_container_refs()]
        if only_built:
            infos = [info for info in infos if info.exists]
        for info in infos:
            if pattern in info.aliases:
                return [info]
        if not self._is_glob_pattern(pattern):
            return []
        matches = []
        for info in infos:
            if any(fnmatchcase(alias, pattern) for alias in info.aliases):
                matches.append(info)
        return matches

    def container_exists(self, ref: str | ContainerRef) -> bool:
        """Return True if the requested built container exists."""
        return self.get_container_path(ref).exists()

    # -- build ----------------------------------------------------------------

    def build_container(self, ref: str | ContainerRef, force: bool = False, verbose: bool = True) -> Path:
        """Build the requested container, auto-building parent layers as needed."""
        available, error_msg = self.check_charliecloud_available()
        if not available:
            raise RuntimeError(error_msg)

        container_ref = self.resolve_container_ref(ref)
        if container_ref.kind == "base":
            return self._build_base_container(force=force, verbose=verbose)

        assert container_ref.harness_id is not None
        harness = self.layout.load_harness(container_ref.harness_id)
        errors = harness.validate()
        if errors:
            raise ValueError("Harness validation failed:\n" + "\n".join(errors))

        self._build_base_container(force=False, verbose=verbose)
        self._ensure_base_image(verbose=verbose)

        if container_ref.kind == "harness":
            return self._build_harness_container(harness, force=force, verbose=verbose)

        assert container_ref.group_id is not None
        runtime_layer = self._load_runtime_layer(container_ref.group_id)
        self._build_harness_container(harness, force=False, verbose=verbose)
        self._ensure_harness_image(harness.harness_id, verbose=verbose)
        return self._build_runtime_layer(harness, runtime_layer, force=force, verbose=verbose)

    def delete_container(self, ref: str | ContainerRef, verbose: bool = True) -> bool:
        """Delete a built container artifact."""
        container_ref = self.resolve_container_ref(ref)
        container_path = self.get_container_path(container_ref)
        if not container_path.exists():
            if verbose:
                print(f"Container does not exist: {container_ref.display_ref}")
            return False
        if verbose:
            info = self.get_container_info(container_ref)
            print(f"Deleting container: {info.container_id}")
            print(f"  Path: {container_path}")
            print(f"  Size: {info.size_human()}")
        shutil.rmtree(container_path)
        if verbose:
            print("Container deleted")
        return True

    def clear_cache(self, verbose: bool = True) -> bool:
        """Remove all Automaton temporary containers and builder storage."""
        if not self.cache_root.exists():
            if verbose:
                print(f"Automaton cache does not exist: {self.cache_root}")
            return False
        if verbose:
            print(f"Removing Automaton cache: {self.cache_root}")
        shutil.rmtree(self.cache_root)
        if verbose:
            print("Cache cleared")
        return True

    def _build_base_container(self, force: bool, verbose: bool) -> Path:
        container_path = self.get_container_path(self.base_container_ref())
        if container_path.exists() and not force:
            if verbose:
                print(f"Container already exists at {container_path}")
                print("Use --rebuild to force rebuild")
            return container_path
        if container_path.exists() and force:
            shutil.rmtree(container_path)
        self._prepare_image_storage(self.shared_image_storage)
        template_path = Path(self.layout.base_template_path)
        if not template_path.exists():
            raise FileNotFoundError(f"Base container template not found: {template_path}")
        if verbose:
            print("Building container: base")
            print(f"Output path: {container_path}")
            print(f"Charliecloud storage: {self.shared_image_storage}")
            print()
        if self._image_exists(self.base_image_name, self.shared_image_storage) and force:
            self._delete_image(self.base_image_name)
        self._build_from_template(
            template_path=template_path,
            image_name=self.base_image_name,
            from_image=None,
            output_dir=container_path,
            verbose=verbose,
        )
        return container_path

    def _build_harness_container(self, harness: Harness, force: bool, verbose: bool) -> Path:
        container_ref = self.harness_container_ref(harness.harness_id)
        container_path = self.get_container_path(container_ref)
        image_name = self.intermediate_harness_image_name(harness.harness_id)
        self._ensure_base_image(verbose=verbose)
        if container_path.exists() and not force:
            if verbose:
                print(f"Container already exists at {container_path}")
                print("Use --rebuild to force rebuild")
            if not self._image_exists(image_name, self.shared_image_storage):
                self._import_image(container_path, image_name, verbose=verbose)
            return container_path
        if container_path.exists() and force:
            shutil.rmtree(container_path)
        self._prepare_image_storage(self.shared_image_storage)
        self._build_harness_image(harness, force=force, verbose=verbose)
        if verbose:
            print(f"Materializing harness container: {container_ref.display_ref}")
            print(f"Output path: {container_path}")
            print(f"Charliecloud storage: {self.shared_image_storage}")
            print()
        self._convert_image_to_dir(image_name, container_path, verbose=verbose)
        return container_path

    def _build_harness_image(self, harness: Harness, force: bool, verbose: bool) -> None:
        image_name = self.intermediate_harness_image_name(harness.harness_id)
        if self._image_exists(image_name, self.shared_image_storage) and not force:
            if verbose:
                print(f"Harness image already exists in Charliecloud storage: {image_name}")
            return
        if self._image_exists(image_name, self.shared_image_storage) and force:
            self._delete_image(image_name)
        template_path = Path(self.layout.harness_template_path(harness.container_template))
        if not template_path.exists():
            raise FileNotFoundError(f"Harness container template not found: {template_path}")
        if verbose:
            print(f"Building intermediate harness image: {harness.harness_id}")
        self._build_from_template(
            template_path=template_path,
            image_name=image_name,
            from_image=self.base_image_name,
            output_dir=None,
            verbose=verbose,
        )

    def _build_runtime_layer(
        self,
        harness: Harness,
        runtime_layer: dict,
        force: bool,
        verbose: bool,
    ) -> Path:
        group_id = str(runtime_layer["id"])
        container_ref = self.runtime_container_ref(harness.harness_id, group_id)
        container_path = self.get_container_path(container_ref)
        if container_path.exists() and not force:
            if verbose:
                print(f"Container already exists at {container_path}")
                print("Use --rebuild to force rebuild")
            return container_path
        if container_path.exists() and force:
            shutil.rmtree(container_path)
        template_name = str(runtime_layer.get("container_template", group_id))
        template_path = Path(self.layout.runtime_template_path(template_name))
        if not template_path.exists():
            raise FileNotFoundError(f"Runtime-layer container template not found: {template_path}")
        image_name = self.final_runtime_image_name(harness.harness_id, group_id)
        if self._image_exists(image_name, self.shared_image_storage) and force:
            self._delete_image(image_name)
        if verbose:
            print(f"Building final runtime image: {container_ref.display_ref}")
            print(f"Output path: {container_path}")
            print(f"Charliecloud storage: {self.shared_image_storage}")
            print()
        self._build_from_template(
            template_path=template_path,
            image_name=image_name,
            from_image=self.intermediate_harness_image_name(harness.harness_id),
            output_dir=None,
            verbose=verbose,
        )
        self._convert_image_to_dir(image_name, container_path, verbose=verbose)
        return container_path

    def _build_from_template(
        self,
        template_path: Path,
        image_name: str,
        from_image: str | None,
        output_dir: Path | None,
        verbose: bool,
    ) -> None:
        rendered = self._render_template(template_path, from_image=from_image)
        with tempfile.TemporaryDirectory(prefix=f"{self.image_prefix}-container-") as temp_dir:
            temp_path = Path(temp_dir)
            dockerfile_path = temp_path / "Dockerfile"
            dockerfile_path.write_text(rendered)
            env = {**os.environ, "CH_IMAGE_STORAGE": str(self.shared_image_storage)}
            build_cmd = [
                "ch-image",
                "--storage",
                str(self.shared_image_storage),
                "build",
                *self._build_force_args(),
                "-t",
                image_name,
                "-f",
                str(dockerfile_path),
                str(temp_path),
            ]
            result = subprocess.run(
                build_cmd,
                env=env,
                stdout=None if verbose else subprocess.PIPE,
                stderr=subprocess.STDOUT if verbose else subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Container build failed for image {image_name} with exit code {result.returncode}")
            if output_dir is not None:
                self._convert_image_to_dir(image_name, output_dir, verbose=verbose)

    def _render_template(self, template_path: Path, from_image: str | None) -> str:
        """Render a minimal Jinja-like template for Dockerfiles."""
        content = template_path.read_text()
        parent_image = from_image or ""
        return content.replace("{{ parent_image }}", parent_image)

    def _image_exists(self, image_name: str, image_storage: Path) -> bool:
        image_path = image_storage / "img" / image_name
        if image_path.exists():
            return True
        if not image_storage.exists():
            return False
        result = subprocess.run(
            ["ch-image", "--storage", str(image_storage), "list", image_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            return False
        return "in local storage:" in result.stdout.lower() and "yes" in result.stdout.lower()

    def _prepare_image_storage(self, image_storage: Path) -> None:
        if image_storage.exists() and not any(image_storage.iterdir()):
            image_storage.rmdir()
        image_storage.parent.mkdir(parents=True, exist_ok=True)

    def _convert_image_to_dir(self, image_name: str, output_dir: Path, verbose: bool) -> None:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "CH_IMAGE_STORAGE": str(self.shared_image_storage)}
        convert_cmd = [
            "ch-convert",
            "--storage",
            str(self.shared_image_storage),
            "-i",
            "ch-image",
            "-o",
            "dir",
            image_name,
            str(output_dir),
        ]
        result = subprocess.run(
            convert_cmd,
            env=env,
            stdout=None if verbose else subprocess.PIPE,
            stderr=subprocess.STDOUT if verbose else subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Container conversion failed for image {image_name} with exit code {result.returncode}")

    def _delete_image(self, image_name: str) -> None:
        subprocess.run(
            ["ch-image", "--storage", str(self.shared_image_storage), "delete", image_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def _ensure_base_image(self, verbose: bool) -> None:
        if self._image_exists(self.base_image_name, self.shared_image_storage):
            return
        self._import_image(self.get_container_path(self.base_container_ref()), self.base_image_name, verbose=verbose)

    def _ensure_harness_image(self, harness_id: str, verbose: bool) -> None:
        image_name = self.intermediate_harness_image_name(harness_id)
        if self._image_exists(image_name, self.shared_image_storage):
            return
        self._import_image(self.get_container_path(self.harness_container_ref(harness_id)), image_name, verbose=verbose)

    def _import_image(self, container_path: Path, image_name: str, verbose: bool) -> None:
        image_storage = self.shared_image_storage
        self._prepare_image_storage(image_storage)
        if verbose:
            print(f"Importing image into Charliecloud storage: {image_name}")
            print(f"  Source path: {container_path}")
            print(f"  Storage path: {image_storage}")
        result = subprocess.run(
            [
                "ch-image",
                "--storage",
                str(image_storage),
                "import",
                str(container_path),
                image_name,
            ],
            stdout=None if verbose else subprocess.PIPE,
            stderr=subprocess.STDOUT if verbose else subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to import image {image_name} from {container_path} with exit code {result.returncode}")

    def _load_runtime_layer(self, group_id: str) -> dict:
        """Load runtime-layer metadata via the layout, ensuring it carries its id."""
        data = dict(self.layout.runtime_layer_metadata(group_id))
        data.setdefault("id", group_id)
        if data.get("id") != group_id:
            raise ValueError(f"Runtime-layer metadata id mismatch for {group_id!r}: {data.get('id')!r}")
        return data
