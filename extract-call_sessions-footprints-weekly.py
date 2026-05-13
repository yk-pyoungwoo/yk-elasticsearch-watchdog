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

TARGET_TYPE = "call"


def _parse_local_uuid_source_keys() -> list[str]:
    """쉼표 구분. 점(.)으로 중첩 경로 가능. 예: local_uuid,localUuid,context.local_uuid"""
    raw = os.environ.get("ES_LOCAL_UUID_SOURCE_KEYS", "local_uuid,localUuid")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _extra_source_roots_for_dotted_keys(keys: list[str]) -> list[str]:
    roots: list[str] = []
    for k in keys:
        if "." not in k:
            continue
        root = k.split(".", 1)[0]
        if root and root not in roots:
            roots.append(root)
    return roots


def _merge_source_field_list(base: list[str], extra_roots: list[str]) -> list[str]:
    out = list(base)
    for r in extra_roots:
        if r not in out:
            out.append(r)
    return out


LOCAL_UUID_SOURCE_KEYS = _parse_local_uuid_source_keys()
_EXTRA_SOURCE_ROOTS_LOCAL_UUID = _extra_source_roots_for_dotted_keys(LOCAL_UUID_SOURCE_KEYS)


def _source_value_by_path(src: dict, path: str):
    cur: object = src
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def pick_local_uuid(src: dict):
    for path in LOCAL_UUID_SOURCE_KEYS:
        v = _source_value_by_path(src, path)
        if v in (None, ""):
            continue
        return v if isinstance(v, str) else str(v)
    return None


# footprints에 남길 필드 (referrer는 이벤트별)
FOOTPRINT_FIELDS = [
    "@timestamp",
    "url",
    "referrer",
    "type",
    "event",
]

# call seed / session 레벨 UTM 보강용
_CALL_SEED_SOURCE_FIELDS_BASE = [
    "local_uuid",
    "session_uuid", "@timestamp", "url", "referrer", "utm",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "type", "event", "device", "user_agent",
]

_SESSION_FOOTPRINT_SOURCE_FIELDS_BASE = [
    "@timestamp",
    "url",
    "referrer",
    "type",
    "event",
    "device",
    "user_agent",
    "local_uuid",
    "session_uuid",
    "utm",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
]

CALL_SEED_SOURCE_FIELDS = _merge_source_field_list(
    _CALL_SEED_SOURCE_FIELDS_BASE,
    _EXTRA_SOURCE_ROOTS_LOCAL_UUID,
)
SESSION_FOOTPRINT_SOURCE_FIELDS = _merge_source_field_list(
    _SESSION_FOOTPRINT_SOURCE_FIELDS_BASE,
    _EXTRA_SOURCE_ROOTS_LOCAL_UUID,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract call sessions and their footprints from Elasticsearch, one JSON file per day (KST-based)."
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


def iso_with_tz(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def iso_day_range_kst(day_yyyy_mm_dd: str):
    start_kst = parse_kst_day_start(day_yyyy_mm_dd)
    end_kst = start_kst + timedelta(days=1)

    gte = iso_with_tz(start_kst)
    lt = iso_with_tz(end_kst)
    return gte, lt


def build_index_patterns_for_day(day_yyyy_mm_dd: str):
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
            patterns.append(f"{site}-{ym}-*")
    return patterns


def build_output_path(out_dir: str, day: str):
    return os.path.join(out_dir, f"call_sessions_footprints_{day}.json")


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


def device_from_user_agent(user_agent):
    """user_agent 문자열에서 mobile / tablet / desktop 추론."""
    if user_agent in (None, ""):
        return None
    if not isinstance(user_agent, str):
        user_agent = str(user_agent)
    u = user_agent.strip().lower()
    if not u:
        return None
    if "ipad" in u or "tablet" in u or "playbook" in u or "kindle" in u:
        return "tablet"
    if "iphone" in u or "ipod" in u:
        return "mobile"
    if "android" in u:
        return "mobile" if "mobile" in u else "tablet"
    if "windows phone" in u or "mobile" in u or "webos" in u:
        return "mobile"
    return "desktop"


def resolve_device_from_source(src: dict):
    """ES device 필드가 있으면 우선, 없으면 user_agent 파싱."""
    d = src.get("device")
    if d not in (None, ""):
        return d if isinstance(d, str) else str(d)
    return device_from_user_agent(src.get("user_agent"))


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
    if value in (None, ""):
        return value

    dt = parse_datetime_string(value)
    if dt is None:
        return value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
        return dt.isoformat(timespec="milliseconds")

    return dt.astimezone(KST).isoformat(timespec="milliseconds")


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


def merged_utm_from_single_source(src: dict) -> dict:
    """한 문서의 utm 객체·utm_* 필드·URL 쿼리 utm_* 만 합친 값(세션 병합용 후보 1건)."""
    fp_utm = normalize_utm_dict(src.get("utm"))
    fp_utm = merge_utm(
        fp_utm,
        {
            "utm_source": decode_value(src.get("utm_source")),
            "utm_medium": decode_value(src.get("utm_medium")),
            "utm_campaign": decode_value(src.get("utm_campaign")),
            "utm_term": decode_value(src.get("utm_term")),
            "utm_content": decode_value(src.get("utm_content")),
        },
    )
    fp_utm = merge_utm(fp_utm, parse_utm_from_url(src.get("url")))
    return fp_utm


def utm_sort_key(ts) -> datetime:
    """@timestamp 원문 기준 정렬 (세션 UTM 후보 중 시간순 첫 건 선택)."""
    if ts in (None, ""):
        return datetime.max.replace(tzinfo=timezone.utc)
    dt = parse_datetime_string(ts) if isinstance(ts, str) else None
    if dt is None:
        return datetime.max.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_session_utm_from_earliest_footprint_event(candidates: list) -> object:
    """footprints에 해당하는 이벤트 중 @timestamp 가 가장 이른 문서의 UTM 만 세션 utm 으로 사용."""
    pairs = [(ts, d) for ts, d in candidates if isinstance(d, dict)]
    if not pairs:
        return None
    pairs.sort(key=lambda x: utm_sort_key(x[0]))
    first = pairs[0][1]
    if not first:
        return None
    return first


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
    out.pop("local_uuid", None)

    if "referrer" in FOOTPRINT_FIELDS and "referrer" not in out:
        out["referrer"] = None

    return out


def build_output_entry(entry: dict) -> dict:
    """JSON 출력 시 local_uuid가 session_uuid 앞에 오도록 순서 고정."""
    return {
        "local_uuid": entry.get("local_uuid"),
        "session_uuid": entry.get("session_uuid"),
        "utm": entry.get("utm"),
        "referrer": entry.get("referrer"),
        "device": entry.get("device"),
        "footprints": entry.get("footprints"),
        "footprints_count": entry.get("footprints_count"),
    }


def scroll_search(indices: str, query_body: dict):
    scroll_id = None
    try:
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
    finally:
        if scroll_id:
            try:
                http_json("DELETE", "/_search/scroll", body={"scroll_id": scroll_id})
            except Exception:
                pass


def chunked(lst, size=2000):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def extract_one_day(day: str, out_dir: str):
    time_gte, time_lt = iso_day_range_kst(day)
    index_patterns = build_index_patterns_for_day(day)
    indices = ",".join(index_patterns)
    out_json = build_output_path(out_dir, day)

    start_kst = parse_kst_day_start(day)
    end_kst = start_kst + timedelta(days=1)

    print(f"\n[DAY {day}] START")
    print(f"[DAY {day}] TARGET_TYPE={TARGET_TYPE}")
    print(f"[DAY {day}] ES_URL={ES_URL}")
    print(f"[DAY {day}] INDICES={indices}")
    print(f"[DAY {day}] RANGE_KST={time_gte} ~ {time_lt}")
    print(f"[DAY {day}] RANGE_UTC={to_utc(start_kst).isoformat(timespec='milliseconds')} ~ {to_utc(end_kst).isoformat(timespec='milliseconds')}")
    print(f"[DAY {day}] BATCH_SIZE={BATCH_SIZE}, SCROLL_TTL={SCROLL_TTL}")
    print(f"[DAY {day}] OUT_JSON={out_json}")

    # 1) call 세션 수집
    call_query = {
        "size": BATCH_SIZE,
        "_source": CALL_SEED_SOURCE_FIELDS,
        "query": {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": time_gte, "lt": time_lt}}},
                    {"term": {"type.keyword": TARGET_TYPE}}
                ]
            }
        }
    }

    call_session_ids = set()
    call_seed = {}

    processed_call = 0
    for h in scroll_search(indices, call_query):
        src = h.get("_source") or {}
        sid = src.get("session_uuid")
        if not sid:
            continue

        call_session_ids.add(sid)
        processed_call += 1

        if sid not in call_seed:
            call_seed[sid] = {
                "local_uuid": pick_local_uuid(src),
                "session_uuid": sid,
                "device": resolve_device_from_source(src),
                "@timestamp": normalize_timestamp_to_kst_string(src.get("@timestamp")),
            }

        if processed_call % 20000 == 0:
            print(f"[DAY {day}] call_docs={processed_call}, call_sessions={len(call_session_ids)}")

    print(f"[DAY {day}] call_docs={processed_call}, unique_call_sessions={len(call_session_ids)}")

    if not call_session_ids:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        print(f"[DAY {day}] DONE no call sessions -> {out_json}")
        return

    # 2) 해당 session_uuid들의 전체 footprints 수집(같은 KST 하루 범위)
    session_ids_list = list(call_session_ids)

    result = defaultdict(lambda: {
        "local_uuid": None,
        "session_uuid": None,
        "utm": {},
        "referrer": None,
        "device": None,
        "footprints": [],
    })

    total_docs = 0

    for batch_idx, batch in enumerate(chunked(session_ids_list, 2000), start=1):
        fp_query = {
            "size": BATCH_SIZE,
            "_source": SESSION_FOOTPRINT_SOURCE_FIELDS,
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": time_gte, "lt": time_lt}}},
                        {"terms": {"session_uuid.keyword": batch}}
                    ]
                }
            },
            "sort": [{"@timestamp": "asc"}]
        }

        for h in scroll_search(indices, fp_query):
            src = h.get("_source") or {}
            sid = src.get("session_uuid")
            if not sid:
                continue

            entry = result[sid]
            entry["session_uuid"] = sid

            seed = call_seed.get(sid)
            if seed:
                if (not entry.get("local_uuid")) and seed.get("local_uuid"):
                    entry["local_uuid"] = seed.get("local_uuid")
                if not entry.get("device"):
                    if seed.get("device"):
                        entry["device"] = seed.get("device")
            if not entry.get("device"):
                dv = resolve_device_from_source(src)
                if dv:
                    entry["device"] = dv

            mu = merged_utm_from_single_source(src)
            entry.setdefault("_utm_candidates", []).append((src.get("@timestamp"), mu))

            slim = slim_source(src)
            entry["footprints"].append(slim)
            lu = pick_local_uuid(src)
            if (not entry.get("local_uuid")) and lu:
                entry["local_uuid"] = lu

            total_docs += 1
            if total_docs % 200000 == 0:
                print(f"[DAY {day}] processed_docs={total_docs}, sessions={len(result)}")

        print(f"[DAY {day}] batch={batch_idx}, processed_docs={total_docs}, sessions={len(result)}")

    out = []
    for sid, entry in result.items():
        entry["utm"] = resolve_session_utm_from_earliest_footprint_event(entry.pop("_utm_candidates", []))
        entry["referrer"] = None

        if entry.get("utm") == {}:
            entry["utm"] = None

        entry["footprints"].sort(key=lambda x: x.get("@timestamp") or "")
        entry["footprints_count"] = len(entry["footprints"])
        out.append(build_output_entry(entry))

    out.sort(key=lambda x: x.get("session_uuid") or "")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[DAY {day}] DONE saved -> {out_json}")
    print(f"[DAY {day}] DONE call_sessions={len(out)}, footprints_docs={total_docs}")


def main():
    try:
        args = parse_args()
        target_dates = resolve_target_dates(args)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    print(f"[INFO] OUT_DIR={args.out_dir}")
    print(f"[INFO] TARGET_TYPE={TARGET_TYPE}")
    print(f"[INFO] DATES={', '.join(target_dates)}")

    for day in target_dates:
        extract_one_day(day, args.out_dir)


if __name__ == "__main__":
    main()