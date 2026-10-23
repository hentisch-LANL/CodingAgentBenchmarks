#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/conda/bin:${PATH}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-${SSL_CERT_FILE}}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-${SSL_CERT_FILE}}"
export CONDA_PLUGINS_AUTO_ACCEPT_TOS="yes"
unset CONDA_DEFAULT_ENV CONDA_EXE CONDA_PREFIX CONDA_PROMPT_MODIFIER CONDA_PYTHON_EXE CONDA_SHLVL
unset _CE_CONDA _CE_M

ENV_DIR="${ABC_BENCH_TASK_ENV:-${PWD}/abc-bench-task-env}"

if [[ ! -d "${ENV_DIR}" ]]; then
    conda create -y -p "${ENV_DIR}" python=3.12 numpy scipy
fi

conda run -p "${ENV_DIR}" python --version
