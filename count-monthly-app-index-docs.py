#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Elasticsearch 월별 앱 인덱스({app}-YYYY-MM-*) 문서 수를 조회해 JSON/CSV로 저장합니다.

일별 인덱스(assault-2026-05-08 등)가 날짜마다 존재할 때, 해당 월의 모든 일별 인덱스를
와일드카드로 묶어 합산합니다.
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
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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

DEFAULT_YEAR = 2026
DEFAULT_MONTHS = [1, 2, 3, 4, 5]


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


def validate_year(year_str: str) -> int:
    try:
        year = int(year_str)
    except ValueError:
        raise ValueError(f"Invalid year: {year_str} (expected YYYY)")
    if year < 1970 or year > 9999:
        raise ValueError(f"Invalid year: {year}")
    return year


def validate_month(month: int) -> int:
    if month < 1 or month > 12:
        raise ValueError(f"Invalid month: {month} (expected 1-12)")
    return month


def parse_months_csv(raw: str) -> list[int]:
    months: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        months.append(validate_month(int(part)))
    if not months:
        raise ValueError("months list is empty")
    return sorted(set(months))


def month_label(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def build_index_pattern(app: str, year: int, month: int) -> str:
    return f"{app}-{year}-{month:02d}-*"


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


def count_index_pattern_docs(index_pattern: str) -> tuple[int, bool]:
    """
    Returns (doc_count, exists).
    exists=False when no index matches the pattern (404 or zero shards).
    """
    encoded = quote(index_pattern, safe="*,:")
    status, raw = http_request("GET", f"/{encoded}/_count")
    if status == 404:
        return 0, False
    if status != 200:
        raise RuntimeError(f"_count failed for {index_pattern}: HTTP {status} body={raw[:500]}")

    data = json.loads(raw)
    count = data.get("count")
    if not isinstance(count, int):
        raise RuntimeError(f"Unexpected _count response for {index_pattern}: {raw[:500]}")

    shards = data.get("_shards") or {}
    total_shards = shards.get("total", 0)
    exists = total_shards > 0
    return count, exists


def resolve_target_months(args) -> tuple[int, list[int]]:
    if args.month_args:
        if args.year is not None or args.months is not None:
            raise ValueError("Use either positional YEAR [MONTH ...] or --year / --months, not both")
        year = validate_year(args.month_args[0])
        if len(args.month_args) == 1:
            return year, list(DEFAULT_MONTHS)
        months = [validate_month(int(x)) for x in args.month_args[1:]]
        return year, sorted(set(months))

    if args.year is None:
        raise ValueError(
            "Year is required. Examples:\n"
            "  count-monthly-app-index-docs.py 2026\n"
            "  count-monthly-app-index-docs.py 2026 1 2 3 4 5\n"
            "  count-monthly-app-index-docs.py --year 2026 --months 1,2,3,4,5"
        )

    year = validate_year(args.year)
    if args.months is not None:
        months = parse_months_csv(args.months)
    else:
        months = list(DEFAULT_MONTHS)
    return year, months


def parse_args():
    p = argparse.ArgumentParser(
        description="월별 앱 인덱스({app}-YYYY-MM-*) 문서 수를 조회해 JSON/CSV로 저장합니다.",
        epilog=(
            "사용 예:\n"
            "  %(prog)s 2026\n"
            "  %(prog)s 2026 1 2 3 4 5\n"
            "  %(prog)s --year 2026 --months 1,2,3,4,5"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "month_args",
        nargs="*",
        metavar="ARG",
        help="YEAR 또는 YEAR MONTH [MONTH ...] (위치 인자)",
    )
    p.add_argument("--year", help="연도 (YYYY)")
    p.add_argument("--months", help="쉼표 구분 월 (1-12). 기본: 1,2,3,4,5")
    p.add_argument(
        "--apps",
        default=",".join(DEFAULT_APPS),
        help=f"쉼표 구분 앱 이름 (기본: {','.join(DEFAULT_APPS)})",
    )
    p.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "exports" / "index_doc_counts_monthly"),
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
    fieldnames = ["app", "year", "month", "month_label", "index_pattern", "doc_count", "exists"]
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
        year, months = resolve_target_months(args)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    month_labels = [month_label(year, m) for m in months]
    month_tag = "-".join(f"{m:02d}" for m in months)
    stem = f"index_doc_counts_monthly_{year}_{month_tag}"
    json_path = out_dir / f"{stem}.json"
    csv_path = out_dir / f"{stem}.csv"

    print(f"[INFO] ES_URL={ES_URL}")
    print(f"[INFO] YEAR={year}")
    print(f"[INFO] MONTHS={', '.join(str(m) for m in months)}")
    print(f"[INFO] APPS={', '.join(apps)}")
    print(f"[INFO] OUT_DIR={out_dir}")

    rows: list[dict] = []
    missing = 0
    monthly_totals: list[dict] = []

    for month in months:
        label = month_label(year, month)
        month_total = 0
        for app in apps:
            pattern = build_index_pattern(app, year, month)
            doc_count, exists = count_index_pattern_docs(pattern)
            if not exists:
                missing += 1
            month_total += doc_count
            row = {
                "app": app,
                "year": year,
                "month": month,
                "month_label": label,
                "index_pattern": pattern,
                "doc_count": doc_count,
                "exists": exists,
            }
            rows.append(row)
            status = str(doc_count) if exists else "0 (no index)"
            print(f"[{label}] {pattern}: {status}")

        monthly_totals.append({"month_label": label, "year": year, "month": month, "total": month_total})
        print(f"total-{label}: {month_total}")

    grand_total = sum(item["total"] for item in monthly_totals)
    print(f"grand-total-{year}: {grand_total}")

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "es_url": ES_URL,
        "year": year,
        "months": months,
        "month_labels": month_labels,
        "apps": apps,
        "row_count": len(rows),
        "missing_pattern_count": missing,
        "monthly_totals": monthly_totals,
        "grand_total": grand_total,
        "rows": rows,
    }

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, rows)

    print(f"[DONE] JSON -> {json_path}")
    print(f"[DONE] CSV  -> {csv_path}")
    print(f"[DONE] rows={len(rows)}, missing_patterns={missing}, grand_total={grand_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
