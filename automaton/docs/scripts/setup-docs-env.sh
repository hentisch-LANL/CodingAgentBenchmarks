#!/usr/bin/env bash
# Set up conda environment for Automaton documentation

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_DIR="${REPO_ROOT}/docs-env"

echo -e "${GREEN}Setting up Automaton documentation environment...${NC}"

# Check for conda
if ! command -v conda &> /dev/null; then
    echo -e "${RED}Error: conda not found. Please install Miniconda or Anaconda first.${NC}"
    exit 1
fi

# Create environment if it doesn't exist
if [[ ! -d "${ENV_DIR}" ]]; then
    echo -e "${YELLOW}Creating conda environment at ${ENV_DIR}...${NC}"
    conda create -y -p "${ENV_DIR}" python=3.12
else
    echo -e "${YELLOW}Environment already exists at ${ENV_DIR}${NC}"
fi

# Activate environment and install dependencies
echo -e "${YELLOW}Installing documentation dependencies...${NC}"

# Use conda run to execute in the environment
conda run -p "${ENV_DIR}" pip install -e "${REPO_ROOT}[docs]"

echo -e "${GREEN}Documentation environment ready!${NC}"
echo -e "Environment location: ${ENV_DIR}"
echo -e ""
echo -e "To use the environment:"
echo -e "  conda activate ${ENV_DIR}"
echo -e ""
echo -e "Or run commands directly:"
echo -e "  conda run -p ${ENV_DIR} mkdocs serve"
echo -e "  conda run -p ${ENV_DIR} mkdocs build"
