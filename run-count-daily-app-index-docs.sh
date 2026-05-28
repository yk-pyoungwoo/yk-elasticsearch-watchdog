#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1090
eval "$(python3 "${ROOT}/env/dotenv_to_shell.py" "${ROOT}/.env" "${ROOT}")"
PYTHON_BIN="$(bash "${ROOT}/env/resolve_python_bin.sh" "${PYTHON_BIN:-python3}")"
exec "${PYTHON_BIN}" "${ROOT}/count-daily-app-index-docs.py" "$@"
