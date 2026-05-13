================================================================================
  Elasticsearch 주간 리포트 (call / kakao / viral) — 실행·자동화 가이드
================================================================================

■ 1. 한눈에 보기

  이 저장소에는 세 가지 파이프라인이 있습니다.

  (1) call_sessions_footprints … run-weekly-call_sessions.(sh|bat|py)
  (2) kakao_sessions_footprints … run-weekly-kakao_sessions.(sh|bat|py)
  (3) viral_marketing_logs … run-weekly-viral_marketing_logs.(sh|bat|py)

  “주간 자동”으로 돌릴 때 권장 순서는 아래와 같습니다. (같은 주간 구간을 처리)

    ① run-weekly-call_sessions
    ② run-weekly-kakao_sessions
    ③ run-weekly-viral_marketing_logs

  세 작업은 서로 데이터 의존이 없어 순서를 바꿔도 동작은 합니다. 다만 같은
  시각에 Elasticsearch에 동시에 무거운 쿼리를 걸면 부하가 커질 수 있어,
  자동 실행 시에는 한 번에 하나씩 순차 실행하는 것을 권장합니다.

  순차 실행을 한 줄로 하려면(맥 / 리눅스):

    ./run-all-weekly.sh

  (run-all-weekly.sh → 내부에서 위 ①②③ 순서로 각 run-weekly-*.sh 를 호출)


■ 2. 사전 준비

  [필수]
  - Python 3 (맥/리눅스: python3, PATH에 있어야 함)
  - viral 파이프라인만 openpyxl 필요. 권장(시스템 python 건드리지 않음):

      ./scripts/bootstrap-venv.sh

    → .venv 생성 후 run-weekly-viral_marketing_logs.sh 가 자동으로 .venv/bin/python 사용.
    대안: python3 -m pip install --user -r requirements.txt
  - call / kakao 추출 스크립트는 표준 라이브러리만 사용합니다.
  - Elasticsearch 가 ES_URL(.env)에서 접속 가능한 상태
  - 저장소 루트에 .env 파일 (비밀·경로 포함). 없으면 .env.example 을 복사해 채움

      cp .env.example .env
      # 편집기로 .env 수정: SLACK_*, ES_*, CS_*, KS_*, VM_* (기본은 모두 상대 경로)

  [경로 — watchdog/ 레이아웃]
  - 산출·로그는 저장소 안 watchdog/ 아래에 둡니다. watchdog/README.txt 참고.
  - .env 의 CS_/KS_/VM_ 경로는 저장소 루트 기준 상대 경로로 적습니다
    (예: CS_RUN_SCRIPT=run-weekly-call_sessions.py).
  - 실행 시 env/dotenv_to_shell.py · bootstrap_dotenv.py 가 절대 경로로 바꿉니다.
  - 최초 한 번: ./scripts/init-watchdog-dirs.sh

  [절대 경로]
  - 맥/윈도 모두 기존처럼 /… 또는 C:\… 를 그대로 적어도 됩니다.

  [권한]
  - 맥에서 최초 한 번: chmod +x run-weekly-*.sh run-all-weekly.sh scripts/*.sh


■ 3. 수동 실행 — “주간 일괄” (권장, Slack 업로드·ZIP·로그까지)

  주간 구간은 스크립트가 자동 계산합니다. (직전 ISO 주 월요일~일요일 7일;
   월요일 아침 스케줄에 맞춰 “바로 지난 주 월~일” 데이터를 처리합니다.)

  [맥 / 리눅스 — 각각 단독 실행]

    cd /절대경로/elasticsearch
    ./run-weekly-call_sessions.sh
    ./run-weekly-kakao_sessions.sh
    ./run-weekly-viral_marketing_logs.sh

  또는 순차 한 번에:

    ./run-all-weekly.sh

  [Windows — 각각 단독 실행]

    run-weekly-call_sessions.bat
    run-weekly-kakao_sessions.bat
    run-weekly-viral_marketing_logs.bat

  (.bat 은 저장소 루트의 .env 를 python 으로 읽어 임시 배치를 만든 뒤,
   CS_/KS_/VM_ 값을 각 작업용 환경 변수로 매핑합니다. python 이 PATH에 있어야 합니다.)

  [Python 직접 호출 — .env 대신 이미 셸에 환경 변수를 export 한 경우]

    python3 run-weekly-call_sessions.py
    python3 run-weekly-kakao_sessions.py
    python3 run-weekly-viral_marketing_logs.py

  내부 동작 요약: 각 run-weekly-*.py 가 (1) 주간 날짜 계산 (2) 대응하는
  extract-*.py 를 하위 프로세스로 실행 (3) 결과 ZIP·로그·Slack 알림.


■ 4. 수동 실행 — “extract만” (기간을 직접 지정할 때)

  주간 래퍼 없이 Elasticsearch에서만 뽑아 JSON/NDJSON 등을 만들 때 사용합니다.
  (Slack·ZIP 래퍼 없음; ES_URL 등은 환경 변수로 넘김)

  call:

    python3 extract-call_sessions-footprints-weekly.py --start-date 2026-04-01 --end-date 2026-04-30

  kakao:

    python3 extract-kakao_sessions-footprints-weekly.py --start-date 2026-04-01 --end-date 2026-04-30

  viral (옵션·경로는 스크립트 --help 참고):

    python3 extract-viral_marketing.py --start-date 2026-04-01 --end-date 2026-04-30 --out-dir ... --checkpoint-dir ...

  session_uuid 목록으로 기간 내 footprints만 뽑기 (input.txt, Slack·ZIP 없음):

    python3 extract-sessions-footprints-by-uuid.py \
      --input-file input.txt \
      --start-date 2026-04-01 \
      --end-date 2026-04-30 \
      --out sessions_footprints_by_uuid_2026-04-01~2026-04-30.json

    - input.txt: 한 줄에 session_uuid 하나. 빈 줄과 # 으로 시작하는 주석 줄은 무시.
    - --out 을 생략하면 현재 디렉터리에 sessions_footprints_by_uuid_<시작>~<끝>.json 이 생성됨.
    - KST 날짜 구간(--start-date / --end-date 필수)에 해당하는 인덱스·시간 범위만 조회함.
      더 넓은 기간이 필요하면 날짜만 넓히면 됨.

  call / kakao 주간 추출 JSON 및 위 by-uuid 출력에는 세션 객체 최상단에 local_uuid 가
  session_uuid 위에 오고, footprints 항목에는 넣지 않는다. 값은 ES _source 에서 읽으며,
  기본으로 필드명 local_uuid 와 localUuid 를 순서대로 시도한다. 다른 이름이거나
  중첩이면 환경 변수 ES_LOCAL_UUID_SOURCE_KEYS 로 쉼표 구분 목록을 지정한다.
  (점으로 경로 가능. 예: local_uuid,context.localUuid — context 가 _source 에 포함되도록
  자동으로 요청 필드에 붙는다.)

    export ES_LOCAL_UUID_SOURCE_KEYS="local_uuid,localUuid,context.local_uuid"

  어떤 키에서도 값이 없으면 local_uuid 는 null.

  이 경우 OUT_DIR, ES_URL 등은 실행 전에 export 하거나 .env 를 셸에서 불러와야 합니다.


■ 5. 자동 실행 — 매주 월요일 오전 9시 (맥 추천: launchd)

  “어떤 스크립트를 어떤 순서로?”
    → 권장: run-all-weekly.sh 하나만 등록하면 내부에서 ①②③ 순서로 실행됩니다.
    → 또는 동일 시각에 run-weekly-*.sh 세 개를 각각 등록(병렬)할 수 있으나 ES 부하 증가.

  [방법 0] 스크립트로 plist 생성·등록 (가장 간단)

  저장소 루트에서:

    ./scripts/install-weekly-monday-launchd.sh

  - ~/Library/LaunchAgents/com.elasticsearch.watchdog.weekly-monday.plist
  - 매주 월요일 09:00 (로컬 시각), StartCalendarInterval Weekday=1 (Apple: 일요일=0, 월요일=1)
  - 제거(스크립트): ./scripts/uninstall-weekly-monday-launchd.sh
  - 지금 파이프라인만 테스트: ./run-all-weekly.sh
  - launchd 에 올린 작업을 즉시 한 번 실행:

      launchctl kickstart -k gui/$(id -u)/com.elasticsearch.watchdog.weekly-monday

  [등록 여부 확인]

  1) plist 가 있는지

      ls -la ~/Library/LaunchAgents/com.elasticsearch.watchdog.weekly-monday.plist

  2) launchd 가 작업을 알고 있는지 (에러 없이 상세가 나오면 로드된 상태)

      launchctl print "gui/$(id -u)/com.elasticsearch.watchdog.weekly-monday"

     "Could not find service" 이면 등록 안 됐거나 이미 제거된 것입니다.

  3) 사용자 에이전트 목록에서 라벨 검색

      launchctl list | grep elasticsearch.watchdog

  4) 트리거(요일·시각) 내용만 보기

      plutil -p ~/Library/LaunchAgents/com.elasticsearch.watchdog.weekly-monday.plist

  5) 실제로 돌았는지 로그 (실행 후)

      ls -lt logs/launchd-weekly-monday*.log logs/launchd-weekly-monday.log 2>/dev/null

  [스케줄 완전 삭제]

  - 권장: 저장소에서

      ./scripts/uninstall-weekly-monday-launchd.sh

  - 수동으로 같게 하려면:

      launchctl bootout "gui/$(id -u)/com.elasticsearch.watchdog.weekly-monday"
      rm -f ~/Library/LaunchAgents/com.elasticsearch.watchdog.weekly-monday.plist

    bootout 후 plist 가 남아 있으면 재부팅 시 다시 올라올 수 있으므로 rm 까지 하는 것이 안전합니다.

  [참고] 예전 매일 스케줄(daily) plist 가 남아 있으면 이름이 다릅니다.

      ls ~/Library/LaunchAgents/com.elasticsearch.watchdog.*

  [방법 A] launchd — plist 를 직접 쓰는 경우 (맥, 재부팅 후에도 유지)

  1) plist 예시 파일을 만듭니다 (경로는 본인 환경에 맞게 수정).

    ~/Library/LaunchAgents/com.yk.elasticsearch.run-all-weekly.plist

  2) 내용 예시 (레포 절대 경로를 YOUR_REPO 로 바꿉니다):

    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>Label</key>
      <string>com.yk.elasticsearch.run-all-weekly</string>
      <key>ProgramArguments</key>
      <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>cd YOUR_REPO && ./run-all-weekly.sh >> YOUR_REPO/logs/launchd-weekly.log 2>&1</string>
      </array>
      <key>StartCalendarInterval</key>
      <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
      </dict>
      <key>StandardOutPath</key>
      <string>YOUR_REPO/logs/launchd-weekly.stdout.log</string>
      <key>StandardErrorPath</key>
      <string>YOUR_REPO/logs/launchd-weekly.stderr.log</string>
    </dict>
    </plist>

  - Weekday: Apple 문서 기준 일요일=0 … 월요일=1 입니다. (다른 문서와 다르면
    한 번 테스트 실행으로 요일을 확인하세요.)
  - logs 디렉터리가 없으면 mkdir logs 로 만듭니다.

  3) 등록 및 적재

    launchctl load ~/Library/LaunchAgents/com.yk.elasticsearch.run-all-weekly.plist

  4) 해제 시

    launchctl unload ~/Library/LaunchAgents/com.yk.elasticsearch.run-all-weekly.plist

  [방법 B] crontab (맥/리눅스)

    crontab -e

  아래 한 줄 (YOUR_REPO 를 실제 절대 경로로 변경; 월요일=1, 9시=9):

    0 9 * * 1 cd YOUR_REPO && ./run-all-weekly.sh >> YOUR_REPO/logs/cron-weekly.log 2>&1

  cron 은 기본적으로 Mac의 로컬 타임존을 사용합니다. 서버가 UTC면 9시 의미가
  달라지므로 TZ 를 명시하거나 서버 시간에 맞춰 시·분을 조정하세요.


■ 6. 자동 실행 — Windows (작업 스케줄러)

  1) “기본 작업 만들기” 또는 작업 스케줄러에서 새 작업
  2) 트리거: 매주 월요일 09:00
  3) 동작: 프로그램 시작
       프로그램: cmd.exe
       인수: /c cd /d YOUR_REPO && run-all-weekly.bat

  run-all-weekly.bat 은 저장소 루트에서 다음을 순서대로 call 합니다.
    run-weekly-call_sessions.bat → run-weekly-kakao_sessions.bat → run-weekly-viral_marketing_logs.bat


■ 7. 실패 시 확인할 것

  - .env 가 루트에 있고, CS_/KS_/VM_ 경로가 실제 파일과 일치하는지
  - python / python3 가 PATH에 있는지 (맥: dotenv_to_shell.py 는 python3 고정)
  - Elasticsearch 가 ES_URL 에 응답하는지
  - Slack 토큰·웹훅이 유효한지 (로그에 WARN 이 있는지)


================================================================================
  문서 끝
================================================================================
