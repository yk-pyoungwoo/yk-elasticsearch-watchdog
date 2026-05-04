#!/usr/bin/env bash
# watchdog/ 하위 작업 디렉터리를 한 번에 만듭니다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base="${ROOT}/watchdog"
for d in \
  call_sessions/workspace call_sessions/exports call_sessions/logs \
  kakao_sessions/workspace kakao_sessions/exports kakao_sessions/logs \
  viral/workspace viral/exports viral/logs viral/checkpoint
do
  mkdir -p "${base}/${d}"
  touch "${base}/${d}/.gitkeep"
done
echo "OK: ${base}/(call_sessions|kakao_sessions|viral)/..."
