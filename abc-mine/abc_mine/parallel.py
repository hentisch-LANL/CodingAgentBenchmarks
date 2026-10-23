"""Parallel execution backends: serial, local process pool, and MPI.

Three backends behind one interface, so a stage is written once and the choice of
concurrency is purely a configuration matter:

``serial``
    One item at a time, in this process. **The default.** Requires nothing beyond
    the standard library — no MPI, no launcher, no extra install. This is what
    makes the tool usable on a laptop, in a plain shell, or inside a CI job, and it
    is the backend to debug with: a traceback is just a traceback.

``local``
    A pool of worker *processes* on one node. Still no MPI. Good for a single
    workstation or a one-node Slurm allocation. Processes rather than threads
    because each item spends its life waiting on a container subprocess, and
    process isolation means one crashed worker cannot take the run with it.

``mpi``
    automaton's :class:`MPIAgentPool`, for multi-node fan-out under
    ``srun``/``mpirun``. Requires the ``[mpi]`` extra. ``mpi4py`` is imported
    **lazily**, inside this backend only, so the other two never pay for it and an
    environment without MPI can still run everything else.

Checkpointing lives above this layer
------------------------------------
Every backend is told which items are pending and simply runs them; none of them
owns resume. Durable progress is each stage's own on-disk artifacts — a summary
file per commit, an append-only manifest — because those are the records a human
actually reads, and they stay meaningful even if this module changes. A side
benefit: switching backends mid-run (serial for a debug pass, MPI for the sweep)
resumes correctly, since the checkpoint is not tied to any backend's bookkeeping.
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

#: A work item: opaque to this module, meaningful to the stage.
Item = Any

#: A worker: takes one item, returns a result. Must not raise — but if it does,
#: the backend catches it and records a failure rather than losing the batch.
Worker = Callable[[Item], "ItemResult"]


@dataclass
class ItemResult:
    """Outcome of one work item.

    Attributes
    ----------
    item_id : str
        Stable identifier (for a commit, its short SHA).
    status : str
        ``ok``, ``failed``, ``skipped``, or ``dry_run``.
    detail : str
        Explanation when not ``ok``.
    data : dict
        Stage-specific payload, carried back for reporting.
    """

    item_id: str
    status: str = "ok"
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True if the item succeeded outright."""
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for transport across a process or MPI boundary."""
        return {
            "item_id": self.item_id,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ItemResult":
        """Rebuild from :meth:`to_dict`."""
        return cls(
            item_id=str(payload.get("item_id", "")),
            status=str(payload.get("status", "failed")),
            detail=str(payload.get("detail", "")),
            data=dict(payload.get("data", {})),
        )


@dataclass
class BatchOutcome:
    """Aggregate outcome of a batch."""

    results: list[ItemResult] = field(default_factory=list)
    #: True only on the rank/process responsible for reporting. Under MPI, worker
    #: ranks return ``False`` so a summary is not printed N times.
    is_primary: bool = True

    @property
    def counts(self) -> dict[str, int]:
        """Item counts by status."""
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def describe(self) -> str:
        """Render a one-line summary."""
        if not self.results:
            return "No items were run."
        counts = self.counts
        parts = [f"{counts[k]} {k}" for k in sorted(counts)]
        return f"{len(self.results)} item(s): " + ", ".join(parts)


def _safe_call(worker: Worker, item: Item, item_id: str) -> ItemResult:
    """Invoke ``worker``, converting any exception into a failed result.

    A single pathological item must never abort a sweep that has already spent
    hours on other items. The full traceback is preserved in ``detail`` because
    this is frequently the only surviving evidence of what went wrong.
    """
    try:
        result = worker(item)
    except BaseException as exc:  # noqa: BLE001 - deliberately broad
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return ItemResult(
            item_id=item_id,
            status="failed",
            detail=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
    if result is None:
        return ItemResult(item_id=item_id, status="ok")
    return result


def item_identity(item: Item) -> str:
    """Best-effort stable id for an item, for logging and failure records."""
    for attr in ("item_id", "short", "sha"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(item, dict):
        for key in ("item_id", "sha", "id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return str(item)[:64]


# -- serial ------------------------------------------------------------------


def run_serial(
    items: Sequence[Item],
    worker: Worker,
    *,
    on_result: Callable[[ItemResult, int, int], None] | None = None,
) -> BatchOutcome:
    """Run every item in this process, one at a time.

    The default backend, and the one to reach for when something is wrong: no
    process boundaries, no launcher, no MPI runtime, and exceptions surface where
    you can see them.
    """
    results: list[ItemResult] = []
    total = len(items)
    for index, item in enumerate(items):
        result = _safe_call(worker, item, item_identity(item))
        results.append(result)
        if on_result:
            on_result(result, index + 1, total)
    return BatchOutcome(results=results, is_primary=True)


# -- local process pool ------------------------------------------------------

#: Module-level worker handle for the forked pool. A ``multiprocessing`` worker
#: function must be picklable by reference, which a closure is not; forking from a
#: module-level global sidesteps that without requiring the stage's worker to be
#: importable by name.
_POOL_WORKER: Worker | None = None


def _pool_init(worker: Worker) -> None:
    """Initializer: install the worker in each child process."""
    global _POOL_WORKER
    _POOL_WORKER = worker


def _pool_call(item: Item) -> dict[str, Any]:
    """Child-side entry point: run one item and return a plain dict."""
    assert _POOL_WORKER is not None, "worker was not installed in the pool process"
    return _safe_call(_POOL_WORKER, item, item_identity(item)).to_dict()


def run_local(
    items: Sequence[Item],
    worker: Worker,
    *,
    workers: int = 4,
    on_result: Callable[[ItemResult, int, int], None] | None = None,
) -> BatchOutcome:
    """Run items across a pool of worker processes on this node.

    Uses the ``fork`` start method where available so the worker callable need not
    be importable by name — stages build their workers as closures over
    configuration, which is far more readable than the module-level indirection
    ``spawn`` would force.

    Results are consumed as they complete (``imap_unordered``), so progress
    reporting reflects real completion rather than submission order.
    """
    total = len(items)
    if total == 0:
        return BatchOutcome(results=[], is_primary=True)
    if workers <= 1:
        return run_serial(items, worker, on_result=on_result)

    import multiprocessing as mp

    try:
        ctx = mp.get_context("fork")
    except ValueError:  # pragma: no cover - platforms without fork
        ctx = mp.get_context()

    results: list[ItemResult] = []
    with ctx.Pool(
        processes=min(workers, total), initializer=_pool_init, initargs=(worker,)
    ) as pool:
        for payload in pool.imap_unordered(_pool_call, list(items)):
            result = ItemResult.from_dict(payload)
            results.append(result)
            if on_result:
                on_result(result, len(results), total)

    return BatchOutcome(results=results, is_primary=True)


# -- MPI ---------------------------------------------------------------------


def mpi_available() -> tuple[bool, str]:
    """Return ``(usable, reason)`` for the MPI backend.

    Importing ``mpi4py`` is **not** sufficient evidence that MPI works. On HPC
    systems the package is frequently installed while its MPI runtime is not
    loaded, in which case the import itself raises ``RuntimeError: cannot load MPI
    library`` — typically because an environment module (``module load openmpi``)
    has not been sourced. This was observed on the very cluster this tool targets.

    So the probe catches any exception, not just :class:`ImportError`, and returns
    the reason so the caller can print something actionable instead of a wall of
    ``libmpi.so: cannot open shared object file``.
    """
    try:
        from mpi4py import MPI  # noqa: F401
    except ImportError as exc:
        return False, (
            f"mpi4py is not installed ({exc}). "
            "Install it with: pip install 'abc-mine[mpi]'"
        )
    except BaseException as exc:  # noqa: BLE001 - see docstring
        return False, (
            f"mpi4py is installed but its MPI runtime failed to load "
            f"({type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else exc}). "
            "On a cluster this usually means the MPI module is not loaded — try "
            "`module load openmpi` (or your site's equivalent) before launching."
        )
    return True, ""


def mpi_rank_size() -> tuple[int, int]:
    """Return ``(rank, size)``, or ``(0, 1)`` when MPI is unusable.

    Lets a stage ask "am I rank 0?" before committing to a backend, which matters
    because destructive setup must happen on exactly one rank.
    """
    usable, _ = mpi_available()
    if not usable:
        return 0, 1
    from mpi4py import MPI

    comm = MPI.COMM_WORLD
    return comm.Get_rank(), comm.Get_size()


def run_mpi(
    items: Sequence[Item],
    worker: Worker,
    *,
    scratch_dir: str = "/var/tmp",
    run_id: str = "run",
    on_result: Callable[[ItemResult, int, int], None] | None = None,
) -> BatchOutcome:
    """Run items across MPI ranks via automaton's :class:`MPIAgentPool`.

    **Collective:** every rank must call this together. Rank 0 both coordinates and
    works; ranks 1..N-1 execute items. Only rank 0 returns results
    (``is_primary=True``).

    The pool's own per-job checkpoint is pointed at throwaway scratch and disabled
    (``resume=False``): this tool's durable resume is each stage's own artifacts
    (see the module docstring), and letting the pool also write done-markers into
    the output tree would create a second, competing source of truth.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from automaton.pool import AgentJob, JobResult, MPIAgentPool

    total = len(items)

    def _runner(job: AgentJob) -> JobResult:
        """Adapt this module's worker contract to the pool's."""
        item = job.payload
        result = _safe_call(worker, item, item_identity(item))
        return JobResult(
            job_id=job.job_id,
            success=result.ok,
            output_dir=str(job.output_dir),
            summary=result.to_dict(),
            error=None if result.ok else (result.detail or "item failed"),
        )

    checkpoint_root = Path(
        tempfile.mkdtemp(prefix=f"lbm-pool-{run_id}-", dir=os.environ.get("LBM_SCRATCH_DIR", scratch_dir))
    )
    try:
        pool = MPIAgentPool(
            list(items), _runner, checkpoint_root=checkpoint_root, resume=False
        )
        job_results = pool.run()
    finally:
        shutil.rmtree(checkpoint_root, ignore_errors=True)

    # Worker ranks get an empty list from the pool.
    is_primary = bool(job_results) or mpi_rank_size()[0] == 0
    results: list[ItemResult] = []
    for jr in job_results:
        summary = jr.summary or {}
        if summary.get("item_id"):
            result = ItemResult.from_dict(summary)
        else:
            result = ItemResult(
                item_id=jr.job_id,
                status="ok" if jr.success else "failed",
                detail=jr.error or "",
            )
        results.append(result)
        if on_result:
            on_result(result, len(results), total)

    return BatchOutcome(results=results, is_primary=is_primary)


# -- static round-robin over MPI ---------------------------------------------


class ParallelError(RuntimeError):
    """A parallel run cannot proceed safely."""


@dataclass
class World:
    """This process's place among cooperating processes.

    The trivial world (``rank 0 of 1``, no communicator) stands in whenever MPI
    is not in use, so a stage can be written once against this interface.
    """

    rank: int = 0
    size: int = 1
    comm: Any = None

    @property
    def is_primary(self) -> bool:
        """True on the rank that plans, merges, and reports."""
        return self.rank == 0

    def bcast(self, obj: Any) -> Any:
        """Return rank 0's ``obj`` on every rank."""
        return obj if self.comm is None else self.comm.bcast(obj, root=0)

    def gather(self, obj: Any) -> list[Any] | None:
        """Collect every rank's ``obj`` on rank 0 (``None`` elsewhere).

        Waits for all ranks first with a *polled* non-blocking barrier. A plain
        blocking collective would spin a core at 100% for as long as the slowest
        rank takes (hours, for an agent run), which is wasteful on a shared node.
        """
        if self.comm is None:
            return [obj]
        import time

        request = self.comm.Ibarrier()
        while not request.Test():
            time.sleep(1.0)
        return self.comm.gather(obj, root=0)


def launcher_size() -> int:
    """Return the task count a parallel launcher reports, or 1 if not launched.

    Read from the environment so it works even when MPI itself does not.
    """
    for var in ("SLURM_NTASKS", "OMPI_COMM_WORLD_SIZE", "PMI_SIZE", "MV2_COMM_WORLD_SIZE"):
        value = os.environ.get(var)
        if value:
            try:
                return max(1, int(value))
            except ValueError:
                continue
    return 1


def mpi_world(backend: str) -> World:
    """Return the MPI world for ``backend``, or the trivial world.

    Falls back to the trivial world (with a warning) when ``mpi`` is requested
    but there is only one rank. Refuses outright when MPI is unusable under a
    multi-task launcher: every task would then believe it is the only one and
    run the *whole* workload, concurrently, against the same output files.

    Raises
    ------
    ParallelError
        If MPI is unusable while a launcher started several tasks.
    """
    if backend != "mpi":
        return World()
    usable, reason = mpi_available()
    if not usable:
        if launcher_size() > 1:
            raise ParallelError(
                f"parallel backend 'mpi' is unusable, but the launcher started "
                f"{launcher_size()} tasks; refusing to let each run everything.\n"
                f"  {reason}"
            )
        print(
            f"WARNING: parallel backend 'mpi' is unusable; running serially "
            f"instead.\n         {reason}",
            file=sys.stderr,
        )
        return World()
    from mpi4py import MPI

    comm = MPI.COMM_WORLD
    if comm.Get_size() < 2:
        print(
            "WARNING: parallel backend 'mpi' requested but this process has only "
            "one rank; running serially instead.\n"
            "         Launch under a launcher, e.g. `srun -n 2 ...`.",
            file=sys.stderr,
        )
        return World()
    return World(rank=comm.Get_rank(), size=comm.Get_size(), comm=comm)


def round_robin(n_items: int, size: int) -> list[list[int]]:
    """Assign item indices to ranks round-robin: item ``i`` goes to rank ``i % size``.

    Returns one list of item indices per rank, in execution order.
    """
    if size < 1:
        raise ValueError(f"size must be >= 1, got {size}")
    return [list(range(rank, n_items, size)) for rank in range(size)]


def run_round_robin(
    items: Sequence[Item],
    worker: Worker,
    world: World,
    *,
    on_result: Callable[[ItemResult, int, int], None] | None = None,
) -> BatchOutcome:
    """Run items with a static round-robin schedule over ``world``, then gather.

    **Collective** when ``world`` is an MPI world: every rank must call this
    together. ``items`` is taken from rank 0 and broadcast, so other ranks may
    pass anything. Each rank runs its own share serially (``on_result`` is
    called on the rank that ran the item, with that rank's own counts); rank 0
    then receives every result, in item order.

    Static rather than dynamic (compare :func:`run_mpi`) because it suits a
    handful of long, similar items: the assignment is known up front and needs
    no coordinator.
    """
    shared = world.bcast(list(items) if world.is_primary else None)
    mine = round_robin(len(shared), world.size)[world.rank]

    local: list[tuple[int, dict[str, Any]]] = []
    for count, index in enumerate(mine, start=1):
        item = shared[index]
        result = _safe_call(worker, item, item_identity(item))
        local.append((index, result.to_dict()))
        if on_result:
            on_result(result, count, len(mine))

    gathered = world.gather(local)
    if not world.is_primary:
        return BatchOutcome(results=[], is_primary=False)
    ordered = sorted((pair for part in gathered or [] for pair in part), key=lambda p: p[0])
    return BatchOutcome(
        results=[ItemResult.from_dict(payload) for _, payload in ordered],
        is_primary=True,
    )


# -- dispatch ----------------------------------------------------------------


def run_batch(
    items: Sequence[Item],
    worker: Worker,
    *,
    backend: str = "serial",
    workers: int = 1,
    scratch_dir: str = "/var/tmp",
    run_id: str = "run",
    on_result: Callable[[ItemResult, int, int], None] | None = None,
) -> BatchOutcome:
    """Run a batch with the named backend.

    Falls back to ``serial`` — with an explanatory warning on stderr — when ``mpi``
    is requested but unusable (``mpi4py`` missing, or only one rank). Degrading
    loudly beats both alternatives: crashing wastes an allocation, and silently
    running single-threaded work that was sized for 32 ranks wastes far more.

    Raises
    ------
    ValueError
        For an unknown backend name.
    """
    if backend not in ("serial", "local", "mpi"):
        raise ValueError(
            f"unknown parallel backend {backend!r}; expected 'serial', 'local', "
            "or 'mpi'"
        )

    if backend == "mpi":
        usable, reason = mpi_available()
        if not usable:
            print(
                f"WARNING: parallel backend 'mpi' is unusable; running serially "
                f"instead.\n         {reason}",
                file=sys.stderr,
            )
            backend = "serial"
        else:
            _, size = mpi_rank_size()
            if size < 2:
                print(
                    "WARNING: parallel backend 'mpi' requested but this process "
                    "has only one rank; running serially instead.\n"
                    "         Launch under a launcher, e.g. `srun -n 8 ...`, or "
                    "use --parallel local.",
                    file=sys.stderr,
                )
                backend = "serial"

    if backend == "serial":
        return run_serial(items, worker, on_result=on_result)
    if backend == "local":
        return run_local(items, worker, workers=workers, on_result=on_result)
    return run_mpi(
        items,
        worker,
        scratch_dir=scratch_dir,
        run_id=run_id,
        on_result=on_result,
    )
