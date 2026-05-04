#!/usr/bin/env bash
# 저장소 루트에 .venv 를 만들고 requirements.txt 를 설치합니다.
# viral 추출(openpyxl)은 시스템 python 에 패키지를 넣지 않고 여기서만 씁니다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON_FOR_VENV:-python3}"
"$PY" -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/pip install -r requirements.txt
echo "OK: use ${ROOT}/.venv/bin/python (run-weekly-viral_marketing_logs.sh picks this automatically)"
