watchdog/ — 이 저장소 안에서만 쓰는 런타임 데이터 루트입니다.

  call_sessions/
    workspace/   … extract 실행 시 cwd (작업 스크래치)
    exports/     … 주간 ZIP·중간 JSON 등 산출물
    logs/        … run-weekly 로그

  kakao_sessions/
    workspace/ exports/ logs/

  viral/
    workspace/ exports/ logs/ checkpoint/ … ES 스크롤 체크포인트

최초 한 번:  ./scripts/init-watchdog-dirs.sh
.env 에는 이 폴더들에 대한 상대 경로만 적으면 됩니다 (저장소 루트 기준).
