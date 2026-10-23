#!/usr/bin/env bash
# Set up conda environment for abc-ci documentation

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_DIR="${REPO_ROOT}/docs-env"

echo -e "${GREEN}Setting up abc-ci documentation environment...${NC}"

# Check for conda
if ! command -v conda &> /dev/null; then
    echo -e "${RED}Error: conda not found. Please install Miniconda or Anaconda first.${NC}"
    exit 1
fi

ensure_working_env() {
    if [[ ! -f "${ENV_DIR}/conda-meta/history" ]]; then
        return 1
    fi

    if ! conda run -p "${ENV_DIR}" python --version >/dev/null 2>&1; then
        return 1
    fi

    if ! conda run -p "${ENV_DIR}" python -m pip --version >/dev/null 2>&1; then
        return 1
    fi

    return 0
}

# Create environment if it doesn't exist. A partially-created directory is not usable,
# so verify the conda metadata instead of checking only for the directory itself.
if ! ensure_working_env; then
    if [[ -d "${ENV_DIR}" ]]; then
        echo -e "${YELLOW}Removing incomplete or broken environment directory at ${ENV_DIR}...${NC}"
        rm -rf "${ENV_DIR}"
    fi

    echo -e "${YELLOW}Creating conda environment at ${ENV_DIR}...${NC}"
    conda create -y -p "${ENV_DIR}" python=3.12

    if ! ensure_working_env; then
        echo -e "${RED}Error: conda environment at ${ENV_DIR} is still unusable after creation.${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}Environment already exists at ${ENV_DIR}${NC}"
fi

# Activate environment and install dependencies
echo -e "${YELLOW}Installing documentation dependencies...${NC}"

# Use python -m pip so we validate the interpreter's pip module directly.
conda run -p "${ENV_DIR}" python -m pip install --upgrade pip
conda run -p "${ENV_DIR}" python -m pip install \
    "mkdocs>=1.5" \
    "mkdocs-material>=9.0" \
    "mkdocstrings[python]>=0.25" \
    "mkdocstrings-python>=1.0" \
    "numpy>=1.26" \
    "scipy>=1.12" \
    "pillow>=10.0"

# Install launcher package in editable mode for API docs
conda run -p "${ENV_DIR}" python -m pip install -e "${REPO_ROOT}/abc-ci-launcher"

echo -e "${GREEN}Documentation environment ready!${NC}"
echo -e "Environment location: ${ENV_DIR}"
echo -e ""
echo -e "To build documentation: ${SCRIPT_DIR}/build-docs.sh"
echo -e "To serve documentation: ${SCRIPT_DIR}/serve-docs.sh"
