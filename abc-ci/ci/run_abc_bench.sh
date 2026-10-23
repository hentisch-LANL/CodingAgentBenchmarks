#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# Keep the working directory off Python's import path (Python >= 3.11).
# Packages are installed explicitly from the sibling source checkouts below.
export PYTHONSAFEPATH=1

mkdir -p api_artifacts
export ABC_CI_ARTIFACTS_DIR="${ROOT_DIR}/api_artifacts"

python3 ci/validate_inputs.py

# Decide up front whether this is a multi-node MPI batch. NODES>1 means the run
# is distributed across the SLURM allocation via automaton's MPI agent pool (see
# the launch section below); NODES unset or 1 runs directly on the single node.
# Computed here (not just at launch) because it also determines whether we must
# install the mpi4py-bearing automaton[mpi] extra.
MPI_ENABLED=0
if [[ -n "${NODES:-}" && "${NODES}" -gt 1 ]]; then
  MPI_ENABLED=1
fi

# Resolve the effective results archive (production vs dev). Sets
# ABC_BENCH_EFFECTIVE_RESULTS_DIR based on the DEV trigger variable or the dev
# branch. This is sourced so the exports land in the current shell.
# shellcheck source=ci/resolve_results_dir.sh
source "${ROOT_DIR}/ci/resolve_results_dir.sh"

# Source directories are siblings, not submodules. The site pipeline must
# provide both checkouts before invoking this script. Editable installs retain
# access to benchmark harness/container assets in the source tree.
BENCH_SRC="$(cd "${ROOT_DIR}/../abc-bench" && pwd)"
AUTOMATON_ROOT="$(cd "${ROOT_DIR}/../automaton" && pwd)"
AUTOMATON_SRC="${AUTOMATON_ROOT}"
echo "=== Installing automaton + abc-bench from sibling checkouts ==="
# Multi-node runs require mpi4py and a compatible site-provided MPI runtime.
if [[ "${MPI_ENABLED}" -eq 1 ]]; then
  echo "NODES=${NODES} (>1): installing automaton with the [mpi] extra (mpi4py)"
  AUTOMATON_SRC="${AUTOMATON_SRC}[mpi]"
fi
python3 -m pip install -e "${AUTOMATON_SRC}"
python3 -m pip install -e "${BENCH_SRC}"

# Fail here, not mid-benchmark, if the imports resolve anywhere other than the
# source checkouts (e.g. a stale non-editable install in the environment).
python3 - "${ROOT_DIR}" <<'PY'
import sys
from pathlib import Path

import automaton
import abc_bench
import abc_bench.cli
from abc_bench.config import get_package_root

root = Path(sys.argv[1]).resolve()
expected = {
    "automaton": (root.parent / "automaton").resolve(),
    "abc_bench": (root.parent / "abc-bench").resolve(),
}


def source_root(module) -> Path | None:
    # A namespace package (the failure mode PYTHONSAFEPATH guards against) has
    # no __file__; report it as a mismatch rather than crashing on it.
    path = getattr(module, "__file__", None)
    return Path(path).resolve().parents[1] if path else None


actual = {
    "automaton": source_root(automaton),
    "abc_bench": source_root(abc_bench),
}
package_root = get_package_root().resolve()
cli_file = Path(abc_bench.cli.__file__).resolve()
print(f"automaton: {actual['automaton']}")
print(f"abc_bench: {actual['abc_bench']}")
print(f"abc_bench.cli: {cli_file}")
print(f"abc-bench package root: {package_root}")
bad = [name for name in expected if actual[name] != expected[name]]
if cli_file.parent != expected["abc_bench"] / "abc_bench":
    bad.append("abc_bench.cli")
if package_root != expected["abc_bench"]:
    bad.append("package_root")
if bad:
    raise SystemExit(f"ERROR: not running from sibling sources: {', '.join(bad)}")
PY

# Make the requested task group available to abc-bench. Benchmarks live in the
# separate benchmarks monorepo and are installed here from a source given by the
# TASK_GROUP_SOURCE CI variable (a directory, .sqfs image, or .tar.gz archive).
# If TASK_PATH is set instead, abc-bench discovers groups from it directly and no
# install is needed.
if [[ -n "${TASK_GROUP_SOURCE:-}" ]]; then
  echo "=== Installing task group(s) from ${TASK_GROUP_SOURCE} ==="
  python3 -m abc_bench.cli install "${TASK_GROUP_SOURCE}" --force
fi


WORK_ROOT="${ABC_BENCH_WORK_DIR:-${ROOT_DIR}/.abc-ci-work/${CI_PIPELINE_ID:-local}/${CI_JOB_ID:-manual}}"
WORK_ROOT="$(python3 - "${WORK_ROOT}" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
RUNTIME_CONFIG="${WORK_ROOT}/abc-bench-config.yaml"
mkdir -p "${WORK_ROOT}"

python3 ci/make_runtime_config.py "${ABC_BENCH_CONFIG}" "${RUNTIME_CONFIG}" "${WORK_ROOT}/config-results-unused"
export RUNTIME_CONFIG
STARTED_AT="$(python3 - <<'PY'
import time
print(time.time())
PY
)"

echo "=== abc-ci API run ==="
if [[ -n "${TASK:-}" ]]; then
  echo "Task: ${TASK}"
else
  echo "Task group: ${TASK_GROUP}"
fi
AGENT_HARNESS="$(python3 - <<'PY'
from automaton import Agent
from abc_bench.config import Config
import os

config = Config(os.environ["RUNTIME_CONFIG"])
agent = Agent(os.environ["AGENT"], config)
print(agent.harness_id)
PY
)"
echo "Agent: ${AGENT}"
echo "Harness: ${AGENT_HARNESS}"
echo "Archive root: ${ABC_BENCH_EFFECTIVE_RESULTS_DIR} (${ABC_BENCH_ARCHIVE_MODE})"
echo "Work root: ${WORK_ROOT}"
echo "Runtime config: ${RUNTIME_CONFIG}"

if [[ -n "${TASK_GROUP:-}" ]]; then
  BUILD_GROUP="${TASK_GROUP}"
else
  BUILD_GROUP="$(python3 - <<'PY'
from abc_bench.config import Config
from abc_bench.task import Task
import os

config = Config()
resolver = config.group_resolver()
task = Task(os.environ["TASK"], resolver)
print(task.group_id)
PY
)"
fi

RUNTIME_CONTAINER_REF="${AGENT_HARNESS}/${BUILD_GROUP}"

echo "=== Building runtime container ==="
echo "Container: ${RUNTIME_CONTAINER_REF}"
set +e
python3 -m abc_bench.cli build-container "${RUNTIME_CONTAINER_REF}"
BUILD_EXIT_CODE=$?
set -e

if [[ "${BUILD_EXIT_CODE}" -ne 0 ]]; then
  echo "ERROR: runtime container build failed with exit code ${BUILD_EXIT_CODE}" >&2
  RUN_EXIT_CODE="${BUILD_EXIT_CODE}"
else
  echo "=== Running benchmark ==="
  ATTEMPTS_ARG=()
  if [[ -n "${ATTEMPTS:-}" ]]; then
    ATTEMPTS_ARG=(--attempts "${ATTEMPTS}")
  fi

  # MPI_ENABLED was decided up front (NODES>1). When set, launch the run across
  # the job's NODES allocated nodes with srun (one rank per node); rank 0 both
  # dispatches and runs jobs, and abc-bench is told to use its MPI agent pool
  # via --mpi. Otherwise (single node) we run directly on the allocation.
  #
  # A step uses the existing allocation; partition/account/QoS belong in the
  # site's outer scheduler request, never in this portable launch command.
  LAUNCH_PREFIX=()
  MPI_FLAG=()
  if [[ "${MPI_ENABLED}" -eq 1 ]]; then
    LAUNCH_PREFIX=(srun --nodes="${NODES}" --ntasks="${NODES}" --ntasks-per-node=1)
    MPI_FLAG=(--mpi)
  fi

  if [[ -n "${TASK_GROUP:-}" ]]; then
    RUN_CMD=(
      "${LAUNCH_PREFIX[@]}"
      python3 -m abc_bench.cli run-tasks
      "${TASK_GROUP}/*"
      --agent "${AGENT}"
      --config "${RUNTIME_CONFIG}"
      --results-dir "${ABC_BENCH_EFFECTIVE_RESULTS_DIR}"
      "${ATTEMPTS_ARG[@]}"
      "${MPI_FLAG[@]}"
    )
  else
    RUN_CMD=(
      "${LAUNCH_PREFIX[@]}"
      python3 -m abc_bench.cli run-tasks
      "${TASK}"
      --agent "${AGENT}"
      --config "${RUNTIME_CONFIG}"
      --results-dir "${ABC_BENCH_EFFECTIVE_RESULTS_DIR}"
      "${ATTEMPTS_ARG[@]}"
      "${MPI_FLAG[@]}"
    )
  fi
  if [[ -n "${TIMEOUT_SECONDS:-}" ]]; then
    RUN_CMD+=(--timeout "${TIMEOUT_SECONDS}")
  fi

  set +e
  "${RUN_CMD[@]}"
  RUN_EXIT_CODE=$?
  set -e
fi

echo "=== Cleaning inconclusive results ==="
python3 ci/clean_inconclusive_results.py \
  --results-root "${ABC_BENCH_EFFECTIVE_RESULTS_DIR}" \
  --started-at "${STARTED_AT}" \
  --summary-out "${ABC_CI_ARTIFACTS_DIR}/inconclusive_cleanup.json" \
  ${TASK_GROUP:+--task-group "${TASK_GROUP}"}

echo "=== Collecting CI artifacts ==="
python3 ci/collect_artifacts.py \
  --results-root "${ABC_BENCH_EFFECTIVE_RESULTS_DIR}" \
  --started-at "${STARTED_AT}" \
  --exit-code "${RUN_EXIT_CODE}" \
  ${TASK_GROUP:+--task-group "${TASK_GROUP}"}

exit "${RUN_EXIT_CODE}"
