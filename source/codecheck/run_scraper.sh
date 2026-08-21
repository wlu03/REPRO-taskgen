#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check \
  --quiet -r "${SCRIPT_DIR}/requirements.txt"

cd "${SCRIPT_DIR}"
exec "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/scraper.py" "$@"
