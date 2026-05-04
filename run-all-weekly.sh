#!/usr/bin/env bash
# 매주 동일 주간(직전 주 월~일)에 대해 call → kakao → viral 순으로 실행합니다.
# Elasticsearch 부하를 줄이기 위해 순차 실행합니다. (셋은 서로 독립적이라 순서 변경도 가능)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
./run-weekly-call_sessions.sh
./run-weekly-kakao_sessions.sh
./run-weekly-viral_marketing_logs.sh
