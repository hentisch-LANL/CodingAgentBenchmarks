# Slurm submission

Cluster-specific launch scripts are not distributed. Adapt this example to your
site's partition, account, time limits, modules, and approved model endpoint.
Charliecloud, the repository, container image, configuration, and run data must
be accessible on every allocated node. Install `../automaton`, `../abc-bench`,
and this component into a shared Python environment; add `.[mpi]` for MPI runs.

Run `abc-mine init` and `abc-mine preflight --write-config` with your configuration
before submitting the screening stage. Configure model credentials in your
environment or your site's secret mechanism, never in committed job scripts.

```bash
export LLM_BASE_URL=https://llm.example.org/v1
export LLM_MODEL=your-model-id
export LLM_API_KEY=your-secret
export LLM_API=chat_completions  # or the API style supported by your endpoint
sbatch --export=ALL --chdir="$PWD" mine.slurm
```

Example `mine.slurm`, submitted from the directory containing `mine.yaml`:

```bash
#!/usr/bin/env bash
#SBATCH --job-name=abc-mine-screen
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=02:00:00
#SBATCH --output=abc-mine-%j.log
set -euo pipefail

# Load site-provided Python/Charliecloud modules here, if required.
source /path/to/shared/venv/bin/activate
abc-mine screen --config mine.yaml --parallel local \
    --workers "${SLURM_CPUS_PER_TASK:-1}"
```

Set account and partition through `sbatch --account=YOUR_ACCOUNT
--partition=YOUR_PARTITION ...` when required. Choose worker counts that fit
memory and endpoint concurrency limits, not just CPU availability.

For multiple nodes, request the desired `--nodes` and `--ntasks-per-node`, load
your site's MPI runtime compatible with `mpi4py`, and replace the last command
with `srun abc-mine screen --config mine.yaml --parallel mpi`. Some sites require
`mpiexec` or additional `srun --mpi` settings; follow the local documentation.

After screening completes, run ranking, then submit build using the same job
pattern with `abc-mine build`. MPI ranking is useful for independent interleaving
groups; single-context ranking uses one agent. Resume an interrupted stage with
the same command and configuration; completed work is checkpointed. Inspect
progress with `abc-mine status --config mine.yaml` and export verified tasks with
`abc-mine export --config mine.yaml --dest ../abc-bench`.
