#!/usr/bin/env bash
# 매주 월요일 로컬 시각 09:00에 run-all-weekly.sh 를 launchd 로 등록합니다.
# (주간 구간: 직전 ISO 주 월요일~일요일 — run-weekly-*.py 가 계산)
set -euo pipefail

LABEL="com.elasticsearch.watchdog.weekly-monday"
OLD_DAILY_LABEL="com.elasticsearch.watchdog.daily"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

mkdir -p "${REPO}/logs"

# 예전 매일 스케줄 plist 가 있으면 내림
launchctl bootout "${DOMAIN}/${OLD_DAILY_LABEL}" 2>/dev/null || true
rm -f "${HOME}/Library/LaunchAgents/${OLD_DAILY_LABEL}.plist" 2>/dev/null || true

export INSTALL_REPO_ROOT="${REPO}"
export INSTALL_PLIST_DST="${PLIST_DST}"
export INSTALL_LABEL="${LABEL}"
python3 <<'PY'
import os
import plistlib
from pathlib import Path

repo = Path(os.environ["INSTALL_REPO_ROOT"]).resolve()
dst = Path(os.environ["INSTALL_PLIST_DST"]).expanduser()
label = os.environ["INSTALL_LABEL"]
dst.parent.mkdir(parents=True, exist_ok=True)

path_env = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

pl = {
    "Label": label,
    "WorkingDirectory": str(repo),
    "EnvironmentVariables": {
        "PATH": path_env,
    },
    "ProgramArguments": [
        "/bin/bash",
        "-lc",
        "./run-all-weekly.sh >> logs/launchd-weekly-monday.log 2>&1",
    ],
    "StartCalendarInterval": {
        "Weekday": 1,
        "Hour": 9,
        "Minute": 0,
    },
    "StandardOutPath": str(repo / "logs" / "launchd-weekly-monday.stdout.log"),
    "StandardErrorPath": str(repo / "logs" / "launchd-weekly-monday.stderr.log"),
}

with open(dst, "wb") as f:
    plistlib.dump(pl, f, fmt=plistlib.FMT_XML)

print(f"Wrote {dst}")
PY

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
launchctl enable "${DOMAIN}/${LABEL}" 2>/dev/null || true

echo ""
echo "등록 완료: 매주 월요일 09:00 (로컬 시각) 에 ${REPO}/run-all-weekly.sh"
echo "plist: ${PLIST_DST}"
echo "StartCalendarInterval Weekday=1 은 Apple 기준 '월요일'(일요일=0) 입니다."
echo ""
echo "--- 지금 파이프라인만 테스트 (launchd 없이) ---"
echo "  cd \"${REPO}\" && ./run-all-weekly.sh"
echo ""
echo "--- 등록된 launchd 작업을 즉시 한 번 실행 ---"
echo "  launchctl kickstart -k ${DOMAIN}/${LABEL}"
echo ""
echo "--- 제거 ---"
echo "  ${REPO}/scripts/uninstall-weekly-monday-launchd.sh"
echo ""
