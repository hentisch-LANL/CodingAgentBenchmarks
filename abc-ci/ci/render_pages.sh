#!/bin/bash
# render_pages.sh - Generate HTML pages for abc-ci
#
# Generate static HTML pages from the abc-bench results archive.
#
# REQUIRED ENVIRONMENT VARIABLES:
#   ABC_BENCH_RESULTS_DIR - Path to abc-bench results archive
#
# OUTPUT:
#   public/ directory with generated HTML pages

set -euo pipefail

echo "=== ABC-Bench Pages Generation ==="
echo ""

# Get output directory from first argument or use default
OUTPUT_DIR="${1:-./public}"

# Ensure output directory is absolute
if [[ ! "$OUTPUT_DIR" =~ ^/ ]]; then
    OUTPUT_DIR="$(pwd)/${OUTPUT_DIR}"
fi

# Resolve the effective results archive (production vs dev). Sets
# ABC_BENCH_EFFECTIVE_RESULTS_DIR based on the DEV variable or the dev branch.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ci/resolve_results_dir.sh
source "${SCRIPT_DIR}/resolve_results_dir.sh"

# Verify results directory exists
if [ -z "${ABC_BENCH_EFFECTIVE_RESULTS_DIR:-}" ]; then
    echo "ERROR: could not resolve an effective results directory"
    exit 1
fi

if [ ! -d "${ABC_BENCH_EFFECTIVE_RESULTS_DIR}" ]; then
    echo "ERROR: Results directory does not exist: ${ABC_BENCH_EFFECTIVE_RESULTS_DIR}"
    exit 1
fi

echo "Results directory: ${ABC_BENCH_EFFECTIVE_RESULTS_DIR}"
echo "Output directory: ${OUTPUT_DIR}"
echo ""

# Clean and recreate output directory
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

# Set environment variables for build script
export ABC_BENCH_ARCHIVE_DIR="${ABC_BENCH_EFFECTIVE_RESULTS_DIR}"
export SITE_OUTPUT_DIR="${OUTPUT_DIR}"

# Run the build script
echo "Generating HTML pages..."
python3 site/build.py

echo ""
echo "=== Complete ==="
echo "HTML pages generated in: ${OUTPUT_DIR}"
echo "Files:"
ls -lh "${OUTPUT_DIR}"
