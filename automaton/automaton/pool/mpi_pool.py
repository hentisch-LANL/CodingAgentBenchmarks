"""MPI-backed agent pool: rank 0 coordinates *and* works.

:class:`MPIAgentPool` executes an :class:`~automaton.pool.base.AgentPool`'s jobs
over ``MPI.COMM_WORLD`` using a master/worker topology in which **rank 0 does
both jobs at once**:

- On rank 0, a **background dispatcher thread** runs the coordinator loop:
  it hands pending job indices to whichever worker reports ready, receives
  lightweight :class:`~automaton.pool.job.JobResult` metadata as each finishes,
  appends each to the checkpoint-root manifest, and sends a shutdown sentinel
  once the queue drains. Rank 0's **main thread** simultaneously runs a worker
  loop, pulling its tasks from an in-process queue the dispatcher feeds. So no
  rank sits idle: rank 0 both schedules and executes.
- **Ranks 1..N-1** each run one agent at a time over MPI: receive a job index,
  execute it (writing artifacts to the job's ``output_dir`` on the shared
  filesystem, plus a definitive completion record on success), send back the
  result metadata, repeat.

Only :class:`JobResult` metadata crosses the MPI boundary; heavy artifacts live
on the (assumed shared) filesystem in each job's ``output_dir``.

Because rank 0 blocks in MPI ``recv`` on a background thread while its main
thread executes jobs, the MPI runtime must support at least
``MPI_THREAD_FUNNELED``/``MPI_THREAD_MULTIPLE``-style concurrency; mpi4py
releases the GIL during blocking calls, so the two threads coexist. Rank 0 is
the only thread issuing MPI calls on rank 0 (the main-thread worker uses an
in-process queue, never MPI), which keeps thread-safety requirements modest.

This module requires ``mpi4py`` (the ``automaton[mpi]`` extra). Importing it
without mpi4py installed raises a clear :class:`ImportError`.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

try:
    from mpi4py import MPI
except ImportError as exc:  # pragma: no cover - exercised via import guard
    raise ImportError(
        "MPIAgentPool requires mpi4py. Install it with: pip install 'automaton[mpi]'"
    ) from exc

from . import checkpoint
from .base import AgentPool
from .job import JobResult

# Message tags for the master/worker protocol (rank 0 <-> ranks 1..N-1).
_TAG_TASK = 1  # coordinator -> worker: a job index to run
_TAG_RESULT = 2  # worker -> coordinator: a JobResult dict
_TAG_READY = 3  # worker -> coordinator: initial "ready for work" ping

# Sentinel meaning "no more work; shut down". Used both as an MPI task payload
# to remote workers and as an in-process queue sentinel to the local worker.
_SHUTDOWN = None

# Special "remote rank" id the coordinator uses to address rank 0's own,
# in-process worker. Rank 0 talks to remote workers over MPI and to its local
# worker over thread-safe queues, but treats both uniformly in its bookkeeping.
_LOCAL_WORKER = 0


class MPIAgentPool(AgentPool):
    """Run agent jobs across MPI ranks with rank 0 coordinating *and* working.

    Parameters
    ----------
    *args, **kwargs
        Forwarded to :class:`~automaton.pool.base.AgentPool`.
    comm : mpi4py.MPI.Intracomm, optional
        Communicator to use. Defaults to ``MPI.COMM_WORLD``.
    """

    def __init__(self, *args: Any, comm: "MPI.Intracomm | None" = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.comm = comm if comm is not None else MPI.COMM_WORLD

    @property
    def rank(self) -> int:
        return self.comm.Get_rank()

    @property
    def size(self) -> int:
        return self.comm.Get_size()

    def run(self) -> list[JobResult]:
        """Execute all pending jobs across ranks.

        This is a **collective** operation: every rank in ``comm`` must call
        ``run()`` together. Rank 0 dispatches to and collects from every worker
        (including its own in-process worker), so a remote worker that returns
        early (or skips ``run()``) will deadlock the coordinator. If you invoke
        the pool more than once (e.g. a resume run), ensure all ranks
        participate each time.

        A single-rank communicator (``size == 1``) is valid: rank 0 coordinates
        and executes every job itself, with no MPI workers.

        Returns
        -------
        list[JobResult]
            On rank 0, results for the full job batch (previously-completed
            plus freshly-run), ordered by job index. On worker ranks, an empty
            list.
        """
        if self.rank == 0:
            return self._coordinate_and_work()
        self._work()
        return []

    # -- rank 0 ---------------------------------------------------------------

    def _coordinate_and_work(self) -> list[JobResult]:
        """Run the coordinator (background thread) and a local worker (here)."""
        pending = self.pending_jobs()
        # Seed with already-done jobs; freshly-run results are added as they
        # arrive (from remote workers, over MPI, and from the local worker, over
        # the results queue).
        results: dict[str, JobResult] = {r.job_id: r for r in self.completed_results()}
        results_lock = threading.Lock()

        # In-process channels between the dispatcher thread and this thread's
        # local worker. The dispatcher puts job indices (or _SHUTDOWN) on
        # local_tasks; the local worker puts JobResult dicts on local_results.
        local_tasks: "queue.Queue[int | None]" = queue.Queue()
        local_results: "queue.Queue[dict[str, Any]]" = queue.Queue()

        dispatcher = threading.Thread(
            target=self._dispatch,
            args=(pending, results, results_lock, local_tasks, local_results),
            name="mpi-agent-pool-dispatch",
            daemon=True,
        )
        dispatcher.start()

        # Local worker loop: mirror a remote worker, but over the queues.
        jobs_by_index = {job.index: job for job in self._jobs}
        while True:
            job_index = local_tasks.get()
            if job_index is _SHUTDOWN:
                break
            result = self.execute_job(jobs_by_index[job_index])
            local_results.put(result.to_dict())

        dispatcher.join()
        return [results[job.job_id] for job in self._jobs if job.job_id in results]

    def _dispatch(
        self,
        pending: list,
        results: dict[str, JobResult],
        results_lock: threading.Lock,
        local_tasks: "queue.Queue[int | None]",
        local_results: "queue.Queue[dict[str, Any]]",
    ) -> None:
        """Coordinator loop: schedule work to remote workers and the local one.

        Runs on rank 0's dispatcher thread. Remote workers are addressed over
        MPI; the local worker (rank 0's main thread) is addressed over the
        in-process queues. Both are tracked uniformly so no worker idles while
        work remains.
        """
        queue_indices = [job.index for job in pending]
        num_remote = self.size - 1

        # Number of tasks currently out for execution (remote + local).
        active = 0

        def record(payload: dict[str, Any]) -> None:
            result = JobResult.from_dict(payload)
            with results_lock:
                results[result.job_id] = result
            checkpoint.append_manifest(self.checkpoint_root, result)

        # Prime remote workers: each announces readiness, then gets one task or
        # an immediate shutdown if the queue is already empty.
        for _ in range(num_remote):
            status = MPI.Status()
            self.comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
            worker = status.Get_source()
            if queue_indices:
                self.comm.send(queue_indices.pop(0), dest=worker, tag=_TAG_TASK)
                active += 1
            else:
                self.comm.send(_SHUTDOWN, dest=worker, tag=_TAG_TASK)

        # Prime the local worker with its first task (if any). The local worker
        # must receive exactly one ``_SHUTDOWN`` over its lifetime; we track
        # whether it has been sent so we neither double-send nor leave the
        # worker blocked forever.
        local_busy = False
        local_shutdown_sent = False
        if queue_indices:
            local_tasks.put(queue_indices.pop(0))
            local_busy = True
            active += 1

        # Main loop: collect a finished result from either transport, then hand
        # that just-freed worker its next task (or a shutdown). Every completion
        # decrements ``active``; every fresh dispatch re-increments it, so
        # ``active`` always equals the number of tasks currently executing.
        while active > 0:
            # 1) Drain a completed local result without blocking, if any.
            if local_busy:
                try:
                    payload = local_results.get_nowait()
                except queue.Empty:
                    payload = None
                if payload is not None:
                    record(payload)
                    active -= 1
                    if queue_indices:
                        local_tasks.put(queue_indices.pop(0))
                        active += 1
                    else:
                        local_tasks.put(_SHUTDOWN)
                        local_busy = False
                        local_shutdown_sent = True
                    continue

            # 2) Service a completed remote result. Block on MPI only when the
            #    local worker is idle (nothing else could make progress);
            #    otherwise poll so we can interleave both transports.
            if num_remote == 0:
                if local_busy:
                    # Only the local worker can make progress: wait on it.
                    payload = local_results.get()
                    record(payload)
                    active -= 1
                    if queue_indices:
                        local_tasks.put(queue_indices.pop(0))
                        active += 1
                    else:
                        local_tasks.put(_SHUTDOWN)
                        local_busy = False
                        local_shutdown_sent = True
                continue

            got_remote = False
            if local_busy:
                got_remote = self.comm.iprobe(source=MPI.ANY_SOURCE, tag=_TAG_RESULT)
            if not local_busy or got_remote:
                status = MPI.Status()
                payload = self.comm.recv(
                    source=MPI.ANY_SOURCE, tag=_TAG_RESULT, status=status
                )
                worker = status.Get_source()
                record(payload)
                active -= 1
                if queue_indices:
                    self.comm.send(queue_indices.pop(0), dest=worker, tag=_TAG_TASK)
                    active += 1
                else:
                    self.comm.send(_SHUTDOWN, dest=worker, tag=_TAG_TASK)
            else:
                # Both a local task and remote workers are outstanding but none
                # has reported yet; yield briefly to avoid a hot spin.
                self._idle_wait(local_results)

        # Ensure the local worker receives its single shutdown even if it was
        # never primed (no pending work, or all work went to remote workers).
        if not local_shutdown_sent:
            local_tasks.put(_SHUTDOWN)

    @staticmethod
    def _idle_wait(local_results: "queue.Queue[dict[str, Any]]") -> None:
        """Sleep briefly to avoid a hot spin while both transports are idle."""
        import time

        time.sleep(0.005)

    # -- ranks 1..N-1 ---------------------------------------------------------

    def _work(self) -> None:
        jobs_by_index = {job.index: job for job in self._jobs}

        # Announce readiness so the coordinator can hand out the first task.
        self.comm.send(None, dest=0, tag=_TAG_READY)

        while True:
            job_index = self.comm.recv(source=0, tag=_TAG_TASK)
            if job_index is _SHUTDOWN:
                break
            job = jobs_by_index[job_index]
            result = self.execute_job(job)
            self.comm.send(result.to_dict(), dest=0, tag=_TAG_RESULT)
