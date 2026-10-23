#!/usr/bin/env bash
# Build ABC Bench documentation with MkDocs

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_DIR="${REPO_ROOT}/docs-env"

echo -e "${GREEN}Building ABC Bench documentation...${NC}"

# Check if we're in the right place
if [[ ! -f "${REPO_ROOT}/mkdocs.yml" ]]; then
    echo -e "${RED}Error: mkdocs.yml not found in ${REPO_ROOT}${NC}"
    exit 1
fi

# Check if docs environment exists *and* works. Testing for the directory alone
# is not enough: a failed or path-stale setup leaves a ${ENV_DIR} that exists but
# cannot run mkdocs, and this script would then skip setup and die with
# "mkdocs: cannot execute". setup-docs-env.sh is idempotent and repairs it.
if ! conda run -p "${ENV_DIR}" mkdocs --version >/dev/null 2>&1; then
    echo -e "${YELLOW}Documentation environment missing or incomplete. Setting up...${NC}"
    "${SCRIPT_DIR}/setup-docs-env.sh"
fi

# Build the documentation using the conda environment
cd "${REPO_ROOT}"
conda run -p "${ENV_DIR}" mkdocs build --clean --strict

echo -e "${GREEN}Documentation built successfully!${NC}"
echo -e "Output directory: ${REPO_ROOT}/site"
echo -e "To serve locally, run: ${SCRIPT_DIR}/serve-docs.sh"
