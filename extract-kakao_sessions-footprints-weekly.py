#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import ssl
import time
import base64
import random
import argparse
from urllib.parse import urlparse, parse_qs, unquote, urlencode, urlunparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ES_URL = os.environ.get("ES_URL", "http://localhost:19200").rstrip("/")
ES_USER = os.environ.get("ES_USER", "")
ES_PASS = os.environ.get("ES_PASS", "")
INSECURE_TLS = os.environ.get("INSECURE_TLS", "1") == "1"

SITES = ["brand", "crime", "civil", "divorce", "assault", "drug", "inherit", "traffic", "school"]

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5000"))
SCROLL_TTL = os.environ.get("SCROLL_TTL", "10m")
MAX_RETRY = int(os.environ.get("MAX_RETRY", "6"))
OUT_DIR = os.environ.get("OUT_DIR", ".")

KST = timezone(timedelta(hours=9))

# footprints에 남길 필드
FOOTPRINT_FIELDS = [
    "@timestamp",
    "url",
    "type",
    "event",
]

# kakao seed / session 레벨 UTM 보강용
KAKAO_SEED_SOURCE_FIELDS = [
    "session_uuid", "@timestamp", "url", "referrer", "utm",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "type", "event", "device"
]

SESSION_FOOTPRINT_SOURCE_FIELDS = [
    "@timestamp",
    "url",
    "referrer",
    "type",
    "event",
    "device",
    "session_uuid",
    "utm",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract kakao sessions and their footprints from Elasticsearch, one JSON file per day (KST-based)."
    )
    parser.add_argument(
        "--date",
        help="Single date to process. Format: YYYY-MM-DD (KST 기준)",
    )
    parser.add_argument(
        "--start-date",
        help="Start date (inclusive). Format: YYYY-MM-DD (KST 기준)",
    )
    parser.add_argument(
        "--end-date",
        help="End date (inclusive). Format: YYYY-MM-DD (KST 기준)",
    )
    parser.add_argument(
        "--out-dir",
        default=OUT_DIR,
        help=f"Output directory. Default: {OUT_DIR}",
    )
    return parser.parse_args()


def validate_ymd(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str} (expected YYYY-MM-DD)")


def daterange_inclusive(start_ymd: str, end_ymd: str):
    s = datetime.strptime(start_ymd, "%Y-%m-%d")
    e = datetime.strptime(end_ymd, "%Y-%m-%d")

    if s > e:
        raise ValueError(f"start-date must be <= end-date: {start_ymd} > {end_ymd}")

    cur = s
    while cur <= e:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def resolve_target_dates(args):
    if args.date:
        return [validate_ymd(args.date)]

    if args.start_date and args.end_date:
        start_date = validate_ymd(args.start_date)
        end_date = validate_ymd(args.end_date)
        return list(daterange_inclusive(start_date, end_date))

    raise ValueError("Use either --date YYYY-MM-DD or --start-date YYYY-MM-DD --end-date YYYY-MM-DD")


def parse_kst_day_start(day_yyyy_mm_dd: str) -> datetime:
    return datetime.strptime(day_yyyy_mm_dd, "%Y-%m-%d").replace(tzinfo=KST)


def to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_day_range_utc(day_yyyy_mm_dd: str):
    """
    입력 날짜는 KST 하루로 해석하고,
    ES 조회 range는 정확한 UTC 범위로 변환해서 사용한다.

    예:
      2026-03-23(KST)
      = 2026-03-22T15:00:00.000Z ~ 2026-03-23T15:00:00.000Z
    """
    start_kst = parse_kst_day_start(day_yyyy_mm_dd)
    end_kst = start_kst + timedelta(days=1)
    gte = iso_z(to_utc(start_kst))
    lt = iso_z(to_utc(end_kst))
    return gte, lt


def build_index_patterns_for_day(day_yyyy_mm_dd: str):
    """
    KST 하루가 걸치는 UTC 월 인덱스를 모두 포함한다.

    기존:
      site-YYYY-MM-* 형태만 조회해서
      월별 통합 인덱스(site-YYYY-MM)를 못 찾음

    수정:
      site-YYYY-MM* 로 조회해서 아래 둘 다 커버
      - 일별 인덱스: brand-2026-03-01
      - 월별 인덱스: brand-2026-03
    """
    start_kst = parse_kst_day_start(day_yyyy_mm_dd)
    end_kst = start_kst + timedelta(days=1)

    start_utc = to_utc(start_kst)
    end_utc = to_utc(end_kst)

    months = set()
    cur = datetime(start_utc.year, start_utc.month, 1, tzinfo=timezone.utc)
    end_month = datetime(end_utc.year, end_utc.month, 1, tzinfo=timezone.utc)

    while cur <= end_month:
        months.add(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            cur = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)

    patterns = []
    for ym in sorted(months):
        for site in SITES:
            patterns.append(f"{site}-{ym}*")
    return patterns


def build_output_path(out_dir: str, day: str):
    return os.path.join(out_dir, f"kakao_sessions_footprints_{day}.json")


def _ssl_context():
    if not ES_URL.lower().startswith("https://"):
        return None
    if INSECURE_TLS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _auth_header():
    if ES_USER and ES_PASS:
        token = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {}


def http_json(method: str, path: str, body=None, query=None, timeout=120):
    url = ES_URL + path
    if query:
        url += "?" + urlencode(query)

    headers = {"Content-Type": "application/json"}
    headers.update(_auth_header())
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(url, data=data, method=method, headers=headers)

    last_err = None
    for i in range(MAX_RETRY):
        try:
            with urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except HTTPError as e:
            last_err = e
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = "(no error body)"
            print(f"\n[HTTPError] status={e.code} url={url}\n{err_body[:2000]}\n", file=sys.stderr)
        except (URLError, TimeoutError) as e:
            last_err = e
            print(f"\n[URLError] url={url} err={e}\n", file=sys.stderr)

        sleep_s = min(2 ** i, 20) + random.random()
        print(f"[WARN] retry {i+1}/{MAX_RETRY} sleep={sleep_s:.1f}s", file=sys.stderr)
        time.sleep(sleep_s)

    raise last_err


def decode_value(value):
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        return value
    try:
        prev = value
        cur = unquote(prev)
        for _ in range(2):
            if cur == prev:
                break
            prev = cur
            cur = unquote(prev)
        return cur
    except Exception:
        return value


def decode_url(url: str):
    if not url:
        return url
    try:
        p = urlparse(url)
        decoded_path = decode_value(p.path)
        decoded_query = decode_value(p.query)
        decoded_fragment = decode_value(p.fragment)

        rebuilt = urlunparse((
            p.scheme,
            p.netloc,
            decoded_path,
            p.params,
            decoded_query,
            decoded_fragment,
        ))
        return rebuilt
    except Exception:
        return decode_value(url)


def parse_datetime_string(value: str):
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def normalize_timestamp_to_kst_string(value):
    """
    출력용 timestamp를 모두 KST 문자열로 통일한다.
    - Z / UTC / 기타 timezone -> KST 변환
    - naive datetime -> 이미 KST라고 간주
    - 파싱 불가 -> 원본 유지
    """
    if value in (None, ""):
        return value

    dt = parse_datetime_string(value)
    if dt is None:
        return value

    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

    return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def parse_utm_from_url(url: str):
    if not url:
        return {}
    try:
        qs = parse_qs(urlparse(url).query)

        def one(k):
            v = qs.get(k)
            if not v:
                return None
            return decode_value(v[0]) if isinstance(v[0], str) else v[0]

        out = {
            "utm_source": one("utm_source"),
            "utm_medium": one("utm_medium"),
            "utm_campaign": one("utm_campaign"),
            "utm_term": one("utm_term"),
            "utm_content": one("utm_content"),
        }
        return {k: v for k, v in out.items() if v not in (None, "")}
    except Exception:
        return {}


def merge_utm(base: dict, extra: dict):
    if base is None:
        base = {}
    for k, v in (extra or {}).items():
        if v in (None, ""):
            continue
        if k not in base or base.get(k) in (None, ""):
            base[k] = v
    return base


def normalize_utm_dict(utm):
    if not isinstance(utm, dict):
        return {}

    out = {}
    for k, v in utm.items():
        if v in (None, ""):
            continue
        out[k] = decode_value(v)
    return out


def slim_source(src: dict):
    out = {}
    for k in FOOTPRINT_FIELDS:
        if k in src and src[k] not in (None, ""):
            out[k] = src[k]

    if "@timestamp" in out:
        out["@timestamp"] = normalize_timestamp_to_kst_string(out["@timestamp"])

    if "url" in out:
        out["url"] = decode_url(out["url"])

    if "referrer" in out:
        out["referrer"] = decode_url(out["referrer"])

    out.pop("title", None)
    out.pop("version", None)
    out.pop("session_uuid", None)
    out.pop("utm", None)
    out.pop("utm_source", None)
    out.pop("utm_medium", None)
    out.pop("utm_campaign", None)
    out.pop("utm_term", None)
    out.pop("utm_content", None)

    return out


def scroll_search(indices: str, query_body: dict):
    first = http_json(
        "POST",
        f"/{indices}/_search",
        body=query_body,
        query={"scroll": SCROLL_TTL},
        timeout=180,
    )
    scroll_id = first.get("_scroll_id")
    hits = (first.get("hits") or {}).get("hits") or []
    yield from hits

    while hits:
        nxt = http_json(
            "POST",
            "/_search/scroll",
            body={"scroll": SCROLL_TTL, "scroll_id": scroll_id},
            timeout=180,
        )
        scroll_id = nxt.get("_scroll_id")
        hits = (nxt.get("hits") or {}).get("hits") or []
        yield from hits

    if scroll_id:
        try:
            http_json("DELETE", "/_search/scroll", body={"scroll_id": scroll_id})
        except Exception:
            pass


def extract_one_day(day: str, out_dir: str):
    time_gte, time_lt = iso_day_range_utc(day)
    index_patterns = build_index_patterns_for_day(day)
    indices = ",".join(index_patterns)
    out_json = build_output_path(out_dir, day)

    start_kst = parse_kst_day_start(day)
    end_kst = start_kst + timedelta(days=1)

    print(f"\n[DAY {day}] START")
    print(f"[DAY {day}] ES_URL={ES_URL}")
    print(f"[DAY {day}] INDICES={indices}")
    print(f"[DAY {day}] RANGE_KST={start_kst.isoformat()} ~ {end_kst.isoformat()}")
    print(f"[DAY {day}] RANGE_UTC={time_gte} ~ {time_lt}")
    print(f"[DAY {day}] BATCH_SIZE={BATCH_SIZE}, SCROLL_TTL={SCROLL_TTL}")
    print(f"[DAY {day}] OUT_JSON={out_json}")

    # 1) kakao 세션 수집
    kakao_query = {
        "size": BATCH_SIZE,
        "_source": KAKAO_SEED_SOURCE_FIELDS,
        "sort": ["_doc"],
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": time_gte, "lt": time_lt}}},
                    {"term": {"type.keyword": "kakao"}}
                ]
            }
        }
    }

    kakao_session_ids = set()
    kakao_seed = {}

    processed_kakao = 0
    for h in scroll_search(indices, kakao_query):
        src = h.get("_source") or {}
        sid = src.get("session_uuid")
        if not sid:
            continue

        kakao_session_ids.add(sid)
        processed_kakao += 1

        if sid not in kakao_seed:
            seed_utm = normalize_utm_dict(src.get("utm"))
            seed_utm = merge_utm(seed_utm, {
                "utm_source": decode_value(src.get("utm_source")),
                "utm_medium": decode_value(src.get("utm_medium")),
                "utm_campaign": decode_value(src.get("utm_campaign")),
                "utm_term": decode_value(src.get("utm_term")),
                "utm_content": decode_value(src.get("utm_content")),
            })
            seed_utm = merge_utm(seed_utm, parse_utm_from_url(src.get("url")))

            kakao_seed[sid] = {
                "session_uuid": sid,
                "utm": seed_utm,
                "referrer": decode_url(src.get("referrer")),
                "device": src.get("device"),
                "@timestamp": normalize_timestamp_to_kst_string(src.get("@timestamp")),
            }

        if processed_kakao % 20000 == 0:
            print(f"[DAY {day}] kakao_docs={processed_kakao}, kakao_sessions={len(kakao_session_ids)}")

    print(f"[DAY {day}] kakao_docs={processed_kakao}, unique_kakao_sessions={len(kakao_session_ids)}")

    if not kakao_session_ids:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        print(f"[DAY {day}] DONE no kakao sessions -> {out_json}")
        return

    # 2) 해당 session_uuid들의 전체 footprints 수집(같은 KST 하루 범위)
    session_ids_list = list(kakao_session_ids)

    def chunks(lst, n=2000):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    result = defaultdict(lambda: {
        "session_uuid": None,
        "utm": {},
        "referrer": None,
        "device": None,
        "footprints": []
    })

    total_docs = 0

    for batch_idx, batch in enumerate(chunks(session_ids_list, 2000), start=1):
        fp_query = {
            "size": BATCH_SIZE,
            "_source": SESSION_FOOTPRINT_SOURCE_FIELDS,
            "sort": ["_doc"],
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": time_gte, "lt": time_lt}}},
                        {"terms": {"session_uuid.keyword": batch}}
                    ]
                }
            }
        }

        for h in scroll_search(indices, fp_query):
            src = h.get("_source") or {}
            sid = src.get("session_uuid")
            if not sid:
                continue

            entry = result[sid]
            entry["session_uuid"] = sid

            seed = kakao_seed.get(sid)
            if seed:
                if (not entry["referrer"]) and seed.get("referrer"):
                    entry["referrer"] = seed.get("referrer")
                if (not entry["device"]) and seed.get("device"):
                    entry["device"] = seed.get("device")
                entry["utm"] = merge_utm(entry["utm"], seed.get("utm") or {})

            fp_utm = normalize_utm_dict(src.get("utm"))
            fp_utm = merge_utm(fp_utm, {
                "utm_source": decode_value(src.get("utm_source")),
                "utm_medium": decode_value(src.get("utm_medium")),
                "utm_campaign": decode_value(src.get("utm_campaign")),
                "utm_term": decode_value(src.get("utm_term")),
                "utm_content": decode_value(src.get("utm_content")),
            })
            fp_utm = merge_utm(fp_utm, parse_utm_from_url(src.get("url")))
            entry["utm"] = merge_utm(entry["utm"], fp_utm)

            slim = slim_source(src)
            entry["footprints"].append(slim)

            total_docs += 1
            if total_docs % 200000 == 0:
                print(f"[DAY {day}] processed_docs={total_docs}, sessions={len(result)}")

        print(f"[DAY {day}] batch={batch_idx}, processed_docs={total_docs}, sessions={len(result)}")

    out = []
    for sid, entry in result.items():
        if entry.get("utm") == {}:
            entry["utm"] = None

        if entry.get("referrer"):
            entry["referrer"] = decode_url(entry["referrer"])

        entry["footprints"].sort(key=lambda x: x.get("@timestamp") or "")
        entry["footprints_count"] = len(entry["footprints"])
        out.append(entry)

    out.sort(key=lambda x: x.get("session_uuid") or "")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[DAY {day}] DONE saved -> {out_json}")
    print(f"[DAY {day}] DONE kakao_sessions={len(out)}, footprints_docs={total_docs}")


def main():
    try:
        args = parse_args()
        target_dates = resolve_target_dates(args)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    print(f"[INFO] OUT_DIR={args.out_dir}")
    print(f"[INFO] DATES={', '.join(target_dates)}")

    for day in target_dates:
        extract_one_day(day, args.out_dir)


if __name__ == "__main__":
    main()