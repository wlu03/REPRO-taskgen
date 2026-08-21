#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="${ICPSR_AEA_VENV_DIR:-${script_dir}/.venv}"

# Load credentials for --browser-auto-login from an untracked .env file.
if [[ -f "${script_dir}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${script_dir}/.env"
  set +a
fi
python_command="${PYTHON_BIN:-python3}"

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  "${python_command}" -m venv "${venv_dir}"
fi

"${venv_dir}/bin/python" -m pip install \
  --disable-pip-version-check \
  --quiet \
  --requirement "${script_dir}/requirements.txt"

exec "${venv_dir}/bin/python" "${script_dir}/scraper.py" "$@"
