#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import socket
import traceback
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# =========================================================
# Config
# =========================================================
BASE_DIR = Path(os.environ.get("VIRAL_BASE_DIR", str(Path(__file__).resolve().parent)))
EXTRACT_SCRIPT = Path(
    os.environ.get(
        "EXTRACT_SCRIPT",
        str(BASE_DIR / "extract-viral_marketing.py"),
    )
)
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", str(BASE_DIR)))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))
CHECKPOINT_DIR = Path(os.environ.get("CHECKPOINT_DIR", str(BASE_DIR / "checkpoint")))

# Slack
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()

PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)

HOSTNAME = socket.gethostname()
START_TS = time.time()
LOG_LINES: list[str] = []


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_log_file(path: Path) -> None:
    ensure_dir(path.parent)
    path.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


def compute_last_saturday_friday(today: datetime | None = None) -> tuple[str, str]:
    """
    토요일 00:05 실행 기준, 방금 끝난 토~금(7일) 계산
    예:
      - 2026-04-04(토) 00:05 실행 -> 2026-03-28 ~ 2026-04-03
      - 2026-03-28(토) 00:05 실행 -> 2026-03-21 ~ 2026-03-27
    """
    if today is None:
        today = datetime.now()

    run_date = today.date()
    last_friday = run_date - timedelta(days=1)
    last_saturday = last_friday - timedelta(days=6)

    return last_saturday.strftime("%Y-%m-%d"), last_friday.strftime("%Y-%m-%d")


def send_slack_webhook(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        log("[WARN] SLACK_WEBHOOK_URL not set. Skip webhook message.")
        return

    payload = json.dumps({"text": text}).encode("utf-8")
    req = Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            _ = resp.read()
    except Exception as e:
        log(f"[WARN] Slack webhook send failed: {e}")


def slack_api_json(method: str, url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set.")

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack API HTTPError {e.code}: {body_text}") from e
    except URLError as e:
        raise RuntimeError(f"Slack API URLError: {e}") from e

    if not data.get("ok", False):
        raise RuntimeError(f"Slack API error: {data}")

    return data


def slack_api_form(url: str, payload: dict, timeout: int = 60) -> dict:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set.")

    body = urlencode(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack API HTTPError {e.code}: {body_text}") from e
    except URLError as e:
        raise RuntimeError(f"Slack API URLError: {e}") from e

    if not data.get("ok", False):
        raise RuntimeError(f"Slack API error: {data}")

    return data


def upload_file_bytes_to_presigned_url(upload_url: str, file_bytes: bytes, filename: str, timeout: int = 300) -> None:
    """
    Slack files.getUploadURLExternal 로 받은 presigned URL에 바이너리 업로드
    """
    req = Request(
        upload_url,
        data=file_bytes,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(file_bytes)),
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            _ = resp.read()
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Upload HTTPError {e.code}: {body_text}") from e
    except URLError as e:
        raise RuntimeError(f"Upload URLError: {e}") from e


def upload_file_to_slack(zip_path: Path) -> None:
    """
    Slack 공식 업로드 흐름
    1) files.getUploadURLExternal
    2) presigned URL 로 실제 업로드
    3) files.completeUploadExternal
    """
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set.")
    if not SLACK_CHANNEL_ID:
        raise RuntimeError("SLACK_CHANNEL_ID is not set.")

    file_bytes = zip_path.read_bytes()
    filename = zip_path.name
    length = len(file_bytes)

    log(f"Prepare Slack upload: {filename} ({length} bytes)")

    step1 = slack_api_form(
        "https://slack.com/api/files.getUploadURLExternal",
        payload={
            "filename": filename,
            "length": str(length),
        },
    )

    upload_url = step1["upload_url"]
    file_id = step1["file_id"]

    upload_file_bytes_to_presigned_url(upload_url, file_bytes, filename)

    slack_api_json(
        "POST",
        "https://slack.com/api/files.completeUploadExternal",
        payload={
            "files": [
                {
                    "id": file_id,
                    "title": filename,
                }
            ],
            "channel_id": SLACK_CHANNEL_ID,
        },
    )

    log("Slack file upload completed")


def build_success_text(start_date: str, end_date: str, zip_path: Path, elapsed_sec: int) -> str:
    lines = [
        "✅ viral_marketing 주간 리포트 완료",
        f"- 서버: {HOSTNAME}",
        f"- 기간: {start_date}~{end_date}",
        f"- ZIP 파일: {zip_path.name}",
        f"- 경로: {zip_path}",
        f"- 소요시간: {elapsed_sec}s",
        f"- 실행 시각: {now_str()}",
    ]
    return "\n".join(lines)


def build_failure_text(start_date: str | None, end_date: str | None, err: Exception) -> str:
    lines = [
        "❌ viral_marketing 주간 리포트 실패",
        f"- 서버: {HOSTNAME}",
        f"- 기간: {start_date or '-'}~{end_date or '-'}",
        f"- 에러: {type(err).__name__}: {err}",
        f"- 실행 시각: {now_str()}",
    ]
    return "\n".join(lines)


def run_extract_script(start_date: str, end_date: str) -> Path:
    if not EXTRACT_SCRIPT.exists():
        raise FileNotFoundError(f"Extract script not found: {EXTRACT_SCRIPT}")

    zip_name = f"viral_marketing_logs_{start_date}~{end_date}.zip"
    zip_path = OUTPUT_ROOT / zip_name

    cmd = [
        str(PYTHON_BIN),
        str(EXTRACT_SCRIPT),
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--out-dir",
        str(OUTPUT_ROOT),
        "--checkpoint-dir",
        str(CHECKPOINT_DIR),
        "--out-prefix",
        "viral_marketing_logs",
        "--zip-name",
        zip_name,
    ]

    log(f"Run extract script: {' '.join(cmd)}")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
    )

    if proc.stdout:
        for line in proc.stdout.splitlines():
            log(f"[extract][stdout] {line}")

    if proc.stderr:
        for line in proc.stderr.splitlines():
            log(f"[extract][stderr] {line}")

    if proc.returncode != 0:
        raise RuntimeError(f"Extract script failed with exit code {proc.returncode}")

    if not zip_path.exists():
        raise FileNotFoundError(f"Expected zip file not found: {zip_path}")

    return zip_path


def main():
    start_date = None
    end_date = None

    try:
        ensure_dir(OUTPUT_ROOT)
        ensure_dir(LOG_DIR)
        ensure_dir(CHECKPOINT_DIR)

        # 토요일 00:05 실행 기준: 방금 끝난 토~금 7일
        start_date, end_date = compute_last_saturday_friday()

        log("Job started")
        log(f"BASE_DIR={BASE_DIR}")
        log(f"EXTRACT_SCRIPT={EXTRACT_SCRIPT}")
        log(f"OUTPUT_ROOT={OUTPUT_ROOT}")
        log(f"LOG_DIR={LOG_DIR}")
        log(f"CHECKPOINT_DIR={CHECKPOINT_DIR}")
        log(f"TARGET_WEEK={start_date}~{end_date}")
        log(f"HOSTNAME={HOSTNAME}")

        send_slack_webhook(
            "\n".join([
                "🚀 viral_marketing 주간 리포트 시작",
                f"- 서버: {HOSTNAME}",
                f"- 기간: {start_date}~{end_date}",
                f"- 실행 시각: {now_str()}",
            ])
        )

        zip_path = run_extract_script(start_date, end_date)

        log(f"ZIP created: {zip_path}")
        upload_file_to_slack(zip_path)

        elapsed_sec = int(time.time() - START_TS)
        log_path = LOG_DIR / f"viral_marketing_logs_{start_date}~{end_date}.log"
        write_log_file(log_path)

        log(f"Log saved: {log_path}")
        log(f"Elapsed seconds: {elapsed_sec}")
        log("Job finished successfully")

        send_slack_webhook(build_success_text(start_date, end_date, zip_path, elapsed_sec))

    except Exception as e:
        tb = traceback.format_exc()
        log(f"[ERROR] {type(e).__name__}: {e}")
        log(tb)

        try:
            ensure_dir(LOG_DIR)
            fallback_name = datetime.now().strftime("failed_viral_marketing_%Y-%m-%d_%H-%M-%S.log")
            write_log_file(LOG_DIR / fallback_name)
        except Exception:
            pass

        try:
            send_slack_webhook(build_failure_text(start_date, end_date, e))
        except Exception:
            pass

        sys.exit(1)


if __name__ == "__main__":
    main()