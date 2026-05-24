"""
MPCI Engine — EconDB 항만 지표 4시간 수집
GitHub Actions: 매 4시간 실행

주의: EconDB를 1시간보다 짧은 주기로 호출하지 않는다.
"""

import logging
import os
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from supabase import create_client, Client

# scripts/ 에서 standalone 실행됨(python scripts/fetch_econdb.py). 이때 sys.path[0]=scripts/ 라
# api/ 가 안 보이므로, repo 루트를 path 에 넣어 api 패키지의 mpci_core 를 import 한다.
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.mpci_core import compute_mpci, combine_final_mpci

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ECONDB_URL = "https://www.econdb.com/maritime/search/ports/"
ECONDB_PORT_URL = "https://www.econdb.com/maritime/ports/async/"  # 단건(async) 폴백
WARMUP_URL = "https://www.econdb.com/maritime/search/"            # 세션/쿠키 발급용

# page>=2 의 WAF/캐시-미스 차단을 피하려면 비브라우저 티를 내면 안 된다.
# 정직한 봇 UA(MTLLink-MPCI-Monitor)는 EconDB와 정식 계약 후 다시 사용할 것.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": WARMUP_URL,
}
PAGE_SLEEP_SECONDS = (1.0, 3.0)
PER_PORT_SLEEP_SECONDS = (1.0, 2.5)
MAX_RETRIES = 3

ECONDB_FIELDS = (
    "rank,name,locode,country,region,coord,turnaround,schedule,"
    "delay_percent,port_congestion,id"
)

# GitHub Actions 정각 회피: 0~60초 랜덤 대기
JITTER_MAX_SECONDS = 60
HISTORY_LOOKBACK_DAYS = int(os.getenv("MPCI_HISTORY_LOOKBACK_DAYS", "365"))
PERIOD_DAYS = int(os.getenv("MPCI_PERIOD_DAYS", "14"))
DEFAULT_WATCHLIST = {
    "USLAX", "USLGB", "USNYC", "USSAV", "CAVAN",
    "CNSHA", "CNNGB", "CNSZX", "CNQIN", "CNYTN", "CNYAT", "HKHKG",
    "SGSIN", "MYTPP", "MYPKG", "THLCH", "VNTOT", "PHMNL",
    "KRPUS", "JPTYO", "JPYOK", "JPNGO",
    "NLRTM", "BEANR", "DEHAM", "GBFXT", "FRLEH", "ESVLC", "ITGOA",
    "INNSA", "INMUN", "LKCMB", "AEJEA", "OMSLL",
    "BRSSZ", "MXZLO", "ZADUR", "EGPSD",
}


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def normalize_locode(locode: str) -> str:
    """Convert EconDB LOCODE like 'US LAX' to internal code 'USLAX'."""
    return "".join(locode.strip().upper().split())


def get_watchlist() -> set[str] | None:
    raw = os.getenv("ECONDB_WATCHLIST", "").strip()
    if raw.upper() == "ALL":
        return None
    if not raw:
        return set(DEFAULT_WATCHLIST)
    return {normalize_locode(part) for part in raw.split(",") if part.strip()}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def calc_current_index(port: dict) -> float | None:
    # 산식은 mpci_core 한 곳에만. EconDB 검색 docs 스키마(port_congestion 등)로 전달.
    return compute_mpci(
        port.get("port_congestion"),
        port.get("delay_percent"),
        port.get("turnaround"),
    )


def parse_observed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_coord(coord: str | None) -> tuple[float | None, float | None]:
    if not coord:
        return None, None
    try:
        lat_s, lon_s = coord.split(",", 1)
        return float(lat_s), float(lon_s)
    except (TypeError, ValueError):
        return None, None


def map_region(locode: str, country: str | None, econdb_region: str | None) -> str:
    code = normalize_locode(locode)
    country = (country or "").upper()

    if code.startswith(("KR", "JP")):
        return "kr-jp"
    if code.startswith(("CN", "HK", "TW")):
        return "china"
    if country in {"SG", "MY", "TH", "VN", "ID", "PH", "MM", "KH"}:
        return "sea"
    if country in {"IN", "PK", "LK", "BD", "AE", "SA", "OM", "QA", "BH", "KW", "IR", "JO"}:
        return "sa-me"
    if country in {
        "NL", "BE", "DE", "GB", "FR", "ES", "IT", "GR", "PT", "PL",
        "SE", "NO", "FI", "DK", "IE", "TR",
    }:
        return "europe"
    if country in {"US", "CA"}:
        return "namerica"
    if country in {"MX", "BR", "AR", "CL", "CO", "PE", "PA", "EC", "UY"}:
        return "latam"
    if country in {"ZA", "EG", "MA", "NG", "KE", "TZ", "GH", "CI", "SN"}:
        return "africa"
    if country in {"RU", "UA", "GE", "KZ"}:
        return "ru-cis"

    return (econdb_region or "").strip()[:30] or "other"


def make_client() -> tuple[object, str]:
    """브라우저처럼 보이는 HTTP 클라이언트 생성.

    page>=2 차단을 뚫는 가장 확실한 방법은 TLS/JA3 지문까지 위장하는 curl_cffi.
    설치돼 있으면 우선 사용하고, 없으면 httpx + 브라우저 헤더로 폴백한다.
    검색 페이지에 워밍업 GET을 보내 세션 쿠키를 미리 확보한다.

    curl_cffi 설치:  pip install curl_cffi
    """
    client = None
    backend = "httpx"
    try:
        from curl_cffi import requests as cffi_requests

        # curl_cffi 버전마다 유효한 impersonate 타깃이 다르므로 순차 시도
        for imp in ("chrome", "chrome124", "chrome120", "chrome110"):
            try:
                client = cffi_requests.Session(impersonate=imp)
                backend = f"curl_cffi:{imp}"
                break
            except Exception:
                client = None
        if client is None:
            raise RuntimeError("no valid curl_cffi impersonate target")
        client.headers.update(BROWSER_HEADERS)
    except Exception:
        client = httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True)
        backend = "httpx"

    try:
        client.get(WARMUP_URL, timeout=30)  # 세션/쿠키 발급
        logger.info("Warmup OK (backend=%s)", backend)
    except Exception as e:
        logger.warning("Warmup failed (backend=%s): %s", backend, e)

    return client, backend


def fetch_page(client, page: int) -> tuple[list[dict], int | None, str]:
    """한 페이지 조회. (docs, num_found, status) 반환.

    status: 'ok'      정상
            'blocked' 403/429 (page>=2 차단) → 폴백 트리거
            'error'   기타 오류
    """
    url = (
        f"{ECONDB_URL}?page_size=40&page={page}&s="
        f"&fl={ECONDB_FIELDS.replace(',', '%2C')}"
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, timeout=60)  # 헤더/쿠키는 세션에 있음
            if resp.status_code in (403, 429):
                logger.warning("EconDB blocked: status=%s page=%s", resp.status_code, page)
                return [], None, "blocked"
            resp.raise_for_status()
            data = resp.json()
            response = data.get("response", {}) if isinstance(data, dict) else {}
            docs = response.get("docs", [])
            num_found = response.get("numFound")
            return (docs if isinstance(docs, list) else []), num_found, "ok"
        except Exception as e:
            if attempt == MAX_RETRIES:
                logger.error("EconDB page fetch failed page=%s: %s", page, e)
                return [], None, "error"
            sleep_s = 2 ** attempt
            logger.warning("Retrying EconDB page=%s in %ss after error: %s", page, sleep_s, e)
            time.sleep(sleep_s)

    return [], None, "error"


def _fetch_paginated(client, watchlist: set[str] | None) -> tuple[list[dict], set[str], bool]:
    """[2번] 워밍업된 세션으로 페이지네이션. (ports, seen, blocked) 반환."""
    ports: list[dict] = []
    seen: set[str] = set()
    max_pages = int(os.getenv("ECONDB_MAX_PAGES", "0") or "0")
    blocked = False
    page = 1
    num_found: int | None = None

    while True:
        docs, total, status = fetch_page(client, page)
        if status == "blocked":
            blocked = True
            break
        if status == "error":
            break
        if total is not None:
            num_found = total
        if not docs:
            break

        added = 0
        for doc in docs:
            locode = doc.get("locode")
            if not locode:
                continue
            code = normalize_locode(str(locode))
            if not code or code in seen:
                continue
            if watchlist is not None and code not in watchlist:
                continue
            seen.add(code)
            ports.append(doc)
            added += 1

        logger.info("page %s: docs=%s added=%s seen=%s/%s",
                    page, len(docs), added, len(ports), num_found or "?")

        if max_pages and page >= max_pages:
            logger.info("Stop at ECONDB_MAX_PAGES=%s", max_pages)
            break
        if watchlist is not None and watchlist.issubset(seen):
            logger.info("All watchlist ports found via pagination (%s).", len(watchlist))
            break
        if num_found is not None and len(ports) >= num_found:
            break

        page += 1
        time.sleep(random.uniform(*PAGE_SLEEP_SECONDS))

    return ports, seen, blocked


def _deep_get(obj, keys: tuple[str, ...]):
    """중첩 dict/list 어디에 있든 keys 중 첫 번째로 존재하는 non-null 값 반환.

    async 단건 엔드포인트의 정확한 응답 스키마가 미확인이라 방어적으로 탐색한다.
    첫 폴백 호출 때 로그로 찍히는 'ASYNC SAMPLE' 키 목록을 보고 keys 후보를 보정할 것.
    """
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        for v in obj.values():
            found = _deep_get(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_get(item, keys)
            if found is not None:
                return found
    return None


def _normalize_async_doc(raw, code: str) -> dict | None:
    """async 응답을 검색 docs와 동일한 스키마로 변환.

    ⚠️ 키 이름은 추정값이다. 실제 응답(로그의 ASYNC SAMPLE)을 보고 아래 후보 목록을
    반드시 검증/보정해야 한다. 셋 다 못 찾으면 None 반환(스킵).
    """
    congestion = _deep_get(raw, ("port_congestion", "congestion"))
    delay = _deep_get(raw, ("delay_percent", "delay_pct", "delay"))
    turnaround = _deep_get(raw, ("turnaround", "turnaround_days", "median_turnaround"))
    if congestion is None and delay is None and turnaround is None:
        return None
    return {
        "locode": f"{code[:2]} {code[2:]}",
        "name": _deep_get(raw, ("name", "port_name")) or code,
        "country": _deep_get(raw, ("country", "country_code")),
        "region": _deep_get(raw, ("region",)),
        "coord": _deep_get(raw, ("coord", "coordinates")),
        "turnaround": turnaround,
        "schedule": _deep_get(raw, ("schedule", "schedule_reliability")),
        "delay_percent": delay,
        "port_congestion": congestion,
    }


def _fetch_per_port(client, codes: set[str]) -> list[dict]:
    """[3번] 항만별 단건(async) 폴백. 각 호출은 direct GET(page=1 등가)이라 차단 회피."""
    from urllib.parse import quote

    out: list[dict] = []
    logged_sample = False
    for code in sorted(codes):
        # 'USLAX' -> 'US LAX' (표준 5자리 LOCODE: 국가 2 + 위치 3)
        locode = f"{code[:2]} {code[2:]}"
        url = f"{ECONDB_PORT_URL}{quote(locode)}/"
        try:
            resp = client.get(url, timeout=60)
            if resp.status_code in (403, 429):
                logger.warning("per-port blocked status=%s code=%s", resp.status_code, code)
                continue
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            logger.warning("per-port fetch failed code=%s: %s", code, e)
            continue

        if not logged_sample:
            sample = list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__
            logger.info("ASYNC SAMPLE (%s) top-level keys=%s", code, sample)
            logged_sample = True

        doc = _normalize_async_doc(raw, code)
        if doc:
            out.append(doc)
        else:
            logger.warning("per-port: %s 지표 추출 실패 (스키마 불일치? _normalize_async_doc 보정 필요)", code)

        time.sleep(random.uniform(*PER_PORT_SLEEP_SECONDS))

    logger.info("per-port fallback recovered %s/%s ports", len(out), len(codes))
    return out


def fetch_econdb_ports() -> list[dict]:
    """[2번] 워밍업 세션 페이지네이션 → 차단/누락 시 [3번] 항만별 단건 폴백."""
    watchlist = get_watchlist()
    client, backend = make_client()

    try:
        # ── 2번: 페이지네이션 ──────────────────────────────────────────
        ports, seen, blocked = _fetch_paginated(client, watchlist)
        logger.info("Pagination done (backend=%s, blocked=%s): %s ports",
                    backend, blocked, len(ports))

        # ── 3번: 폴백 (watchlist 누락분만) ────────────────────────────
        if watchlist is not None:
            missing = watchlist - seen
            if missing:
                logger.warning(
                    "Pagination incomplete %s/%s. Per-port fallback for %s ports: %s",
                    len(seen), len(watchlist), len(missing), sorted(missing),
                )
                for doc in _fetch_per_port(client, missing):
                    code = normalize_locode(str(doc.get("locode", "")))
                    if code and code not in seen:
                        seen.add(code)
                        ports.append(doc)
        elif blocked:
            # ALL 모드는 누락 목록을 특정할 수 없어 단건 폴백 불가
            logger.warning("ALL 모드에서 차단 발생 — 단건 폴백 불가. watchlist 지정을 권장.")
    finally:
        try:
            client.close()
        except Exception:
            pass

    # ── 커버리지 검증: 부분 성공을 성공으로 착각하지 않기 ────────────
    if watchlist is not None:
        still_missing = watchlist - seen
        if still_missing:
            logger.error("EconDB 커버리지 미달: %s/%s 누락=%s",
                         len(seen), len(watchlist), sorted(still_missing))
            # 기본은 graceful(워크플로 실패 방지). 엄격 모드는 env로 켠다.
            if os.getenv("ECONDB_STRICT_COVERAGE", "").lower() in ("1", "true", "yes"):
                raise SystemExit(1)

    logger.info("Fetched %s unique ports from EconDB", len(ports))
    return ports


def fetch_metric_history(supabase: Client, port_codes: list[str]) -> dict[str, list[dict]]:
    if not port_codes:
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_LOOKBACK_DAYS)).isoformat()
    rows: list[dict] = []
    page_size = 1000
    start = 0

    try:
        while True:
            resp = (
                supabase.table("port_metrics_history")
                .select("port_code,observed_at,econdb_current_index")
                .in_("port_code", port_codes)
                .gte("observed_at", cutoff)
                .order("observed_at", desc=False)
                .range(start, start + page_size - 1)
                .execute()
            )
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
    except Exception as e:
        logger.warning("Could not read port_metrics_history; using current-only MPCI: %s", e)
        return {}

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["port_code"]].append(row)
    return grouped


def fetch_signal_context(supabase: Client, port_codes: list[str]) -> dict[str, dict]:
    if not port_codes:
        return {}

    try:
        resp = (
            supabase.table("port_snapshots")
            .select(
                "port_code,portwatch_historic_index,portwatch_trend_index,"
                "portwatch_history_days,portwatch_activity_delta_pct,"
                "ais_wait_index,ais_anomaly_level"
            )
            .in_("port_code", port_codes)
            .execute()
        )
    except Exception as e:
        logger.warning("Could not read PortWatch/AIS signal context: %s", e)
        return {}

    return {row["port_code"]: row for row in (resp.data or []) if row.get("port_code")}


def calculate_historical_index(
    port_code: str,
    current_index: float,
    history: list[dict],
    now_dt: datetime,
    signal_context: dict | None = None,
) -> dict:
    current_start = now_dt - timedelta(days=PERIOD_DAYS)
    previous_start = now_dt - timedelta(days=PERIOD_DAYS * 2)
    previous_end = current_start

    values: list[tuple[datetime, float]] = []
    for row in history:
        observed_at = parse_observed_at(row.get("observed_at"))
        value = row.get("econdb_current_index")
        if observed_at is None or value is None:
            continue
        values.append((observed_at, float(value)))

    values.append((now_dt, current_index))
    values.sort(key=lambda item: item[0])

    current_values = [v for ts, v in values if ts >= current_start]
    previous_values = [v for ts, v in values if previous_start <= ts < previous_end]
    one_year_values = [v for ts, v in values if ts >= now_dt - timedelta(days=HISTORY_LOOKBACK_DAYS)]

    _cur_mean = mean(current_values)
    current_avg = _cur_mean if _cur_mean is not None else current_index
    previous_avg = mean(previous_values)
    percentile = None
    if len(one_year_values) >= PERIOD_DAYS:
        below_or_equal = sum(1 for v in one_year_values if v <= current_avg)
        percentile = round((below_or_equal / len(one_year_values)) * 100.0, 1)

    delta_prev = None
    delta_pct_prev = None
    trend_score = 50.0
    if previous_avg is not None:
        delta_prev = round(current_avg - previous_avg, 2)
        if previous_avg > 0:
            delta_pct_prev = round((current_avg / previous_avg) - 1.0, 4)
            trend_score = clamp(50.0 + delta_pct_prev * 100.0, 0.0, 100.0)

    if values:
        history_days = max(1, (values[-1][0].date() - values[0][0].date()).days + 1)
    else:
        history_days = 0

    portwatch_index = None
    if signal_context:
        try:
            raw_portwatch = signal_context.get("portwatch_historic_index")
            portwatch_index = None if raw_portwatch is None else clamp(float(raw_portwatch), 0.0, 100.0)
        except (TypeError, ValueError):
            portwatch_index = None

    if portwatch_index is not None:
        _pw_trend = signal_context.get("portwatch_trend_index")
        trend_score = _pw_trend if _pw_trend is not None else trend_score
        try:
            trend_score = clamp(float(trend_score), 0.0, 100.0)
        except (TypeError, ValueError):
            trend_score = 50.0

        ais_index = signal_context.get("ais_wait_index")
        # 합성식은 mpci_core 한 곳에만.
        final_mpci, confidence = combine_final_mpci(current_index, portwatch_index, ais_index)

        portwatch_days = signal_context.get("portwatch_history_days")
        try:
            history_days = int(portwatch_days) if portwatch_days is not None else history_days
        except (TypeError, ValueError):
            pass

        delta_pct_prev = signal_context.get("portwatch_activity_delta_pct")
        try:
            delta_pct_prev = None if delta_pct_prev is None else round(float(delta_pct_prev), 4)
        except (TypeError, ValueError):
            delta_pct_prev = None

        return {
            "econdb_current_index": round(current_index, 1),
            "historic_percentile_index": round(portwatch_index, 1),
            "trend_change_index": round(trend_score, 1),
            "final_mpci": round(clamp(final_mpci, 0.0, 100.0), 1),
            "mpci_confidence": confidence,
            "mpci_history_days": history_days,
            "mpci_period_start": current_start.date().isoformat(),
            "mpci_period_end": now_dt.date().isoformat(),
            "mpci_previous_start": previous_start.date().isoformat(),
            "mpci_previous_end": (previous_end - timedelta(days=1)).date().isoformat(),
            "mpci_delta_prev": delta_prev,
            "mpci_delta_pct_prev": delta_pct_prev,
        }

    if history_days < PERIOD_DAYS or previous_avg is None:
        final_mpci = current_index
        confidence = "current_only"
    elif history_days < 90:
        final_mpci = current_index * 0.70 + trend_score * 0.30
        confidence = "short_trend"
    elif history_days < HISTORY_LOOKBACK_DAYS or percentile is None:
        pct = percentile if percentile is not None else current_index
        final_mpci = current_index * 0.60 + pct * 0.25 + trend_score * 0.15
        confidence = "medium_history"
    else:
        final_mpci = current_index * 0.55 + percentile * 0.30 + trend_score * 0.15
        confidence = "full_history"

    return {
        "econdb_current_index": round(current_index, 1),
        "historic_percentile_index": percentile,
        "trend_change_index": round(trend_score, 1),
        "final_mpci": round(clamp(final_mpci, 0.0, 100.0), 1),
        "mpci_confidence": confidence,
        "mpci_history_days": history_days,
        "mpci_period_start": current_start.date().isoformat(),
        "mpci_period_end": now_dt.date().isoformat(),
        "mpci_previous_start": previous_start.date().isoformat(),
        "mpci_previous_end": (previous_end - timedelta(days=1)).date().isoformat(),
        "mpci_delta_prev": delta_prev,
        "mpci_delta_pct_prev": delta_pct_prev,
    }


def update_supabase(supabase: Client, ports: list[dict]) -> None:
    """
    port_snapshots 테이블에서 LOCODE가 일치하는 행의 EconDB 컬럼 업데이트.
    컬럼: econdb_congestion, econdb_delay_pct, econdb_turnaround, econdb_updated_at
    """
    now_str = datetime.now(timezone.utc).isoformat()
    now_dt = datetime.now(timezone.utc)
    history_by_port = fetch_metric_history(
        supabase,
        [normalize_locode(str(port.get("locode", ""))) for port in ports if port.get("locode")],
    )
    port_codes = [normalize_locode(str(port.get("locode", ""))) for port in ports if port.get("locode")]
    signal_by_port = fetch_signal_context(supabase, port_codes)
    upsert_rows: list[dict] = []
    history_rows: list[dict] = []

    for port in ports:
        locode = (
            port.get("locode") or
            port.get("LOCODE") or
            port.get("port_locode") or
            port.get("un_locode", "")
        )
        if not locode:
            continue

        port_code = normalize_locode(str(locode))
        if not port_code:
            continue

        congestion  = port.get("port_congestion")
        delay_pct   = port.get("delay_percent")
        turnaround  = port.get("turnaround")

        # 값이 하나도 없으면 스킵
        if congestion is None and delay_pct is None and turnaround is None:
            continue

        current_index = calc_current_index(port)
        if current_index is None:
            continue

        index_data = calculate_historical_index(
            port_code,
            current_index,
            history_by_port.get(port_code, []),
            now_dt,
            signal_by_port.get(port_code),
        )

        lat, lon = parse_coord(port.get("coord"))
        update_data: dict = {
            "port_code": port_code,
            "port_name": port.get("name") or port_code,
            "country": port.get("country"),
            "region": map_region(str(locode), port.get("country"), port.get("region")),
            "updated_at": now_str,
            "econdb_updated_at": now_str,
            **index_data,
        }
        if lat is not None:
            update_data["lat"] = lat
        if lon is not None:
            update_data["lon"] = lon
        if congestion is not None:
            try:
                update_data["econdb_congestion"] = float(congestion)
            except (TypeError, ValueError):
                pass
        if delay_pct is not None:
            try:
                update_data["econdb_delay_pct"] = float(delay_pct)
            except (TypeError, ValueError):
                pass
        if turnaround is not None:
            try:
                update_data["econdb_turnaround"] = float(turnaround)
            except (TypeError, ValueError):
                pass
        if port.get("schedule") is not None:
            try:
                update_data["econdb_schedule"] = float(port["schedule"])
            except (TypeError, ValueError):
                pass

        upsert_rows.append(update_data)
        history_rows.append({
            "port_code": port_code,
            "observed_at": now_str,
            "source": "econdb",
            "econdb_congestion": update_data.get("econdb_congestion"),
            "econdb_delay_pct": update_data.get("econdb_delay_pct"),
            "econdb_turnaround": update_data.get("econdb_turnaround"),
            "econdb_schedule": update_data.get("econdb_schedule"),
            "econdb_current_index": index_data["econdb_current_index"],
            "historic_percentile_index": index_data["historic_percentile_index"],
            "trend_change_index": index_data["trend_change_index"],
            "final_mpci": index_data["final_mpci"],
            "mpci_confidence": index_data["mpci_confidence"],
            "mpci_history_days": index_data["mpci_history_days"],
            "mpci_delta_prev": index_data["mpci_delta_prev"],
            "mpci_delta_pct_prev": index_data["mpci_delta_pct_prev"],
        })

    if not upsert_rows:
        logger.info("No EconDB rows to upsert.")
        return

    try:
        supabase.table("port_snapshots").upsert(
            upsert_rows, on_conflict="port_code"
        ).execute()
    except Exception as e:
        logger.error("Failed to upsert EconDB rows: %s", e)
        raise

    logger.info("Upserted EconDB data for %s ports.", len(upsert_rows))

    try:
        supabase.table("port_metrics_history").insert(history_rows).execute()
        logger.info("Inserted %s rows into port_metrics_history.", len(history_rows))
    except Exception as e:
        logger.warning("Failed to insert port_metrics_history rows: %s", e)


def main() -> None:
    # GitHub Actions 정각 회피: 랜덤 지터
    jitter = random.uniform(0, JITTER_MAX_SECONDS)
    logger.info(f"Sleeping {jitter:.1f}s to avoid GHA cron spike...")
    time.sleep(jitter)

    logger.info("fetch_econdb.py starting...")

    ports = fetch_econdb_ports()
    if not ports:
        logger.warning("No ports fetched from EconDB. Exiting gracefully.")
        # exit(0) — 워크플로 실패 방지
        return

    supabase = get_supabase()
    update_supabase(supabase, ports)

    logger.info("fetch_econdb.py completed successfully.")


if __name__ == "__main__":
    main()
