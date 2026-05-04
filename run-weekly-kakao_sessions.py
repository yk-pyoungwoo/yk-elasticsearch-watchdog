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
JOB_NAME = "kakao_sessions_footprints"

BASE_DIR = Path(os.environ.get("CALL_REPORT_BASE_DIR", str(Path(__file__).resolve().parent)))
EXTRACT_SCRIPT = Path(
    os.environ.get(
        "EXTRACT_SCRIPT",
        str(BASE_DIR / "extract-kakao_sessions-footprints-weekly.py"),
    )
)
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", str(BASE_DIR)))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))

# Slack
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()

# Optional
PYTHON_BIN = os.environ.get("PYTHON_BIN", "").strip() or sys.executable
KEEP_WEEK_FOLDER = os.environ.get("KEEP_WEEK_FOLDER", "1") == "1"

# =========================================================
# Utils
# =========================================================
HOSTNAME = socket.gethostname()
START_TS = time.time()
LOG_LINES = []


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


def safe_tail(text: str, max_len: int = 3000) -> str:
    if not text:
        return "(empty)"
    text = text.strip()
    if len(text) <= max_len:
        return text
    return "...(truncated)...\n" + text[-max_len:]


def compute_previous_week_monday_sunday(today: datetime = None):
    """
    직전 ISO 주의 월요일~일요일(7일). 월요일 스케줄에서 전 주 월~일 데이터를 쓸 때 사용.

    예:
      - 2026-02-03(월) 실행 -> 2026-01-27(월) ~ 2026-02-02(일)
      - 2026-02-05(수) 실행 -> 2026-01-27 ~ 2026-02-02
    """
    if today is None:
        today = datetime.now()

    d = today.date()
    this_monday = d - timedelta(days=d.weekday())
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = prev_monday + timedelta(days=6)

    return prev_monday.strftime("%Y-%m-%d"), prev_sunday.strftime("%Y-%m-%d")


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


def slack_api_json(method: str, url: str, payload: dict = None, timeout: int = 60) -> dict:
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
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set.")
    if not SLACK_CHANNEL_ID:
        raise RuntimeError("SLACK_CHANNEL_ID is not set.")

    file_bytes = zip_path.read_bytes()
    filename = zip_path.name
    length = len(file_bytes)

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


def zip_files(zip_path: Path, files):
    ensure_dir(zip_path.parent)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            zf.write(file_path, arcname=file_path.name)


def build_summary_text(start_date: str, end_date: str, json_files, zip_path: Path, elapsed_sec: int, log_path: Path) -> str:
    lines = [
        f"✅ {JOB_NAME} 주간 리포트 완료",
        f"- 서버: {HOSTNAME}",
        f"- 기간: {start_date}~{end_date}",
        f"- JSON 파일 수: {len(json_files)}",
        f"- ZIP 파일: {zip_path.name}",
        f"- 로그 파일: {log_path.name}",
        f"- 소요시간: {elapsed_sec}s",
        f"- 실행 시각: {now_str()}",
    ]
    return "\n".join(lines)


def build_failure_text(start_date: str, end_date: str, err: Exception, child_info: dict = None, log_path: Path = None) -> str:
    lines = [
        f"❌ {JOB_NAME} 주간 리포트 실패",
        f"- 서버: {HOSTNAME}",
        f"- 기간: {start_date or '-'}~{end_date or '-'}",
        f"- 에러: {type(err).__name__}: {err}",
    ]

    if log_path:
        lines.append(f"- 로그 파일: {log_path}")

    if child_info:
        lines.append(f"- child returncode: {child_info.get('returncode')}")
        lines.append(f"- child cmd: {child_info.get('cmd')}")
        lines.append("- child stderr tail:")
        lines.append(safe_tail(child_info.get("stderr", ""), 1500))
        lines.append("- child stdout tail:")
        lines.append(safe_tail(child_info.get("stdout", ""), 1500))

    lines.append(f"- 실행 시각: {now_str()}")
    return "\n".join(lines)


def run_extract_script(start_date: str, end_date: str, out_dir: Path, child_log_prefix: Path) -> dict:
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
    log(f"Extract cwd: {BASE_DIR}")
    log(f"Python executable for child: {PYTHON_BIN}")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
    )

    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""

    stdout_path = child_log_prefix.with_suffix(".stdout.log")
    stderr_path = child_log_prefix.with_suffix(".stderr.log")
    meta_path = child_log_prefix.with_suffix(".meta.log")

    ensure_dir(stdout_path.parent)
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    meta_path.write_text(
        "\n".join([
            f"job={JOB_NAME}",
            f"time={now_str()}",
            f"cwd={BASE_DIR}",
            f"cmd={' '.join(cmd)}",
            f"returncode={proc.returncode}",
            f"python_bin={PYTHON_BIN}",
            f"extract_script={EXTRACT_SCRIPT}",
        ]) + "\n",
        encoding="utf-8",
    )

    log(f"Child stdout log saved: {stdout_path}")
    log(f"Child stderr log saved: {stderr_path}")
    log(f"Child meta log saved: {meta_path}")

    if stdout_text:
        for line in stdout_text.splitlines():
            log(f"[extract][stdout] {line}")

    if stderr_text:
        for line in stderr_text.splitlines():
            log(f"[extract][stderr] {line}")

    child_info = {
        "cmd": " ".join(cmd),
        "cwd": str(BASE_DIR),
        "returncode": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "meta_path": str(meta_path),
    }

    if proc.returncode != 0:
        raise RuntimeError(
            "Extract script failed "
            f"(exit={proc.returncode}). "
            f"stderr_tail={safe_tail(stderr_text, 1000)}"
        )

    return child_info


def find_expected_json_files(out_dir: Path, start_date: str, end_date: str):
    files = []
    for day in iter_dates(start_date, end_date):
        fp = out_dir / f"kakao_sessions_footprints_{day}.json"
        if not fp.exists():
            raise FileNotFoundError(f"Expected JSON file not found: {fp}")
        files.append(fp)
    return files


def main():
    start_date = None
    end_date = None
    child_info = None
    final_log_path = None

    try:
        ensure_dir(OUTPUT_ROOT)
        ensure_dir(LOG_DIR)

        # 직전 주 월요일~일요일
        start_date, end_date = compute_previous_week_monday_sunday()
        week_name = f"{start_date}~{end_date}"
        week_out_dir = OUTPUT_ROOT / week_name
        ensure_dir(week_out_dir)

        final_log_path = LOG_DIR / f"{JOB_NAME}_{start_date}~{end_date}.log"
        child_log_prefix = LOG_DIR / f"{JOB_NAME}_{start_date}~{end_date}_extract"

        log("Job started")
        log(f"JOB_NAME={JOB_NAME}")
        log(f"BASE_DIR={BASE_DIR}")
        log(f"EXTRACT_SCRIPT={EXTRACT_SCRIPT}")
        log(f"OUTPUT_ROOT={OUTPUT_ROOT}")
        log(f"LOG_DIR={LOG_DIR}")
        log(f"TARGET_WEEK={start_date}~{end_date}")
        log(f"HOSTNAME={HOSTNAME}")
        log(f"sys.executable={sys.executable}")
        log(f"PYTHON_BIN={PYTHON_BIN}")

        if PYTHON_BIN.lower() == "python":
            log("[WARN] PYTHON_BIN is plain 'python'. In Task Scheduler this may resolve to an unexpected interpreter.")

        send_slack_webhook(
            "\n".join([
                f"🚀 {JOB_NAME} 주간 리포트 시작",
                f"- 서버: {HOSTNAME}",
                f"- 기간: {start_date}~{end_date}",
                f"- 실행 시각: {now_str()}",
            ])
        )

        child_info = run_extract_script(start_date, end_date, week_out_dir, child_log_prefix)

        json_files = find_expected_json_files(week_out_dir, start_date, end_date)
        for fp in json_files:
            log(f"Generated JSON: {fp}")

        zip_path = OUTPUT_ROOT / f"{JOB_NAME}_{start_date}~{end_date}.zip"
        zip_files(zip_path, json_files)
        log(f"ZIP created: {zip_path}")

        elapsed_sec = int(time.time() - START_TS)
        log(f"Log will be saved to: {final_log_path}")
        write_log_file(final_log_path)

        upload_file_to_slack(zip_path)
        log("Slack file upload completed")

        summary_text = build_summary_text(start_date, end_date, json_files, zip_path, elapsed_sec, final_log_path)
        send_slack_webhook(summary_text)

        if not KEEP_WEEK_FOLDER:
            shutil.rmtree(week_out_dir, ignore_errors=True)
            log(f"Removed week output folder: {week_out_dir}")

        log("Job finished successfully")
        write_log_file(final_log_path)

    except Exception as e:
        tb = traceback.format_exc()
        log(f"[ERROR] {type(e).__name__}: {e}")
        log(tb)

        if not final_log_path:
            ensure_dir(LOG_DIR)
            suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            final_log_path = LOG_DIR / f"{JOB_NAME}_failed_{suffix}.log"

        try:
            write_log_file(final_log_path)
        except Exception:
            pass

        try:
            fail_text = build_failure_text(
                start_date,
                end_date,
                e,
                child_info=child_info,
                log_path=final_log_path,
            )
            send_slack_webhook(fail_text)
        except Exception:
            pass

        try:
            write_log_file(final_log_path)
        except Exception:
            pass

        sys.exit(1)


if __name__ == "__main__":
    main()