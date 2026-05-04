#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1090
eval "$(python3 "${ROOT}/env/dotenv_to_shell.py" "${ROOT}/.env" "${ROOT}")"
export VIRAL_BASE_DIR="${VM_VIRAL_BASE_DIR}"
export EXTRACT_SCRIPT="${VM_EXTRACT_SCRIPT}"
export OUTPUT_ROOT="${VM_OUTPUT_ROOT}"
export LOG_DIR="${VM_LOG_DIR}"
export CHECKPOINT_DIR="${VM_CHECKPOINT_DIR}"
export BATCH_SIZE="${VM_BATCH_SIZE}"
export SLEEP_SEC="${VM_SLEEP_SEC}"
# viral 은 openpyxl 이 필요 — 저장소 .venv 가 있으면 그걸 우선 (시스템 python 에 설치 불필요)
if [[ -x "${ROOT}/.venv/bin/python" ]] && "${ROOT}/.venv/bin/python" -c "import openpyxl" 2>/dev/null; then
  PYTHON_BIN="${ROOT}/.venv/bin/python"
else
  PYTHON_BIN="$(bash "${ROOT}/env/resolve_python_bin.sh" "${VM_PYTHON_BIN:-}" "${ROOT}")"
fi
export PYTHON_BIN
exec "${PYTHON_BIN}" "${VM_RUN_SCRIPT}"
