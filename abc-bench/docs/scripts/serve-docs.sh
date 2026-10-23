#!/usr/bin/env bash
# Serve ABC Bench documentation locally with live reload

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_DIR="${REPO_ROOT}/docs-env"

# Default values
PORT=8000
DEV_ADDR="127.0.0.1"
OPEN_BROWSER=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            PORT="$2"
            shift 2
            ;;
        --dev-addr)
            DEV_ADDR="$2"
            shift 2
            ;;
        --open)
            OPEN_BROWSER=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Serve ABC Bench documentation locally with live reload."
            echo ""
            echo "Options:"
            echo "  --port PORT       Port to serve on (default: 8000)"
            echo "  --dev-addr ADDR   Address to bind to (default: 127.0.0.1)"
            echo "  --open            Open browser after starting"
            echo "  --help, -h        Show this help"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

echo -e "${GREEN}Starting ABC Bench documentation server...${NC}"

# Check if we're in the right place
if [[ ! -f "${REPO_ROOT}/mkdocs.yml" ]]; then
    echo -e "${RED}Error: mkdocs.yml not found in ${REPO_ROOT}${NC}"
    exit 1
fi

# Check if docs environment exists *and* works; a directory-only test lets a
# failed or path-stale setup through (see build-docs.sh).
if ! conda run -p "${ENV_DIR}" mkdocs --version >/dev/null 2>&1; then
    echo -e "${YELLOW}Documentation environment missing or incomplete. Setting up...${NC}"
    "${SCRIPT_DIR}/setup-docs-env.sh"
fi

cd "${REPO_ROOT}"

# Build first to catch any errors
echo -e "${YELLOW}Building documentation...${NC}"
conda run -p "${ENV_DIR}" mkdocs build --clean --strict

echo -e "${GREEN}Starting server on http://${DEV_ADDR}:${PORT}${NC}"
echo -e "${YELLOW}Press Ctrl+C to stop${NC}"

# Open browser if requested
if [[ "${OPEN_BROWSER}" == "true" ]]; then
    # Try to open browser (works on Linux/macOS)
    if command -v xdg-open &> /dev/null; then
        xdg-open "http://${DEV_ADDR}:${PORT}" &
    elif command -v open &> /dev/null; then
        open "http://${DEV_ADDR}:${PORT}" &
    fi
fi

# Serve with live reload
conda run -p "${ENV_DIR}" mkdocs serve --dev-addr="${DEV_ADDR}:${PORT}"
