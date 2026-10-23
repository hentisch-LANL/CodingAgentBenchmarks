#!/usr/bin/env bash
# resolve_results_dir.sh - Choose the effective abc-bench results archive.
#
# abc-ci supports a parallel "dev" archive so the dev branch (and
# benchmarks triggered with the launcher's --dev flag) can publish a separate
# GitLab Pages site and keep their results isolated from production.
#
# This script sets ABC_BENCH_EFFECTIVE_RESULTS_DIR to either the dev archive or
# the production archive, based on:
#   - DEV trigger variable (1/true) -> dev archive
#   - running on the dev branch ($CI_COMMIT_REF_NAME == $ABC_BENCH_DEV_REF) -> dev archive
#   - otherwise -> production archive (ABC_BENCH_RESULTS_DIR)
#
# It does NOT mutate ABC_BENCH_RESULTS_DIR; callers should read
# ABC_BENCH_EFFECTIVE_RESULTS_DIR. It is safe to source multiple times.
#
# Required (production): ABC_BENCH_RESULTS_DIR
# Required when dev mode is active: ABC_BENCH_RESULTS_DIR_DEV
# Optional: ABC_BENCH_DEV_REF (default: dev)

# Truthy check for the DEV flag.
_abc_bench_dev_requested=0
case "${DEV:-}" in
  1|true|True|TRUE|yes|Yes) _abc_bench_dev_requested=1 ;;
esac

# Also treat a pipeline running on the configured dev branch as dev mode, so a
# dev-branch scheduled/web pipeline archives to the dev dir without needing DEV.
_abc_bench_dev_ref="${ABC_BENCH_DEV_REF:-dev}"
if [ -n "${CI_COMMIT_REF_NAME:-}" ] && [ "${CI_COMMIT_REF_NAME}" = "${_abc_bench_dev_ref}" ]; then
  _abc_bench_dev_requested=1
fi

if [ "${_abc_bench_dev_requested}" -eq 1 ]; then
  if [ -z "${ABC_BENCH_RESULTS_DIR_DEV:-}" ]; then
    echo "ERROR: dev mode is active (DEV set or dev branch) but ABC_BENCH_RESULTS_DIR_DEV is not set" >&2
    return 1 2>/dev/null || exit 1
  fi
  export ABC_BENCH_EFFECTIVE_RESULTS_DIR="${ABC_BENCH_RESULTS_DIR_DEV}"
  export ABC_BENCH_ARCHIVE_MODE="dev"
else
  if [ -z "${ABC_BENCH_RESULTS_DIR:-}" ]; then
    echo "ERROR: ABC_BENCH_RESULTS_DIR is not set" >&2
    return 1 2>/dev/null || exit 1
  fi
  export ABC_BENCH_EFFECTIVE_RESULTS_DIR="${ABC_BENCH_RESULTS_DIR}"
  export ABC_BENCH_ARCHIVE_MODE="production"
fi

echo "Archive mode: ${ABC_BENCH_ARCHIVE_MODE}"
echo "Effective results dir: ${ABC_BENCH_EFFECTIVE_RESULTS_DIR}"
