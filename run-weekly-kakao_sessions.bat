@echo off
setlocal

set CALL_REPORT_BASE_DIR=C:\Users\YK\Desktop\Elasticsearch\footprints
set EXTRACT_SCRIPT=C:\Users\YK\Desktop\Elasticsearch\footprints\extract-kakao_sessions-footprints-weekly.py
set OUTPUT_ROOT=C:\Users\YK\Desktop\Elasticsearch\footprints
set LOG_DIR=C:\Users\YK\Desktop\Elasticsearch\footprints\logs

set ES_URL=http://localhost:19200
set ES_USER=
set ES_PASS=
set INSECURE_TLS=1

REM Set SLACK_WEBHOOK_URL, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID in the environment before running (User/System env vars or set here without committing real values).

set PYTHON_BIN=C:\Python314\python.exe

"%PYTHON_BIN%" "C:\Users\YK\Desktop\Elasticsearch\footprints\run-weekly-kakao_sessions.py"