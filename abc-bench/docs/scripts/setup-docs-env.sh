#!/usr/bin/env bash
# Set up conda environment for ABC Bench documentation

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_DIR="${REPO_ROOT}/docs-env"

echo -e "${GREEN}Setting up ABC Bench documentation environment...${NC}"

# Check for conda
if ! command -v conda &> /dev/null; then
    echo -e "${RED}Error: conda not found. Please install Miniconda or Anaconda first.${NC}"
    exit 1
fi

# API documentation imports the sibling automaton component.
if [[ ! -f "${REPO_ROOT}/../automaton/pyproject.toml" ]]; then
    echo -e "${RED}Error: sibling ../automaton component is missing.${NC}" >&2
    exit 1
fi

# Create environment if it doesn't exist. A conda env hard-codes its absolute
# path into the shebang of every console script, so an env created under a
# different repo path still has a working ``bin/python`` (a real binary) but every
# entry point fails with "cannot execute: required file not found". Probe a
# console script rather than the interpreter, and rebuild rather than layering
# installs onto a broken prefix.
if [[ -d "${ENV_DIR}" ]] && ! "${ENV_DIR}/bin/pip" --version >/dev/null 2>&1; then
    echo -e "${YELLOW}Environment at ${ENV_DIR} is broken (likely created under a${NC}"
    echo -e "${YELLOW}different repo path); removing and recreating it...${NC}"
    rm -rf "${ENV_DIR}"
fi

if [[ ! -d "${ENV_DIR}" ]]; then
    echo -e "${YELLOW}Creating conda environment at ${ENV_DIR}...${NC}"
    conda create -y -p "${ENV_DIR}" python=3.12
else
    echo -e "${YELLOW}Environment already exists at ${ENV_DIR}${NC}"
fi

# Activate environment and install dependencies
echo -e "${YELLOW}Installing documentation dependencies...${NC}"

conda run -p "${ENV_DIR}" pip install "${REPO_ROOT}/../automaton"
conda run -p "${ENV_DIR}" pip install -e "${REPO_ROOT}[docs]"

# Fail loudly here rather than leaving a ${ENV_DIR} that later callers mistake
# for a working environment.
if ! conda run -p "${ENV_DIR}" mkdocs --version >/dev/null 2>&1; then
    echo -e "${RED}Error: mkdocs is still not runnable in ${ENV_DIR} after install.${NC}" >&2
    exit 1
fi

echo -e "${GREEN}Documentation environment ready!${NC}"
echo -e "Environment location: ${ENV_DIR}"
echo -e ""
echo -e "To use the environment:"
echo -e "  conda activate ${ENV_DIR}"
echo -e ""
echo -e "Or run commands directly:"
echo -e "  conda run -p ${ENV_DIR} mkdocs serve"
echo -e "  conda run -p ${ENV_DIR} mkdocs build"
