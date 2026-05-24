"""
MPCI Engine - PortWatch port-level historical baseline fetcher.

PortWatch is used as the long-term activity baseline. EconDB remains the
current delay/dwell signal, and AIS remains a supplemental queue signal.
"""

import logging
import math
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

import httpx
from supabase import Client, create_client

# scripts/ standalone 실행 대응: repo 루트를 path 에 넣어 api 패키지에서 import.
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.mpci_core import combine_final_mpci

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

CATALOG_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
    "/PortWatch_ports_database/FeatureServer/0/query"
)
DAILY_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services"
    "/Daily_Ports_Data/FeatureServer/0/query"
)

DEFAULT_WATCHLIST = {
    "USLAX", "USLGB", "USNYC", "USSAV", "CAVAN",
    "CNSHA", "CNNGB", "CNQIN", "CNYTN", "HKHKG",
    "SGSIN", "MYTPP", "MYPKG", "THLCH", "VNTOT", "PHMNL",
    "KRPUS", "JPTYO", "JPYOK", "JPNGO",
    "NLRTM", "BEANR", "DEHAM", "GBFXT", "FRLEH", "ESVLC", "ITGOA",
    "INNSA", "INMUN", "LKCMB", "AEJEA", "OMSLL",
    "BRSSZ", "MXZLO", "ZADUR", "EGPSD",
}

PAGE_SIZE = 1000
LOOKBACK_DAYS = int(os.getenv("PORTWATCH_LOOKBACK_DAYS", "400"))
PERIOD_DAYS = int(os.getenv("MPCI_PERIOD_DAYS", "14"))
MAX_MATCH_DISTANCE_KM = float(os.getenv("PORTWATCH_MATCH_MAX_KM", "125"))
REQUEST_SLEEP_SECONDS = float(os.getenv("PORTWATCH_REQUEST_SLEEP_SECONDS", "0.25"))


def get_supabase() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def normalize_code(value: str | None) -> str:
    return "".join((value or "").upper().split())


def get_watchlist() -> set[str] | None:
    raw = (os.getenv("PORTWATCH_WATCHLIST") or os.getenv("ECONDB_WATCHLIST") or "").strip()
    if raw.upper() == "ALL":
        return None
    if raw:
        return {normalize_code(part) for part in raw.split(",") if part.strip()}
    return set(DEFAULT_WATCHLIST)


def parse_manual_map() -> dict[str, str]:
    raw = os.getenv("PORTWATCH_PORT_MAP", "").strip()
    result: dict[str, str] = {}
    if not raw:
        return result
    for part in raw.split(","):
        if ":" not in part:
            continue
        code, portid = part.split(":", 1)
        code = normalize_code(code)
        portid = portid.strip()
        if code and portid:
            result[code] = portid
    return result


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def name_score(left: str | None, right: str | None) -> float:
    lval = (left or "").lower().replace("port of", "").strip()
    rval = (right or "").lower().replace("port of", "").strip()
    if not lval or not rval:
        return 0.0
    return SequenceMatcher(None, lval, rval).ratio()


def fetch_catalog() -> list[dict]:
    records: list[dict] = []
    offset = 0
    with httpx.Client(timeout=45) as client:
        while True:
            params = {
                "where": "1=1",
                "outFields": "portid,portname,fullname,country,ISO3,LOCODE,lat,lon",
                "f": "json",
                "resultRecordCount": PAGE_SIZE,
                "resultOffset": offset,
            }
            resp = client.get(CATALOG_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(data["error"])
            features = data.get("features", [])
            for feature in features:
                attr = feature.get("attributes") or {}
                if attr.get("portid"):
                    records.append(attr)
            if not data.get("exceededTransferLimit"):
                break
            offset += PAGE_SIZE
            time.sleep(REQUEST_SLEEP_SECONDS)
    logger.info("Fetched %s PortWatch catalog ports.", len(records))
    return records


def fetch_snapshot_ports(supabase: Client) -> list[dict]:
    watchlist = get_watchlist()
    query = (
        supabase.table("port_snapshots")
        .select(
            "port_code,port_name,country,lat,lon,econdb_current_index,"
            "ais_wait_index,ais_anomaly_level"
        )
        .order("port_code")
    )
    resp = query.execute()
    rows = resp.data or []
    if watchlist is not None:
        rows = [row for row in rows if normalize_code(row.get("port_code")) in watchlist]
    return rows


def build_catalog_indexes(catalog: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_portid = {row["portid"]: row for row in catalog if row.get("portid")}
    by_locode: dict[str, dict] = {}
    for row in catalog:
        locode = normalize_code(row.get("LOCODE"))
        if locode and locode not in by_locode:
            by_locode[locode] = row
    return by_portid, by_locode


def resolve_port(
    snapshot: dict,
    catalog: list[dict],
    by_portid: dict[str, dict],
    by_locode: dict[str, dict],
    manual_map: dict[str, str],
) -> dict | None:
    code = normalize_code(snapshot.get("port_code"))
    if not code:
        return None

    if code in manual_map and manual_map[code] in by_portid:
        row = by_portid[manual_map[code]]
        return make_map_row(snapshot, row, "manual", 1.0, None)

    if code in by_locode:
        row = by_locode[code]
        return make_map_row(snapshot, row, "locode", 1.0, None)

    lat = snapshot.get("lat")
    lon = snapshot.get("lon")
    best: tuple[float, dict, float | None, float] | None = None
    for row in catalog:
        dist = None
        dist_score = 0.0
        if lat is not None and lon is not None and row.get("lat") is not None and row.get("lon") is not None:
            dist = haversine_km(float(lat), float(lon), float(row["lat"]), float(row["lon"]))
            if dist > MAX_MATCH_DISTANCE_KM:
                continue
            dist_score = max(0.0, 1.0 - dist / MAX_MATCH_DISTANCE_KM)

        n_score = name_score(snapshot.get("port_name"), row.get("portname"))
        score = dist_score * 0.75 + n_score * 0.25 if dist is not None else n_score
        if best is None or score > best[0]:
            best = (score, row, dist, n_score)

    if best is None or best[0] < 0.35:
        logger.warning("No PortWatch match for %s (%s)", code, snapshot.get("port_name"))
        return None
    # 퍼지(좌표+이름) 매칭은 오매핑 위험이 있으므로 항상 눈에 띄게 경고.
    # 신뢰가 필요한 항만은 build_portwatch_map.py 로 PORTWATCH_PORT_MAP 을 고정할 것.
    logger.warning(
        "FUZZY match used for %s (%s) -> portid=%s score=%.3f dist=%s km. "
        "검수 권장: PORTWATCH_PORT_MAP 으로 고정하세요.",
        code, snapshot.get("port_name"), best[1].get("portid"), best[0],
        f"{best[2]:.1f}" if best[2] is not None else "n/a",
    )
    return make_map_row(snapshot, best[1], "geo_name", best[0], best[2])


def make_map_row(
    snapshot: dict,
    portwatch: dict,
    method: str,
    score: float,
    distance_km: float | None,
) -> dict:
    return {
        "port_code": normalize_code(snapshot.get("port_code")),
        "portwatch_portid": portwatch.get("portid"),
        "portwatch_name": portwatch.get("portname"),
        "portwatch_country": portwatch.get("country"),
        "portwatch_iso3": portwatch.get("ISO3"),
        "locode": portwatch.get("LOCODE"),
        "match_method": method,
        "match_score": round(score, 4),
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "is_active": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_daily_for_port(client: httpx.Client, port_code: str, portid: str, start_date: date) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    fields = (
        "date,portid,portcalls,portcalls_container,portcalls_dry_bulk,"
        "portcalls_general_cargo,portcalls_roro,portcalls_tanker,portcalls_cargo,"
        "import,export,import_container,export_container"
    )
    while True:
        params = {
            "where": f"portid='{portid}' AND date>='{start_date.isoformat()}'",
            "outFields": fields,
            "orderByFields": "date asc",
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
        }
        resp = client.get(DAILY_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(data["error"])

        features = data.get("features", [])
        for feature in features:
            attr = feature.get("attributes") or {}
            recorded_date = attr.get("date")
            if not recorded_date:
                continue
            rows.append({
                "port_code": port_code,
                "portwatch_portid": portid,
                "recorded_date": recorded_date,
                "portcalls": attr.get("portcalls"),
                "portcalls_container": attr.get("portcalls_container"),
                "portcalls_dry_bulk": attr.get("portcalls_dry_bulk"),
                "portcalls_general_cargo": attr.get("portcalls_general_cargo"),
                "portcalls_roro": attr.get("portcalls_roro"),
                "portcalls_tanker": attr.get("portcalls_tanker"),
                "portcalls_cargo": attr.get("portcalls_cargo"),
                "import_total": attr.get("import"),
                "export_total": attr.get("export"),
                "import_container": attr.get("import_container"),
                "export_container": attr.get("export_container"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        if not data.get("exceededTransferLimit"):
            break
        offset += PAGE_SIZE
        time.sleep(REQUEST_SLEEP_SECONDS)
    return rows


def upsert_chunks(supabase: Client, table: str, rows: list[dict], on_conflict: str) -> None:
    if not rows:
        return
    chunk_size = 500
    for start in range(0, len(rows), chunk_size):
        supabase.table(table).upsert(
            rows[start:start + chunk_size], on_conflict=on_conflict
        ).execute()


def fetch_stored_daily(supabase: Client, port_codes: list[str]) -> dict[str, list[dict]]:
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows: list[dict] = []
    start = 0
    while True:
        resp = (
            supabase.table("portwatch_port_daily")
            .select("port_code,recorded_date,portcalls,portcalls_container")
            .in_("port_code", port_codes)
            .gte("recorded_date", cutoff)
            .order("recorded_date", desc=False)
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["port_code"]].append(row)
    return grouped


def calculate_portwatch_index(rows: list[dict]) -> dict | None:
    values: list[tuple[date, float]] = []
    for row in rows:
        raw_date = row.get("recorded_date")
        metric = row.get("portcalls_container")
        if metric is None:  # 결측일 때만 total 로 폴백 (0건은 실제 0으로 유지)
            metric = row.get("portcalls")
        if raw_date is None or metric is None:
            continue
        try:
            observed = date.fromisoformat(str(raw_date)[:10])
            values.append((observed, float(metric)))
        except (TypeError, ValueError):
            continue

    if len(values) < PERIOD_DAYS:
        return None
    values.sort(key=lambda item: item[0])
    end_date = values[-1][0]
    current_start = end_date - timedelta(days=PERIOD_DAYS - 1)
    previous_start = current_start - timedelta(days=PERIOD_DAYS)
    previous_end = current_start - timedelta(days=1)
    one_year_start = end_date - timedelta(days=365)

    current_values = [v for d, v in values if current_start <= d <= end_date]
    previous_values = [v for d, v in values if previous_start <= d <= previous_end]
    one_year_values = [(d, v) for d, v in values if d >= one_year_start]
    current_avg = mean(current_values)
    previous_avg = mean(previous_values)
    if current_avg is None:
        return None

    rolling_avgs: list[float] = []
    for idx, (observed, _) in enumerate(one_year_values):
        window_start = observed - timedelta(days=PERIOD_DAYS - 1)
        window = [v for d, v in one_year_values[:idx + 1] if window_start <= d <= observed]
        if len(window) >= max(7, PERIOD_DAYS // 2):
            rolling_avgs.append(sum(window) / len(window))

    percentile = None
    if rolling_avgs:
        percentile = round(sum(1 for v in rolling_avgs if v <= current_avg) / len(rolling_avgs) * 100, 1)

    delta_pct = None
    trend_index = 50.0
    if previous_avg is not None and previous_avg > 0:
        delta_pct = (current_avg / previous_avg) - 1.0
        trend_index = clamp(50.0 + delta_pct * 100.0, 0.0, 100.0)

    historic_index = percentile if percentile is not None else trend_index
    if percentile is not None:
        historic_index = percentile * 0.70 + trend_index * 0.30

    history_days = max(1, (end_date - values[0][0]).days + 1)
    return {
        "portwatch_history_days": history_days,
        "portwatch_recent_activity": round(current_avg, 2),
        "portwatch_previous_activity": round(previous_avg, 2) if previous_avg is not None else None,
        "portwatch_activity_percentile": percentile,
        "portwatch_trend_index": round(trend_index, 1),
        "portwatch_historic_index": round(clamp(historic_index, 0.0, 100.0), 1),
        "portwatch_activity_delta_pct": round(delta_pct, 4) if delta_pct is not None else None,
        "mpci_period_start": current_start.isoformat(),
        "mpci_period_end": end_date.isoformat(),
        "mpci_previous_start": previous_start.isoformat(),
        "mpci_previous_end": previous_end.isoformat(),
    }


def update_snapshot_indices(
    supabase: Client,
    snapshots: dict[str, dict],
    map_rows: list[dict],
    daily_by_port: dict[str, list[dict]],
) -> None:
    now_str = datetime.now(timezone.utc).isoformat()
    updates = []
    by_code = {row["port_code"]: row for row in map_rows}
    for port_code, rows in daily_by_port.items():
        index = calculate_portwatch_index(rows)
        if index is None:
            logger.warning("Not enough PortWatch history for %s", port_code)
            continue
        map_row = by_code.get(port_code)
        snapshot = snapshots.get(port_code, {})
        final_mpci, confidence = combine_final_mpci(
            snapshot.get("econdb_current_index"),
            index["portwatch_historic_index"],
            snapshot.get("ais_wait_index"),
        )
        data = {
            "port_code": port_code,
            "portwatch_portid": map_row.get("portwatch_portid") if map_row else None,
            "portwatch_updated_at": now_str,
            "historic_percentile_index": index["portwatch_historic_index"],
            "trend_change_index": index["portwatch_trend_index"],
            "mpci_history_days": index["portwatch_history_days"],
            "mpci_delta_pct_prev": index["portwatch_activity_delta_pct"],
            **index,
        }
        if final_mpci is not None:
            data["final_mpci"] = final_mpci
            data["mpci_confidence"] = confidence
        updates.append(data)

    upsert_chunks(supabase, "port_snapshots", updates, "port_code")
    logger.info("Updated PortWatch indices for %s ports.", len(updates))


def main() -> None:
    logger.info("fetch_portwatch_ports.py starting...")
    supabase = get_supabase()
    snapshots_list = fetch_snapshot_ports(supabase)
    if not snapshots_list:
        logger.warning("No port_snapshots rows found; run EconDB fetch first.")
        return

    snapshots = {normalize_code(row.get("port_code")): row for row in snapshots_list}
    catalog = fetch_catalog()
    by_portid, by_locode = build_catalog_indexes(catalog)
    manual_map = parse_manual_map()

    map_rows = []
    for snapshot in snapshots_list:
        row = resolve_port(snapshot, catalog, by_portid, by_locode, manual_map)
        if row:
            map_rows.append(row)
            logger.info(
                "Mapped %s -> %s (%s, %s, score=%s, distance=%s)",
                row["port_code"], row["portwatch_portid"], row["portwatch_name"],
                row["match_method"], row["match_score"], row["distance_km"],
            )
    upsert_chunks(supabase, "portwatch_port_map", map_rows, "port_code")

    start_date = date.today() - timedelta(days=LOOKBACK_DAYS)
    daily_rows = []
    with httpx.Client(timeout=60) as client:
        for row in map_rows:
            fetched = fetch_daily_for_port(
                client, row["port_code"], row["portwatch_portid"], start_date
            )
            daily_rows.extend(fetched)
            logger.info("Fetched %s PortWatch daily rows for %s.", len(fetched), row["port_code"])
            time.sleep(REQUEST_SLEEP_SECONDS)
    upsert_chunks(supabase, "portwatch_port_daily", daily_rows, "port_code,recorded_date")
    logger.info("Upserted %s PortWatch port daily rows.", len(daily_rows))

    daily_by_port = fetch_stored_daily(supabase, [row["port_code"] for row in map_rows])
    update_snapshot_indices(supabase, snapshots, map_rows, daily_by_port)
    logger.info("fetch_portwatch_ports.py completed successfully.")


if __name__ == "__main__":
    main()
