#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
input.txt(한 줄에 session_uuid 하나)에 나열된 세션의 footprints를 ES에서 추출한다.
출력 JSON 형태는 extract-kakao_sessions-footprints-weekly.py 와 동일하게 맞춘다
(local_uuid는 세션 객체에만 두고, footprints 항목에는 넣지 않는다).
"""

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

KST = timezone(timedelta(hours=9))


def _parse_local_uuid_source_keys() -> list[str]:
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


FOOTPRINT_FIELDS = [
    "@timestamp",
    "url",
    "referrer",
    "type",
    "event",
]

_SEED_SOURCE_FIELDS_BASE = [
    "local_uuid",
    "session_uuid",
    "@timestamp",
    "url",
    "referrer",
    "utm",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "type",
    "event",
    "device",
    "user_agent",
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

SEED_SOURCE_FIELDS = _merge_source_field_list(
    _SEED_SOURCE_FIELDS_BASE,
    _EXTRA_SOURCE_ROOTS_LOCAL_UUID,
)
SESSION_FOOTPRINT_SOURCE_FIELDS = _merge_source_field_list(
    _SESSION_FOOTPRINT_SOURCE_FIELDS_BASE,
    _EXTRA_SOURCE_ROOTS_LOCAL_UUID,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Read session_uuid list from a text file and extract all footprints in a KST date range."
    )
    p.add_argument(
        "--input-file",
        default="input.txt",
        help="Path to text file: one session_uuid per line (empty lines and # comments ignored). Default: input.txt",
    )
    p.add_argument(
        "--start-date",
        required=True,
        help="Start date (inclusive), KST. Format: YYYY-MM-DD",
    )
    p.add_argument(
        "--end-date",
        required=True,
        help="End date (inclusive), KST. Format: YYYY-MM-DD",
    )
    p.add_argument(
        "--out",
        default="",
        help="Output JSON path. Default: sessions_footprints_by_uuid_<start>~<end>.json in current directory",
    )
    return p.parse_args()


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


def parse_kst_day_start(day_yyyy_mm_dd: str) -> datetime:
    return datetime.strptime(day_yyyy_mm_dd, "%Y-%m-%d").replace(tzinfo=KST)


def to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_day_range_utc(day_yyyy_mm_dd: str):
    start_kst = parse_kst_day_start(day_yyyy_mm_dd)
    end_kst = start_kst + timedelta(days=1)
    return iso_z(to_utc(start_kst)), iso_z(to_utc(end_kst))


def iso_range_kst_span_inclusive(start_ymd: str, end_ymd: str):
    start_kst = parse_kst_day_start(start_ymd)
    end_kst = parse_kst_day_start(end_ymd) + timedelta(days=1)
    return iso_z(to_utc(start_kst)), iso_z(to_utc(end_kst))


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
            patterns.append(f"{site}-{ym}*")
    return patterns


def build_index_patterns_for_range(start_ymd: str, end_ymd: str):
    acc = set()
    for day in daterange_inclusive(start_ymd, end_ymd):
        for p in build_index_patterns_for_day(day):
            acc.add(p)
    return sorted(acc)


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
        rebuilt = urlunparse((
            p.scheme,
            p.netloc,
            decode_value(p.path),
            p.params,
            decode_value(p.query),
            decode_value(p.fragment),
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


def merged_utm_from_single_source(src: dict) -> dict:
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


def chunked(lst, size=2000):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def read_session_uuids(path: str) -> list[str]:
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"Input file not found: {p}")
    seen = set()
    ordered: list[str] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s not in seen:
                seen.add(s)
                ordered.append(s)
    if not ordered:
        raise ValueError(f"No session_uuid lines in {p}")
    return ordered


def build_seed_from_src(src: dict, sid: str) -> dict:
    mu = merged_utm_from_single_source(src)
    return {
        "local_uuid": pick_local_uuid(src),
        "session_uuid": sid,
        "device": resolve_device_from_source(src),
        "@timestamp": normalize_timestamp_to_kst_string(src.get("@timestamp")),
        "_utm_candidate": (src.get("@timestamp"), mu),
    }


def collect_first_seed_per_session(indices: str, time_gte: str, time_lt: str, session_ids: list[str]) -> dict:
    """각 session_uuid에 대해 시간순 첫 문서로 seed(device/local_uuid 및 UTM 후보)를 만든다."""
    session_seed: dict[str, dict] = {}
    for batch in chunked(session_ids, 2000):
        q = {
            "size": BATCH_SIZE,
            "_source": SEED_SOURCE_FIELDS,
            "sort": [{"@timestamp": "asc"}],
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": time_gte, "lt": time_lt}}},
                        {"terms": {"session_uuid.keyword": batch}},
                    ]
                }
            },
        }
        for h in scroll_search(indices, q):
            src = h.get("_source") or {}
            sid = src.get("session_uuid")
            if not sid or sid in session_seed:
                continue
            session_seed[sid] = build_seed_from_src(src, sid)
    return session_seed


def extract_by_uuids(session_ids: list[str], start_ymd: str, end_ymd: str, out_path: str):
    time_gte, time_lt = iso_range_kst_span_inclusive(start_ymd, end_ymd)
    patterns = build_index_patterns_for_range(start_ymd, end_ymd)
    indices = ",".join(patterns)

    print(f"[INFO] ES_URL={ES_URL}")
    print(f"[INFO] RANGE_UTC={time_gte} ~ {time_lt}")
    print(f"[INFO] SESSIONS={len(session_ids)}")
    print(f"[INFO] INDICES(count)={len(patterns)}")
    print(f"[INFO] OUT={out_path}")

    session_seed = collect_first_seed_per_session(indices, time_gte, time_lt, session_ids)

    result = defaultdict(
        lambda: {
            "local_uuid": None,
            "session_uuid": None,
            "utm": {},
            "referrer": None,
            "device": None,
            "footprints": [],
        }
    )

    total_docs = 0
    for batch_idx, batch in enumerate(chunked(session_ids, 2000), start=1):
        fp_query = {
            "size": BATCH_SIZE,
            "_source": SESSION_FOOTPRINT_SOURCE_FIELDS,
            "sort": ["_doc"],
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": time_gte, "lt": time_lt}}},
                        {"terms": {"session_uuid.keyword": batch}},
                    ]
                }
            },
        }
        for h in scroll_search(indices, fp_query):
            src = h.get("_source") or {}
            sid = src.get("session_uuid")
            if not sid:
                continue
            entry = result[sid]
            entry["session_uuid"] = sid
            seed = session_seed.get(sid)
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
        print(f"[INFO] batch={batch_idx} processed_docs={total_docs} sessions_in_result={len(result)}")

    out: list[dict] = []
    for sid in session_ids:
        entry = result.get(sid)
        seed = session_seed.get(sid)
        if entry is None:
            if seed:
                cands = []
                if seed.get("_utm_candidate"):
                    cands.append(seed["_utm_candidate"])
                utm = resolve_session_utm_from_earliest_footprint_event(cands)
                if utm == {}:
                    utm = None
                out.append(
                    build_output_entry(
                        {
                            "local_uuid": seed.get("local_uuid"),
                            "session_uuid": sid,
                            "utm": utm,
                            "referrer": None,
                            "device": seed.get("device"),
                            "footprints": [],
                            "footprints_count": 0,
                        }
                    )
                )
            else:
                out.append(
                    build_output_entry(
                        {
                            "local_uuid": None,
                            "session_uuid": sid,
                            "utm": None,
                            "referrer": None,
                            "device": None,
                            "footprints": [],
                            "footprints_count": 0,
                        }
                    )
                )
            continue

        cands = entry.pop("_utm_candidates", [])
        entry["utm"] = resolve_session_utm_from_earliest_footprint_event(cands)
        entry["referrer"] = None

        if entry.get("utm") == {}:
            entry["utm"] = None

        entry["footprints"].sort(key=lambda x: x.get("@timestamp") or "")
        entry["footprints_count"] = len(entry["footprints"])
        out.append(build_output_entry(entry))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[DONE] sessions_written={len(out)} footprint_hits={total_docs} -> {out_path}")


def main():
    try:
        args = parse_args()
        start = validate_ymd(args.start_date)
        end = validate_ymd(args.end_date)
        daterange_inclusive(start, end)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        uuids = read_session_uuids(args.input_file)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    out_path = args.out.strip()
    if not out_path:
        out_path = f"sessions_footprints_by_uuid_{start}~{end}.json"

    try:
        extract_by_uuids(uuids, start, end, out_path)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
