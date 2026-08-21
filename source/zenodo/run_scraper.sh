#!/usr/bin/env bash
set -euo pipefail

HARVESTER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARVESTER_PYTHON="${PYTHON:-python3}"
ZENODO_HARVESTER_PROG="${0##*/}"
export ZENODO_HARVESTER_PROG

"${HARVESTER_PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required")'

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="${HARVESTER_ROOT}/src:${PYTHONPATH}"
else
  export PYTHONPATH="${HARVESTER_ROOT}/src"
fi

cd "${HARVESTER_ROOT}"
exec "${HARVESTER_PYTHON}" -m zenodo "$@"
