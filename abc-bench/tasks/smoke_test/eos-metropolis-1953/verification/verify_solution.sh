#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

WORKSPACE="${ABC_BENCH_WORKSPACE:-${RUN_ROOT}/workspace}"
if [[ ! -f "${WORKSPACE}/solution.py" && -f "${RUN_ROOT}/solution.py" ]]; then
  WORKSPACE="${RUN_ROOT}"
fi

if [[ ! -f "${WORKSPACE}/solution.py" ]]; then
  echo "FAIL"
  echo "Could not find solution.py in ${WORKSPACE}" >&2
  exit 1
fi

export ABC_BENCH_WORKSPACE="${WORKSPACE}"
TASK_ENV="${ABC_BENCH_TASK_ENV:-${WORKSPACE}/abc-bench-task-env}"
PYTHON_BIN="python"
if [[ -x "${TASK_ENV}/bin/python" ]]; then
  PYTHON_BIN="${TASK_ENV}/bin/python"
fi

if "${PYTHON_BIN}" "${SCRIPT_DIR}/test_solution.py"; then
  echo "PASS"
else
  echo "FAIL"
  exit 1
fi
