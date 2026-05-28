#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Elasticsearch 일별 앱 인덱스({app}-YYYY-MM-DD) 문서 수를 조회해 JSON/CSV로 저장합니다.

예: assault-2026-05-08, brand-2026-05-09, ...
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import ssl
import sys
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent

ES_URL = os.environ.get("ES_URL", "http://localhost:19200").rstrip("/")
ES_USER = os.environ.get("ES_USER", "")
ES_PASS = os.environ.get("ES_PASS", "")
INSECURE_TLS = os.environ.get("INSECURE_TLS", "1") == "1"
MAX_RETRY = int(os.environ.get("MAX_RETRY", "6"))

DEFAULT_APPS = [
    "assault",
    "brand",
    "civil",
    "crime",
    "divorce",
    "drug",
    "estate",
    "inherit",
    "school",
    "traffic",
]

def iter_dotenv(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        yield key, val


def load_dotenv_file(path: Path, override: bool = False) -> None:
    if not path.is_file():
        return
    for key, val in iter_dotenv(path):
        if override or key not in os.environ:
            os.environ[key] = val


def refresh_es_config_from_env() -> None:
    global ES_URL, ES_USER, ES_PASS, INSECURE_TLS
    ES_URL = os.environ.get("ES_URL", ES_URL).rstrip("/")
    ES_USER = os.environ.get("ES_USER", ES_USER)
    ES_PASS = os.environ.get("ES_PASS", ES_PASS)
    INSECURE_TLS = os.environ.get("INSECURE_TLS", "1" if INSECURE_TLS else "0") == "1"


def validate_ymd(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str} (expected YYYY-MM-DD)")


def iter_dates(start_ymd: str, end_ymd: str):
    s = datetime.strptime(validate_ymd(start_ymd), "%Y-%m-%d").date()
    e = datetime.strptime(validate_ymd(end_ymd), "%Y-%m-%d").date()
    if s > e:
        raise ValueError(f"start-date must be <= end-date: {start_ymd} > {end_ymd}")
    cur = s
    while cur <= e:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def build_index_name(app: str, day: str) -> str:
    return f"{app}-{day}"


def _ssl_context():
    if not ES_URL.lower().startswith("https://"):
        return None
    if INSECURE_TLS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _auth_header() -> dict[str, str]:
    if ES_USER and ES_PASS:
        token = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {}


def http_request(method: str, path: str, query: dict | None = None, timeout: int = 120) -> tuple[int, str]:
    url = ES_URL + path
    if query:
        url += "?" + urlencode(query)

    headers = _auth_header()
    req = Request(url, method=method, headers=headers)

    last_err: Exception | None = None
    for i in range(MAX_RETRY):
        try:
            with urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            last_err = e
            if e.code == 404:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                return 404, body
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = "(no error body)"
            print(f"\n[HTTPError] status={e.code} url={url}\n{err_body[:2000]}\n", file=sys.stderr)
        except (URLError, TimeoutError) as e:
            last_err = e
            print(f"\n[URLError] url={url} err={e}\n", file=sys.stderr)

        sleep_s = min(2 ** i, 20) + random.random()
        time.sleep(sleep_s)

    raise RuntimeError(f"HTTP request failed after retries: {last_err}") from last_err


def count_index_docs(index: str) -> tuple[int | None, bool]:
    """
    Returns (doc_count, exists).
    exists=False when index is missing (404).
    """
    status, raw = http_request("GET", f"/{index}/_count")
    if status == 404:
        return None, False
    if status != 200:
        raise RuntimeError(f"_count failed for {index}: HTTP {status} body={raw[:500]}")

    data = json.loads(raw)
    count = data.get("count")
    if not isinstance(count, int):
        raise RuntimeError(f"Unexpected _count response for {index}: {raw[:500]}")
    return count, True


def resolve_target_dates(args) -> tuple[str, str, list[str]]:
    if args.dates:
        if args.date or args.start_date or args.end_date:
            raise ValueError("Use either positional DATE [END_DATE] or --date / --start-date --end-date, not both")
        if len(args.dates) == 1:
            day = validate_ymd(args.dates[0])
            return day, day, [day]
        if len(args.dates) == 2:
            start = validate_ymd(args.dates[0])
            end = validate_ymd(args.dates[1])
            dates = list(iter_dates(start, end))
            return start, end, dates
        raise ValueError("Expected 1 or 2 positional dates: YYYY-MM-DD [END_DATE]")

    if args.date:
        if args.start_date or args.end_date:
            raise ValueError("Use either --date or --start-date --end-date, not both")
        day = validate_ymd(args.date)
        return day, day, [day]

    if args.start_date and args.end_date:
        start = validate_ymd(args.start_date)
        end = validate_ymd(args.end_date)
        dates = list(iter_dates(start, end))
        return start, end, dates

    if args.start_date or args.end_date:
        raise ValueError("Both --start-date and --end-date are required for a date range")

    raise ValueError(
        "Date is required. Examples:\n"
        "  count-daily-app-index-docs.py 2026-05-21\n"
        "  count-daily-app-index-docs.py 2026-05-08 2026-05-21\n"
        "  count-daily-app-index-docs.py --date 2026-05-21\n"
        "  count-daily-app-index-docs.py --start-date 2026-05-08 --end-date 2026-05-21"
    )


def parse_args():
    p = argparse.ArgumentParser(
        description="일별 앱 인덱스({app}-YYYY-MM-DD) 문서 수를 조회해 JSON/CSV로 저장합니다.",
        epilog=(
            "날짜 지정 예:\n"
            "  %(prog)s 2026-05-21\n"
            "  %(prog)s 2026-05-08 2026-05-21\n"
            "  %(prog)s --date 2026-05-21\n"
            "  %(prog)s --start-date 2026-05-08 --end-date 2026-05-21"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "dates",
        nargs="*",
        metavar="DATE",
        help="YYYY-MM-DD 또는 YYYY-MM-DD END_DATE (위치 인자)",
    )
    p.add_argument("--date", help="단일 날짜 (YYYY-MM-DD)")
    p.add_argument("--start-date", help="시작일 (YYYY-MM-DD, --end-date 와 함께)")
    p.add_argument("--end-date", help="종료일 (YYYY-MM-DD, --start-date 와 함께)")
    p.add_argument(
        "--apps",
        default=",".join(DEFAULT_APPS),
        help=f"쉼표 구분 앱 이름 (기본: {','.join(DEFAULT_APPS)})",
    )
    p.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "exports" / "index_doc_counts"),
        help="결과 저장 디렉터리",
    )
    p.add_argument(
        "--dotenv",
        default=str(REPO_ROOT / ".env"),
        help="ES_URL 등을 읽을 .env 경로 (없으면 무시)",
    )
    p.add_argument(
        "--no-dotenv",
        action="store_true",
        help=".env 자동 로드 비활성화",
    )
    return p.parse_args()


def write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["app", "date", "index", "doc_count", "exists"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in fieldnames})


def main() -> int:
    args = parse_args()

    if not args.no_dotenv:
        load_dotenv_file(Path(args.dotenv))
    refresh_es_config_from_env()

    apps = [x.strip() for x in args.apps.split(",") if x.strip()]
    if not apps:
        print("[ERROR] --apps is empty", file=sys.stderr)
        return 1

    try:
        start_date, end_date, dates = resolve_target_dates(args)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = f"index_doc_counts_{start_date}~{end_date}"
    json_path = out_dir / f"{stem}.json"
    csv_path = out_dir / f"{stem}.csv"

    print(f"[INFO] ES_URL={ES_URL}")
    print(f"[INFO] APPS={', '.join(apps)}")
    print(f"[INFO] DATES={start_date}~{end_date} ({len(dates)} days)")
    print(f"[INFO] OUT_DIR={out_dir}")

    rows: list[dict] = []
    missing = 0

    daily_totals: list[dict] = []

    for day in dates:
        day_total = 0
        for app in apps:
            index = build_index_name(app, day)
            doc_count, exists = count_index_docs(index)
            if not exists:
                missing += 1
            elif doc_count is not None:
                day_total += doc_count
            row = {
                "app": app,
                "date": day,
                "index": index,
                "doc_count": doc_count,
                "exists": exists,
            }
            rows.append(row)
            status = str(doc_count) if exists else "MISSING"
            print(f"[{day}] {index}: {status}")

        daily_totals.append({"date": day, "total": day_total})
        print(f"total-{day}: {day_total}")

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "es_url": ES_URL,
        "start_date": start_date,
        "end_date": end_date,
        "apps": apps,
        "row_count": len(rows),
        "missing_index_count": missing,
        "daily_totals": daily_totals,
        "rows": rows,
    }

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, rows)

    print(f"[DONE] JSON -> {json_path}")
    print(f"[DONE] CSV  -> {csv_path}")
    print(f"[DONE] rows={len(rows)}, missing_indices={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
