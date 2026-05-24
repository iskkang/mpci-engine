"""
MPCI Engine — AIS WebSocket Collector
Railway에서 24/7 실행. 15분 수집 → Supabase 저장 → 반복.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import websockets
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 항만 정의 (50개)
# ─────────────────────────────────────────────
PORTS = {
  # 한국·일본
  "KRPUS": {"name": "Busan",     "country": "KR", "region": "kr-jp", "lat": 35.10, "lon": 129.04, "box": [[34.9, 128.7], [35.3, 129.3]]},
  "KRICN": {"name": "Incheon",   "country": "KR", "region": "kr-jp", "lat": 37.45, "lon": 126.55, "box": [[37.3, 126.3], [37.6, 126.8]]},
  "JPNGO": {"name": "Nagoya",    "country": "JP", "region": "kr-jp", "lat": 35.07, "lon": 136.88, "box": [[34.8, 136.5], [35.4, 137.2]]},
  "JPYOK": {"name": "Yokohama",  "country": "JP", "region": "kr-jp", "lat": 35.45, "lon": 139.65, "box": [[35.2, 139.4], [35.7, 139.9]]},
  "JPTYO": {"name": "Tokyo",     "country": "JP", "region": "kr-jp", "lat": 35.62, "lon": 139.77, "box": [[35.4, 139.5], [35.8, 140.1]]},
  # 중국·홍콩·대만
  "CNSHA": {"name": "Shanghai",  "country": "CN", "region": "china", "lat": 31.23, "lon": 121.47, "box": [[30.8, 121.0], [31.6, 122.0]]},
  "CNQIN": {"name": "Qingdao",   "country": "CN", "region": "china", "lat": 36.07, "lon": 120.38, "box": [[35.8, 120.0], [36.3, 120.7]]},
  "CNNGB": {"name": "Ningbo",    "country": "CN", "region": "china", "lat": 29.87, "lon": 121.55, "box": [[29.6, 121.2], [30.1, 122.0]]},
  "CNTXG": {"name": "Tianjin",   "country": "CN", "region": "china", "lat": 38.98, "lon": 117.72, "box": [[38.7, 117.3], [39.2, 118.1]]},
  "CNYTN": {"name": "Yantian",   "country": "CN", "region": "china", "lat": 22.57, "lon": 114.27, "box": [[22.3, 114.0], [22.8, 114.6]]},
  "CNNSA": {"name": "Nansha",    "country": "CN", "region": "china", "lat": 22.77, "lon": 113.57, "box": [[22.5, 113.2], [23.0, 113.9]]},
  "CNDLC": {"name": "Dalian",    "country": "CN", "region": "china", "lat": 38.91, "lon": 121.60, "box": [[38.6, 121.2], [39.1, 122.0]]},
  "HKHKG": {"name": "Hong Kong", "country": "HK", "region": "china", "lat": 22.31, "lon": 114.17, "box": [[22.15, 113.95], [22.50, 114.35]]},
  "CNXMN": {"name": "Xiamen",    "country": "CN", "region": "china", "lat": 24.48, "lon": 118.08, "box": [[24.20, 117.80], [24.75, 118.35]]},
  "TWKHH": {"name": "Kaohsiung", "country": "TW", "region": "china", "lat": 22.62, "lon": 120.28, "box": [[22.40, 120.10], [22.80, 120.45]]},
  # 동남아
  "SGSIN": {"name": "Singapore",       "country": "SG", "region": "sea", "lat": 1.26,  "lon": 103.82, "box": [[0.9, 103.5],   [1.6, 104.2]]},
  "MYTPP": {"name": "Tanjung Pelepas", "country": "MY", "region": "sea", "lat": 1.36,  "lon": 103.53, "box": [[1.15, 103.35],[1.55, 103.70]]},
  "MYLPK": {"name": "Port Klang",      "country": "MY", "region": "sea", "lat": 3.00,  "lon": 101.38, "box": [[2.7, 101.0],   [3.3, 101.7]]},
  "THLCH": {"name": "Laem Chabang",    "country": "TH", "region": "sea", "lat": 13.08, "lon": 100.88, "box": [[12.8, 100.6],  [13.4, 101.2]]},
  "VNTOT": {"name": "Cai Mep",         "country": "VN", "region": "sea", "lat": 10.52, "lon": 107.03, "box": [[10.2, 106.7],  [10.8, 107.4]]},
  "VNHPH": {"name": "Haiphong",        "country": "VN", "region": "sea", "lat": 20.87, "lon": 106.68, "box": [[20.6, 106.4],  [21.1, 107.0]]},
  "IDJKT": {"name": "Jakarta",         "country": "ID", "region": "sea", "lat": -6.10, "lon": 106.88, "box": [[-6.4, 106.5],  [-5.7, 107.2]]},
  "PHMNL": {"name": "Manila",          "country": "PH", "region": "sea", "lat": 14.59, "lon": 120.97, "box": [[14.2, 120.6],  [14.9, 121.3]]},
  # 남아시아·중동
  "AEJEA": {"name": "Jebel Ali",       "country": "AE", "region": "sa-me", "lat": 24.98, "lon": 55.06, "box": [[24.6, 54.7],  [25.3, 55.4]]},
  "LKCMB": {"name": "Colombo",         "country": "LK", "region": "sa-me", "lat": 6.93,  "lon": 79.85, "box": [[6.6, 79.5],   [7.2, 80.2]]},
  "INNSA": {"name": "Nhava Sheva",     "country": "IN", "region": "sa-me", "lat": 18.95, "lon": 72.95, "box": [[18.75, 72.75],[19.15, 73.15]]},
  "INMUN": {"name": "Mundra",          "country": "IN", "region": "sa-me", "lat": 22.74, "lon": 69.70, "box": [[22.45, 69.40],[23.00, 69.95]]},
  "OMSLL": {"name": "Salalah",         "country": "OM", "region": "sa-me", "lat": 16.95, "lon": 54.01, "box": [[16.75, 53.80],[17.15, 54.25]]},
  "DJJIB": {"name": "Djibouti",        "country": "DJ", "region": "sa-me", "lat": 11.59, "lon": 43.14, "box": [[11.4, 42.9],  [11.8, 43.4]]},
  "SADMM": {"name": "Dammam",          "country": "SA", "region": "sa-me", "lat": 26.43, "lon": 50.10, "box": [[26.15, 49.85],[26.70, 50.35]]},
  "JOAQJ": {"name": "Aqaba",           "country": "JO", "region": "sa-me", "lat": 29.52, "lon": 35.00, "box": [[29.2, 34.7],  [29.8, 35.3]]},
  # 유럽
  "NLRTM": {"name": "Rotterdam",   "country": "NL", "region": "europe", "lat": 51.92, "lon":  4.48, "box": [[51.6, 4.0],  [52.2, 5.0]]},
  "DEHAM": {"name": "Hamburg",     "country": "DE", "region": "europe", "lat": 53.55, "lon":  9.99, "box": [[53.3, 9.5],  [53.8, 10.4]]},
  "BEANR": {"name": "Antwerp",     "country": "BE", "region": "europe", "lat": 51.22, "lon":  4.40, "box": [[50.9, 4.0],  [51.5, 4.8]]},
  "GBFXT": {"name": "Felixstowe",  "country": "GB", "region": "europe", "lat": 51.95, "lon":  1.33, "box": [[51.7, 1.0],  [52.2, 1.7]]},
  "FRLEH": {"name": "Le Havre",    "country": "FR", "region": "europe", "lat": 49.49, "lon":  0.11, "box": [[49.2, -0.2], [49.8, 0.5]]},
  "GRPIR": {"name": "Piraeus",     "country": "GR", "region": "europe", "lat": 37.94, "lon": 23.64, "box": [[37.7, 23.3], [38.2, 24.0]]},
  "ESVLC": {"name": "Valencia",    "country": "ES", "region": "europe", "lat": 39.46, "lon": -0.31, "box": [[39.1, -0.6], [39.8, 0.0]]},
  "ESALG": {"name": "Algeciras",   "country": "ES", "region": "europe", "lat": 36.13, "lon": -5.45, "box": [[35.9, -5.7], [36.4, -5.1]]},
  "ITGOA": {"name": "Genoa",       "country": "IT", "region": "europe", "lat": 44.41, "lon":  8.93, "box": [[44.1, 8.6],  [44.7, 9.3]]},
  # 북미
  "USLAX": {"name": "Los Angeles", "country": "US", "region": "namerica", "lat": 33.73, "lon": -118.25, "box": [[33.4, -118.6],[34.0, -117.9]]},
  "USLGB": {"name": "Long Beach",  "country": "US", "region": "namerica", "lat": 33.77, "lon": -118.22, "box": [[33.5, -118.5],[34.0, -117.9]]},
  "USNYC": {"name": "New York",    "country": "US", "region": "namerica", "lat": 40.67, "lon":  -74.01, "box": [[40.3, -74.4], [40.9, -73.6]]},
  "USSAV": {"name": "Savannah",    "country": "US", "region": "namerica", "lat": 32.08, "lon":  -81.09, "box": [[31.8, -81.4], [32.4, -80.8]]},
  "CAVAN": {"name": "Vancouver",   "country": "CA", "region": "namerica", "lat": 49.29, "lon": -123.11, "box": [[49.0, -123.5],[49.6, -122.7]]},
  # 중남미
  "BRSSZ": {"name": "Santos",      "country": "BR", "region": "latam", "lat": -23.95, "lon":  -46.33, "box": [[-24.20, -46.60],[-23.70, -46.00]]},
  "COCTG": {"name": "Cartagena",   "country": "CO", "region": "latam", "lat":  10.39, "lon":  -75.53, "box": [[10.15, -75.75], [10.60, -75.30]]},
  "PABLB": {"name": "Balboa",      "country": "PA", "region": "latam", "lat":   8.95, "lon":  -79.57, "box": [[8.75, -79.75],  [9.10, -79.40]]},
  # 러시아
  "RUVVO": {"name": "Vladivostok", "country": "RU", "region": "ru-cis", "lat": 43.12, "lon": 131.89, "box": [[42.8, 131.5],[43.5, 132.3]]},
  # 아프리카
  "MATNG": {"name": "Tanger Med",  "country": "MA", "region": "africa", "lat": 35.89, "lon":  -5.50, "box": [[35.70, -5.75],[36.05, -5.25]]},
  "NGAPP": {"name": "Lagos",       "country": "NG", "region": "africa", "lat":  6.45, "lon":   3.38, "box": [[6.25, 3.15],  [6.65, 3.60]]},
  "ZADUR": {"name": "Durban",      "country": "ZA", "region": "africa", "lat": -29.87, "lon":  31.03, "box": [[-30.2, 30.7],[-29.5, 31.4]]},
  "EGPSD": {"name": "Port Said",   "country": "EG", "region": "africa", "lat": 31.25, "lon":  32.30, "box": [[30.9, 31.9],  [31.5, 32.7]]},
}

COLLECT_SECONDS = 900   # 15분 수집 윈도우
FRESH_CUTOFF    = 300   # 5분 이상 미보고 선박 제외
AIS_WS_URL      = "wss://stream.aisstream.io/v0/stream"

# TPFS 가중치 및 레벨 임계값
ANCHORED_WEIGHT = 6.0
BERTHED_WEIGHT  = 2.0
LEVEL_CONGESTED = 75
LEVEL_BUSY      = 50
LEVEL_STABLE    = 25


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def find_port(lat: float, lon: float) -> str | None:
    """bounding box 포함 여부로 항만 코드 반환"""
    for code, info in PORTS.items():
        min_lat, min_lon = info["box"][0]
        max_lat, max_lon = info["box"][1]
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return code
    return None


def classify_status(nav_status: int, sog: float) -> str | None:
    """
    nav_status == 1 또는 SOG < 1.0  → 'anchored'
    nav_status == 5 또는 SOG < 0.3  → 'berthed'
    그 외 → None (무시)
    """
    if nav_status == 1 or sog < 1.0:
        return "anchored"
    if nav_status == 5 or sog < 0.3:
        return "berthed"
    return None


def compute_level(tpfs: float) -> str:
    if tpfs >= LEVEL_CONGESTED:
        return "CONGESTED"
    if tpfs >= LEVEL_BUSY:
        return "BUSY"
    if tpfs >= LEVEL_STABLE:
        return "STABLE"
    return "LOW"


def build_rows(vessel_snapshot: dict, now: float) -> list[dict]:
    """
    vessel_snapshot 을 항만별로 집계하여 upsert할 행 리스트 반환.
    fresh_cutoff 이전에 마지막으로 관찰된 선박은 제외.
    """
    fresh_cutoff = now - FRESH_CUTOFF
    port_counts: dict[str, dict] = {}

    for key, info in vessel_snapshot.items():
        if info["last_seen"] < fresh_cutoff:
            continue
        port_code = info["port_code"]
        status    = info["status"]
        if port_code not in port_counts:
            port_counts[port_code] = {"anchored": 0, "berthed": 0}
        port_counts[port_code][status] += 1

    rows = []
    ts = datetime.now(timezone.utc).isoformat()
    for port_code, counts in port_counts.items():
        p = PORTS[port_code]
        anchored = counts["anchored"]
        berthed  = counts["berthed"]
        tpfs     = min(100.0, anchored * ANCHORED_WEIGHT + berthed * BERTHED_WEIGHT)
        level    = compute_level(tpfs)
        rows.append({
            "port_code":        port_code,
            "port_name":        p["name"],
            "country":          p["country"],
            "region":           p["region"],
            "lat":              p["lat"],
            "lon":              p["lon"],
            "updated_at":       ts,
            "vessels_anchored": anchored,
            "vessels_berthed":  berthed,
            "tpfs":             tpfs,
            "level":            level,
        })
    return rows


def save_to_supabase(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        logger.info("No rows to save.")
        return

    # port_snapshots: upsert on port_code
    upsert_data = rows
    supabase.table("port_snapshots").upsert(
        upsert_data, on_conflict="port_code"
    ).execute()
    logger.info(f"Upserted {len(upsert_data)} rows into port_snapshots")

    # port_history: insert (시계열 보존 — upsert 사용 금지)
    history_data = [
        {
            "port_code":        r["port_code"],
            "snapshot_at":      r["updated_at"],
            "vessels_anchored": r["vessels_anchored"],
            "vessels_berthed":  r["vessels_berthed"],
            "tpfs":             r["tpfs"],
            "level":            r["level"],
        }
        for r in rows
    ]
    supabase.table("port_history").insert(history_data).execute()
    logger.info(f"Inserted {len(history_data)} rows into port_history")


async def collect_cycle(supabase: Client) -> None:
    """15분 동안 AIS 수집 후 저장하는 단일 사이클"""
    api_key = os.environ["AISSTREAM_API_KEY"]
    vessel_snapshot: dict[str, dict] = {}
    end_time = time.monotonic() + COLLECT_SECONDS

    # AIS 구독 메시지 — 모든 선박 유형, 전 세계
    subscribe_msg = json.dumps({
        "APIKey":         api_key,
        "MessageType":    "Subscribe",
        "BoundingBoxes":  [[[-90, -180], [90, 180]]],
        "FilterMessageTypes": ["PositionReport"],
    })

    logger.info("Starting AIS collection cycle (900s)...")
    try:
        async with websockets.connect(AIS_WS_URL, ping_interval=20) as ws:
            await ws.send(subscribe_msg)
            while time.monotonic() < end_time:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.warning(f"WS recv error: {e}")
                    break

                if msg.get("MessageType") != "PositionReport":
                    continue

                meta  = msg.get("MetaData", {})
                pr    = msg.get("Message", {}).get("PositionReport", {})
                mmsi  = str(meta.get("MMSI", ""))
                lat   = meta.get("latitude",  pr.get("Latitude",  None))
                lon   = meta.get("longitude", pr.get("Longitude", None))
                sog   = float(pr.get("Sog", 99))
                nav   = int(pr.get("NavigationalStatus", 0))

                if lat is None or lon is None or not mmsi:
                    continue

                port_code = find_port(lat, lon)
                if port_code is None:
                    continue

                status = classify_status(nav, sog)
                if status is None:
                    continue

                key = f"{mmsi}_{port_code}"
                vessel_snapshot[key] = {
                    "port_code": port_code,
                    "status":    status,
                    "last_seen": time.monotonic(),
                }
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")

    now  = time.monotonic()
    rows = build_rows(vessel_snapshot, now)
    logger.info(f"Collection done. Ports observed: {len(rows)}, vessels tracked: {len(vessel_snapshot)}")
    save_to_supabase(supabase, rows)


async def main() -> None:
    supabase = get_supabase()
    while True:
        try:
            await collect_cycle(supabase)
        except Exception as e:
            logger.error(f"Cycle error: {e}")
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
