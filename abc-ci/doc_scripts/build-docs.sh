#!/usr/bin/env bash
# Build abc-ci documentation with MkDocs

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_DIR="${REPO_ROOT}/docs-env"
OUTPUT_DIR="${1:-${REPO_ROOT}/docs-site}"

# Resolve relative output paths from the repository root so CI and local use behave the same.
if [[ ! "${OUTPUT_DIR}" =~ ^/ ]]; then
    OUTPUT_DIR="${REPO_ROOT}/${OUTPUT_DIR}"
fi

echo -e "${GREEN}Building abc-ci documentation...${NC}"

# Check if we're in the right place
if [[ ! -f "${REPO_ROOT}/mkdocs.yml" ]]; then
    echo -e "${RED}Error: mkdocs.yml not found in ${REPO_ROOT}${NC}"
    exit 1
fi

# Check if docs environment exists and is runnable.
if [[ ! -f "${ENV_DIR}/conda-meta/history" ]] || ! conda run -p "${ENV_DIR}" python --version >/dev/null 2>&1; then
    echo -e "${YELLOW}Documentation environment not found. Setting up...${NC}"
    "${SCRIPT_DIR}/setup-docs-env.sh"
fi

# Build the documentation using the conda environment
cd "${REPO_ROOT}"
conda run -p "${ENV_DIR}" python -m mkdocs build --clean --strict --site-dir "${OUTPUT_DIR}"

echo -e "${GREEN}Documentation built successfully!${NC}"
echo -e "Output directory: ${OUTPUT_DIR}"
echo -e "To serve locally, run: ${SCRIPT_DIR}/serve-docs.sh"
