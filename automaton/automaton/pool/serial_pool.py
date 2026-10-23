"""Serial agent pool: run jobs one at a time, in-process.

:class:`SerialAgentPool` is a first-class scheduler that executes an
:class:`~automaton.pool.base.AgentPool`'s pending jobs sequentially in the
current process. It is **not** a fallback for MPI and never degrades from an MPI
pool to a serial one — it is simply the scheduler to use when you want
single-process execution (e.g. a laptop run, or a single-rank environment).

Like every :class:`AgentPool`, it honors checkpoint-aware resume: already-done
jobs are skipped and their recorded results included in the returned batch.
"""

from __future__ import annotations

from . import checkpoint
from .base import AgentPool
from .job import JobResult


class SerialAgentPool(AgentPool):
    """Run all pending jobs sequentially in the current process.

    The returned list covers the full job batch (previously-completed plus
    freshly-run), ordered by canonical job index.
    """

    def run(self) -> list[JobResult]:
        results: dict[str, JobResult] = {r.job_id: r for r in self.completed_results()}
        for job in self.pending_jobs():
            result = self.execute_job(job)
            results[job.job_id] = result
            checkpoint.append_manifest(self.checkpoint_root, result)
        return [results[job.job_id] for job in self._jobs if job.job_id in results]
