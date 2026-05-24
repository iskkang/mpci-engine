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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ECONDB_URL = "https://www.econdb.com/maritime/search/ports/"
HEADERS = {"User-Agent": "MTLLink-MPCI-Monitor/1.0"}
PAGE_SLEEP_SECONDS = (1.0, 3.0)
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
    cong = port.get("port_congestion")
    delay_pct = port.get("delay_percent")
    turnaround = port.get("turnaround")
    if cong is None or delay_pct is None or turnaround is None:
        return None
    try:
        cong_score = clamp((float(cong) / 18.0) * 100.0, 0.0, 100.0)
        delay_score = clamp(float(delay_pct), 0.0, 100.0)
        turn_score = clamp(((float(turnaround) - 0.5) / 4.5) * 100.0, 0.0, 100.0)
    except (TypeError, ValueError):
        return None
    return round(clamp(cong_score * 0.35 + delay_score * 0.35 + turn_score * 0.30, 0.0, 100.0), 1)


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


def fetch_page(client: httpx.Client, page: int) -> tuple[list[dict], int | None]:
    # Use the paged port table endpoint. It returns the same EconDB metrics we
    # need and is less brittle than the map bbox endpoint for batch collection.
    url = (
        f"{ECONDB_URL}?page_size=40&page={page}&s="
        f"&fl={ECONDB_FIELDS.replace(',', '%2C')}"
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, headers=HEADERS, timeout=60)
            if resp.status_code in {403, 429}:
                logger.warning("EconDB rate limited or blocked: status=%s page=%s", resp.status_code, page)
                return [], None
            resp.raise_for_status()
            data = resp.json()
            response = data.get("response", {}) if isinstance(data, dict) else {}
            docs = response.get("docs", [])
            num_found = response.get("numFound")
            return docs if isinstance(docs, list) else [], num_found
        except Exception as e:
            if attempt == MAX_RETRIES:
                logger.error("EconDB page fetch failed page=%s: %s", page, e)
                return [], None
            sleep_s = 2 ** attempt
            logger.warning("Retrying EconDB page=%s in %ss after error: %s", page, sleep_s, e)
            time.sleep(sleep_s)

    return [], None


def fetch_econdb_ports() -> list[dict]:
    """Fetch paginated EconDB port docs with polite pacing."""
    ports: list[dict] = []
    seen: set[str] = set()
    max_pages = int(os.getenv("ECONDB_MAX_PAGES", "0") or "0")
    watchlist = get_watchlist()

    with httpx.Client() as client:
        page = 1
        num_found: int | None = None
        while True:
            docs, total = fetch_page(client, page)
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
                if watchlist is not None and code not in watchlist:
                    continue
                if not code or code in seen:
                    continue
                seen.add(code)
                ports.append(doc)
                added += 1

            logger.info("Fetched EconDB page %s: docs=%s added=%s total_seen=%s/%s",
                        page, len(docs), added, len(ports), num_found or "?")

            if max_pages and page >= max_pages:
                logger.info("Stopping at ECONDB_MAX_PAGES=%s", max_pages)
                break
            if watchlist is not None and watchlist.issubset(seen):
                logger.info("All watchlist ports found (%s).", len(watchlist))
                break
            if num_found is not None and len(ports) >= num_found:
                break

            page += 1
            time.sleep(random.uniform(*PAGE_SLEEP_SECONDS))

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

    current_avg = mean(current_values) or current_index
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
        trend_score = signal_context.get("portwatch_trend_index") or trend_score
        try:
            trend_score = clamp(float(trend_score), 0.0, 100.0)
        except (TypeError, ValueError):
            trend_score = 50.0

        ais_index = signal_context.get("ais_wait_index")
        if ais_index is not None:
            try:
                final_mpci = current_index * 0.50 + portwatch_index * 0.35 + clamp(float(ais_index), 0.0, 100.0) * 0.15
                confidence = "portwatch_ais"
            except (TypeError, ValueError):
                final_mpci = current_index * 0.60 + portwatch_index * 0.40
                confidence = "portwatch_history"
        else:
            final_mpci = current_index * 0.60 + portwatch_index * 0.40
            confidence = "portwatch_history"

        portwatch_days = signal_context.get("portwatch_history_days")
        try:
            history_days = int(portwatch_days or history_days)
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
