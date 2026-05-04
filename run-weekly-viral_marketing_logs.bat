@echo off
setlocal

set VIRAL_BASE_DIR=C:\Users\YK\Desktop\Elasticsearch\viral
set EXTRACT_SCRIPT=C:\Users\YK\Desktop\Elasticsearch\viral\extract-viral_marketing.py
set OUTPUT_ROOT=C:\Users\YK\Desktop\Elasticsearch\viral
set LOG_DIR=C:\Users\YK\Desktop\Elasticsearch\viral\logs
set CHECKPOINT_DIR=C:\Users\YK\Desktop\Elasticsearch\viral\checkpoint

set ES_URL=http://localhost:19200
set ES_USER=
set ES_PASS=
set INSECURE_TLS=1

set BATCH_SIZE=2000
set SLEEP_SEC=0

REM Set SLACK_WEBHOOK_URL, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID in the environment before running (User/System env vars or set here without committing real values).

set PYTHON_BIN=python

"%PYTHON_BIN%" "C:\Users\YK\Desktop\Elasticsearch\viral\run-weekly-viral_marketing_logs.py"