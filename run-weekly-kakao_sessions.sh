#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1090
eval "$(python3 "${ROOT}/env/dotenv_to_shell.py" "${ROOT}/.env" "${ROOT}")"
export CALL_REPORT_BASE_DIR="${KS_CALL_REPORT_BASE_DIR}"
export EXTRACT_SCRIPT="${KS_EXTRACT_SCRIPT}"
export OUTPUT_ROOT="${KS_OUTPUT_ROOT}"
export LOG_DIR="${KS_LOG_DIR}"
PYTHON_BIN="$(bash "${ROOT}/env/resolve_python_bin.sh" "${KS_PYTHON_BIN:-}")"
export PYTHON_BIN
exec "${PYTHON_BIN}" "${KS_RUN_SCRIPT}"
