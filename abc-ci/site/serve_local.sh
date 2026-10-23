#!/usr/bin/env bash
# serve_local.sh - Build and serve the abc-ci site locally for development
#
# This script:
# 1. Builds the site from the archive directory (or test data)
# 2. Serves it on localhost:8000
# 3. Opens it in your browser (if available)
#
# Usage:
#   ./serve_local.sh [archive_dir]
#
# If archive_dir is not provided, uses test_archive/ or creates sample data

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$REPO_ROOT/public"

# Determine archive directory
if [ $# -ge 1 ]; then
    ARCHIVE_DIR="$1"
else
    # Default to a local fixture archive; callers can select durable storage.
    DEFAULT_ARCHIVE_DIR="$REPO_ROOT/test_archive"
    # Support CI archive selection and the standalone builder variable.
    ARCHIVE_DIR="${ABC_BENCH_RESULTS_DIR:-${ABC_BENCH_ARCHIVE_DIR:-$(cd "$DEFAULT_ARCHIVE_DIR" 2>/dev/null && pwd || echo "$DEFAULT_ARCHIVE_DIR")}}"
fi

echo "=== abc-ci Local Site Builder ==="
echo "Archive directory: $ARCHIVE_DIR"
echo "Output directory: $OUTPUT_DIR"
echo ""

# Check if archive directory exists
if [ ! -d "$ARCHIVE_DIR" ]; then
    echo "Error: Archive directory does not exist: $ARCHIVE_DIR"
    echo "Please create it or specify a valid path as an argument."
    exit 1
fi

# Build the site
echo "Building site..."
export ABC_BENCH_ARCHIVE_DIR="$ARCHIVE_DIR"
export SITE_OUTPUT_DIR="$OUTPUT_DIR"

python3 "$SCRIPT_DIR/build.py"

if [ $? -ne 0 ]; then
    echo "Error: Build failed"
    exit 1
fi

echo ""
echo "=== Starting local server ==="
echo "Serving at: http://localhost:8000"
echo "Press Ctrl+C to stop"
echo ""

# Change to output directory and serve
cd "$OUTPUT_DIR"

# Try to open in browser (works on many systems)
if command -v xdg-open &> /dev/null; then
    xdg-open "http://localhost:8000" &> /dev/null &
elif command -v open &> /dev/null; then
    open "http://localhost:8000" &> /dev/null &
fi

# Start simple HTTP server
python3 -m http.server 8000
