#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import socket
import shutil
import zipfile
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
BASE_DIR = Path(os.environ.get("CALL_REPORT_BASE_DIR", str(Path(__file__).resolve().parent)))
EXTRACT_SCRIPT = Path(
    os.environ.get(
        "EXTRACT_SCRIPT",
        str(BASE_DIR / "extract-call_sessions-footprints-weekly.py"),
    )
)
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", str(BASE_DIR)))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))

# Slack
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()

# Optional
PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)
KEEP_WEEK_FOLDER = os.environ.get("KEEP_WEEK_FOLDER", "1") == "1"

# =========================================================
# Utils
# =========================================================
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
      - 2026-04-04 00:05:00 (토) 실행 -> 2026-03-28 ~ 2026-04-03
      - 2026-03-28 00:05:00 (토) 실행 -> 2026-03-21 ~ 2026-03-27
    """
    if today is None:
        today = datetime.now()

    run_date = today.date()

    # 토요일 실행 기준 직전 금요일
    last_friday = run_date - timedelta(days=1)
    last_saturday = last_friday - timedelta(days=6)

    return last_saturday.strftime("%Y-%m-%d"), last_friday.strftime("%Y-%m-%d")


def iter_dates(start_ymd: str, end_ymd: str):
    s = datetime.strptime(start_ymd, "%Y-%m-%d").date()
    e = datetime.strptime(end_ymd, "%Y-%m-%d").date()
    cur = s
    while cur <= e:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


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


def upload_file_to_slack(zip_path: Path, initial_comment: str) -> None:
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

    # 1) 업로드 URL 요청
    step1 = slack_api_form(
        "https://slack.com/api/files.getUploadURLExternal",
        payload={
            "filename": filename,
            "length": str(length),
        },
    )

    upload_url = step1["upload_url"]
    file_id = step1["file_id"]

    # 2) 실제 업로드
    upload_file_bytes_to_presigned_url(upload_url, file_bytes, filename)

    # 3) 업로드 완료 및 채널 공유
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
            "initial_comment": initial_comment,
        },
    )


def zip_files(zip_path: Path, files: list[Path]) -> None:
    ensure_dir(zip_path.parent)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            zf.write(file_path, arcname=file_path.name)


def build_summary_text(start_date: str, end_date: str, json_files: list[Path], zip_path: Path, elapsed_sec: int) -> str:
    lines = [
        "✅ call_sessions_footprints 주간 리포트 완료",
        f"- 서버: {HOSTNAME}",
        f"- 기간: {start_date}~{end_date}",
        f"- JSON 파일 수: {len(json_files)}",
        f"- ZIP 파일: {zip_path.name}",
        f"- 소요시간: {elapsed_sec}s",
        f"- 실행 시각: {now_str()}",
    ]
    return "\n".join(lines)


def build_failure_text(start_date: str | None, end_date: str | None, err: Exception) -> str:
    lines = [
        "❌ call_sessions_footprints 주간 리포트 실패",
        f"- 서버: {HOSTNAME}",
        f"- 기간: {start_date or '-'}~{end_date or '-'}",
        f"- 에러: {type(err).__name__}: {err}",
        f"- 실행 시각: {now_str()}",
    ]
    return "\n".join(lines)


def run_extract_script(start_date: str, end_date: str, out_dir: Path) -> None:
    if not EXTRACT_SCRIPT.exists():
        raise FileNotFoundError(f"Extract script not found: {EXTRACT_SCRIPT}")

    cmd = [
        str(PYTHON_BIN),
        str(EXTRACT_SCRIPT),
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--out-dir",
        str(out_dir),
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


def find_expected_json_files(out_dir: Path, start_date: str, end_date: str) -> list[Path]:
    files = []
    for day in iter_dates(start_date, end_date):
        fp = out_dir / f"call_sessions_footprints_{day}.json"
        if not fp.exists():
            raise FileNotFoundError(f"Expected JSON file not found: {fp}")
        files.append(fp)
    return files


def main():
    start_date = None
    end_date = None

    try:
        ensure_dir(OUTPUT_ROOT)
        ensure_dir(LOG_DIR)

        # 토요일 00:05 실행 기준: 방금 끝난 토~금 7일
        start_date, end_date = compute_last_saturday_friday()
        week_name = f"{start_date}~{end_date}"
        week_out_dir = OUTPUT_ROOT / week_name
        ensure_dir(week_out_dir)

        log("Job started")
        log(f"BASE_DIR={BASE_DIR}")
        log(f"EXTRACT_SCRIPT={EXTRACT_SCRIPT}")
        log(f"OUTPUT_ROOT={OUTPUT_ROOT}")
        log(f"LOG_DIR={LOG_DIR}")
        log(f"TARGET_WEEK={start_date}~{end_date}")
        log(f"HOSTNAME={HOSTNAME}")

        send_slack_webhook(
            "\n".join([
                "🚀 call_sessions_footprints 주간 리포트 시작",
                f"- 서버: {HOSTNAME}",
                f"- 기간: {start_date}~{end_date}",
                f"- 실행 시각: {now_str()}",
            ])
        )

        run_extract_script(start_date, end_date, week_out_dir)

        json_files = find_expected_json_files(week_out_dir, start_date, end_date)
        for fp in json_files:
            log(f"Generated JSON: {fp}")

        zip_path = OUTPUT_ROOT / f"call_sessions_footprints_{start_date}~{end_date}.zip"
        zip_files(zip_path, json_files)
        log(f"ZIP created: {zip_path}")

        elapsed_sec = int(time.time() - START_TS)
        log_path = LOG_DIR / f"call_sessions_footprints_{start_date}~{end_date}.log"
        write_log_file(log_path)
        log(f"Log saved: {log_path}")

        summary_text = build_summary_text(start_date, end_date, json_files, zip_path, elapsed_sec)

        upload_file_to_slack(zip_path, summary_text)
        log("Slack file upload completed")

        if not KEEP_WEEK_FOLDER:
            shutil.rmtree(week_out_dir, ignore_errors=True)
            log(f"Removed week output folder: {week_out_dir}")

        log("Job finished successfully")

    except Exception as e:
        tb = traceback.format_exc()
        log(f"[ERROR] {type(e).__name__}: {e}")
        log(tb)

        if start_date and end_date:
            fail_text = build_failure_text(start_date, end_date, e)
        else:
            fail_text = "\n".join([
                "❌ call_sessions_footprints 주간 리포트 실패",
                f"- 서버: {HOSTNAME}",
                f"- 에러: {type(e).__name__}: {e}",
                f"- 실행 시각: {now_str()}",
            ])

        try:
            send_slack_webhook(fail_text)
        except Exception:
            pass

        try:
            ensure_dir(LOG_DIR)
            fallback_name = datetime.now().strftime("failed_%Y-%m-%d_%H-%M-%S.log")
            write_log_file(LOG_DIR / fallback_name)
        except Exception:
            pass

        sys.exit(1)


if __name__ == "__main__":
    main()