#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_COMMAND="${PYTHON_COMMAND:-python3}"
VENV_DIR="${PROJECT_DIR}/.venv"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_COMMAND}" -m venv "${VENV_DIR}"
fi

exec "${VENV_DIR}/bin/python" "${PROJECT_DIR}/scraper.py" "$@"

