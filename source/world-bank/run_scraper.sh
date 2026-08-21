#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${SCRAPER_VENV_DIR:-${SCRIPT_DIR}/.venv}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    printf 'Error: Python executable not found: %s\n' "${PYTHON_BIN}" >&2
    printf 'Install Python 3.10+ or set PYTHON_BIN to its executable.\n' >&2
    exit 127
fi

if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    printf 'Error: Python 3.10 or newer is required.\n' >&2
    exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/scraper.py" ]]; then
    printf 'Error: scraper.py was not found in %s\n' "${SCRIPT_DIR}" >&2
    exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    printf 'Error: requirements.txt was not found in %s\n' "${SCRIPT_DIR}" >&2
    exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    printf 'Creating virtual environment at %s\n' "${VENV_DIR}"
    if ! "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
        printf 'Error: could not create the virtual environment. Install the Python venv module and try again.\n' >&2
        exit 1
    fi
fi

PIP_DISABLE_PIP_VERSION_CHECK=1 \
    "${VENV_DIR}/bin/python" -m pip install \
    --quiet \
    --requirement "${SCRIPT_DIR}/requirements.txt"

cd -- "${SCRIPT_DIR}"
exec "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/scraper.py" "$@"
