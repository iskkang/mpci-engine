"""
scripts/fetch_econdb.py

EconDB 전체 수집 파이프라인

사용법:
  python scripts/fetch_econdb.py --job regions     # [A] 지역별 locode 수집 (주 1회)
  python scripts/fetch_econdb.py --job ports       # [B] per-port 3개 endpoint (매 시간, 100개씩)
  python scripts/fetch_econdb.py --job snapshots   # [C] Top 20 스냅샷 (매일 1회)
  python scripts/fetch_econdb.py --job cleanup     # [D] 90일 이전 timeseries 삭제 (주 1회)

구조:
  [A] congestion_region_ports × 18지역 → econdb_ports 테이블
  [B] per-locode × 3 endpoint           → econdb_port_timeseries 테이블
  [C] search/ports Top 20 (2종 sort)    → econdb_port_snapshots 테이블
  [D] 오래된 timeseries 정리

가드레일:
  - 응답 스키마 불일치 → 로그+스킵, 값 날조 금지
  - 429 수신 시 즉시 중단 (재시도 없음)
  - per-port 배치: 100개씩, 24시간 이상 된 것만 재수집
"""

import argparse
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── 엔드포인트 상수 ───────────────────────────────────────────────────────────

REGION_PORTS_URL = "https://www.econdb.com/maritime/congestion_region_ports/"
CONTAINERS_URL   = "https://www.econdb.com/widgets/containers-in-terminal/data/"
OMISSIONS_URL    = "https://www.econdb.com/widgets/omissions-time-series/data/"
SCHEDULE_URL     = "https://www.econdb.com/widgets/weekly-schedule-profile/data/"
SEARCH_URL       = "https://www.econdb.com/maritime/search/ports/"

SEARCH_FL = (
    "rank,name,locode,id,schedule,port_congestion,delay_percent,"
    "import_dwell_time,export_dwell_time,ts_dwell_time,"
    "last_import_teu,last_export_teu,turnaround,region,vessels_berthed"
)

REGIONS = [
    "East Asia", "Middle East", "Indian Subcontinent",
    "Mediterranean", "East Med", "West Med", "Northwest Europe",
    "Baltic", "West Africa", "East Africa", "South Africa",
    "North America East", "North America West", "US GOM",
    "East South America", "West South America", "Central America", "Oceania",
]

# congestion_region_ports 응답에 lat/lon 없을 경우 정적 폴백 (공백 포함 locode)
COORD_FALLBACK: dict[str, tuple[float, float]] = {
    "SG SIN": (1.264, 103.820),  "CN SHA": (31.234, 121.476), "KR PUS": (35.099, 129.043),
    "CN NGB": (29.867, 121.544), "MY TPP": (1.363, 103.549),  "CN TAO": (36.067, 120.387),
    "MA PTM": (35.888, -5.510),  "BE ANR": (51.221, 4.404),   "NL RTM": (51.950, 4.140),
    "TW KHH": (22.617, 120.297), "CN YTN": (22.577, 114.267), "MY PKG": (2.979, 101.397),
    "AE JEA": (25.007, 55.077),  "LK CMB": (6.933, 79.843),   "TH LCH": (13.082, 100.882),
    "IN MUN": (22.838, 69.703),  "CN SHK": (22.480, 113.900), "US LAX": (33.736, -118.261),
    "IN NSA": (18.914, 72.954),  "HK HKG": (22.289, 114.158), "CN TXG": (38.867, 117.720),
    "US NYC": (40.693, -74.139), "EG PSD": (31.259, 32.284),  "VN VUT": (10.346, 107.084),
    "US LGB": (33.754, -118.214),"CN XMN": (24.451, 118.068), "PA ONX": (9.362, -79.862),
    "ES VLC": (39.452, -0.327),  "DE BRV": (53.544, 8.575),   "US SAV": (32.082, -81.096),
    "BR SSZ": (-23.958, -46.305),"IT GIT": (38.428, 15.900),  "VN HPH": (20.866, 106.684),
    "CO CTG": (10.394, -75.524), "US HOU": (29.745, -95.268), "GR PIR": (37.941, 23.627),
    "SA JED": (21.485, 39.173),  "FR LEH": (49.490, 0.107),   "ES BCN": (41.332, 2.167),
    "CA VAN": (49.295, -123.111),"ZA DUR": (-29.879, 31.026), "GB LGP": (51.497, 0.491),
    "CN DLC": (38.900, 121.653), "JP YOK": (35.454, 139.640), "US OAK": (37.796, -122.276),
    "DE HAM": (53.545, 9.960),   "GH TEM": (5.636, -0.016),   "KR INC": (37.458, 126.705),
    "ES ALG": (36.129, -5.446),  "AU MEL": (-37.818, 144.867),"US MIA": (25.774, -80.178),
    "CI ABJ": (5.354, -4.016),   "GB FXT": (51.963, 1.352),   "ID SUB": (-7.241, 112.736),
    "ID JKT": (-6.121, 106.843), "IN MAA": (13.074, 80.287),  "VN SGN": (10.778, 106.699),
    "PH MNL": (14.580, 120.966), "JP TYO": (35.621, 139.760), "CN NSA": (22.728, 113.628),
    "IT GOA": (44.406, 8.934),   "EG ALY": (31.197, 29.892),  "PK KHI": (24.861, 66.990),
    "KE MBA": (-4.057, 39.668),  "MA CAS": (33.615, -7.615),  "TR AMR": (40.983, 28.683),
    "OM SLL": (17.020, 54.090),  "JP NGO": (35.064, 136.881), "CN SHN": (22.480, 113.900),
    "CN QIN": (36.067, 120.387), "KR KAN": (34.905, 127.692), "TW TPE": (25.091, 121.567),
    "US SAV": (32.082, -81.096), "MX ZLO": (19.055, -104.321),"US CHS": (32.779, -79.944),
    "PL GDN": (54.353, 18.683),  "US ORF": (36.848, -76.299), "US BAL": (39.268, -76.614),
}

# ── Supabase 클라이언트 ───────────────────────────────────────────────────────

def get_sb() -> Client:
    url = os.environ["SUPABASE_URL"]
    # SUPABASE_SERVICE_KEY (기존 시크릿명) 또는 SUPABASE_SERVICE_ROLE_KEY 지원
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


# ── [A] 지역별 locode 수집 ────────────────────────────────────────────────────

def job_regions(sb: Client) -> None:
    """congestion_region_ports × 18지역 → econdb_ports 저장."""
    total = 0
    for region in REGIONS:
        try:
            resp = httpx.get(REGION_PORTS_URL, params={"region": region}, timeout=20)
            resp.raise_for_status()
            raw = resp.json()

            # 응답이 list면 그대로, dict면 내부 배열 추출
            ports = raw if isinstance(raw, list) else (
                raw.get("ports") or raw.get("results") or raw.get("data") or []
            )

            rows = []
            for p in ports:
                locode = (p.get("locode") or p.get("port_code") or "").strip()
                if not locode:
                    continue
                lat = p.get("lat") or p.get("latitude")
                lon = p.get("lon") or p.get("longitude")
                if not lat:
                    lat, lon = COORD_FALLBACK.get(locode, (None, None))

                rows.append({
                    "locode":    locode,
                    "port_name": (p.get("name") or p.get("port_name") or locode).strip(),
                    "region":    region,
                    "lat":       float(lat) if lat is not None else None,
                    "lon":       float(lon) if lon is not None else None,
                })

            if rows:
                sb.table("econdb_ports").upsert(
                    rows, on_conflict="locode"
                ).execute()
                total += len(rows)
                logger.info("[regions] %s: %d개", region, len(rows))

        except Exception as e:
            logger.warning("[regions] %s 실패: %s", region, e)
        time.sleep(0.5)

    logger.info("[regions] 완료: 총 %d개 항만", total)


# ── [B] per-port 3개 endpoint 수집 ───────────────────────────────────────────

def _parse_timeseries(resp_json: dict, series_type: str) -> list[dict]:
    """containers / schedule / omissions 응답 → 통일된 rows 변환."""
    plots = resp_json.get("plots", [])
    if not plots:
        return []
    data = plots[0].get("data", [])
    rows = []
    for pt in data:
        ts = pt.get("Date")
        if not ts:
            continue
        if series_type in ("containers", "schedule"):
            va = pt.get("TEU") or pt.get("teu")
            vb = pt.get("TEU_Last_Year") or pt.get("teu_last_year")
        else:  # omissions
            # 응답 필드명은 API 버전마다 다를 수 있음 — 가능한 후보 모두 시도
            va = (pt.get("Blanked capacity") or pt.get("blanked_teu")
                  or pt.get("BlankSailed") or pt.get("blank_sailed"))
            vb = (pt.get("Actual capacity") or pt.get("actual_teu")
                  or pt.get("Actual") or pt.get("actual"))
        rows.append({"ts": ts, "value_a": va, "value_b": vb})
    return rows


def _fetch_one_port(sb: Client, locode: str) -> None:
    """locode 하나에 대해 3개 endpoint 순차 호출 → timeseries 저장."""
    endpoints = [
        ("containers", CONTAINERS_URL, {"locode": locode}),
        ("omissions",  OMISSIONS_URL,  {"locode": locode}),
        ("schedule",   SCHEDULE_URL,   {"locode": locode, "metric": "TEU", "size": "ALL"}),
    ]
    now = datetime.now(timezone.utc).isoformat()

    for series_type, url, params in endpoints:
        try:
            resp = httpx.get(url, params=params, timeout=20)

            if resp.status_code == 429:
                logger.error("429 Too Many Requests — 수집 중단")
                raise SystemExit(1)

            resp.raise_for_status()
            pts = _parse_timeseries(resp.json(), series_type)
            if not pts:
                logger.info("  [%s] %s: 데이터 없음", series_type, locode)
                continue

            rows = [
                {
                    "locode":      locode,
                    "series_type": series_type,
                    "ts":          pt["ts"],
                    "value_a":     pt["value_a"],
                    "value_b":     pt["value_b"],
                    "fetched_at":  now,
                }
                for pt in pts
            ]
            sb.table("econdb_port_timeseries").upsert(
                rows, on_conflict="locode,series_type,ts"
            ).execute()
            logger.info("  [%s] %s: %d pts", series_type, locode, len(rows))

        except SystemExit:
            raise
        except Exception as e:
            logger.warning("  [%s] %s 실패: %s", series_type, locode, e)
        time.sleep(0.5)

    # 수집 완료 시각 기록
    try:
        sb.table("econdb_ports").update({"last_collected_at": now})\
          .eq("locode", locode).execute()
    except Exception as e:
        logger.warning("[ports] last_collected_at 업데이트 실패 (%s): %s", locode, e)


def job_ports(sb: Client, batch_size: int = 100) -> None:
    """아직 수집 안 했거나 24시간 이상 지난 locode 최대 100개 처리.
    GitHub Actions 매 시간 실행 → 하루 2,400개 처리 가능.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    result = sb.table("econdb_ports")\
        .select("locode")\
        .or_(f"last_collected_at.is.null,last_collected_at.lt.{cutoff}")\
        .limit(batch_size)\
        .execute()

    locodes = [r["locode"] for r in (result.data or [])]
    logger.info("[ports] 처리 대상: %d개", len(locodes))

    for locode in locodes:
        logger.info("[ports] 수집 중: %s", locode)
        _fetch_one_port(sb, locode)

    logger.info("[ports] 완료: %d개", len(locodes))


# ── [C] Top 20 스냅샷 ─────────────────────────────────────────────────────────

def job_snapshots(sb: Client) -> None:
    """search/ports × 2종류(congestion, volume) → econdb_port_snapshots."""
    today = date.today().isoformat()

    sorts = [
        ("congestion", "port_congestion desc"),
        ("volume",     "global_trade desc"),
    ]

    for sort_type, sort_by in sorts:
        try:
            resp = httpx.get(SEARCH_URL, params={
                "page_size": 20, "page": 1, "s": "",
                "sort_by": sort_by, "fl": SEARCH_FL,
            }, timeout=20)
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", [])

            rows = []
            for i, doc in enumerate(docs):
                locode = (doc.get("locode") or "").strip()
                if not locode:
                    continue

                # econdb_ports 마스터에도 없으면 추가
                try:
                    sb.table("econdb_ports").upsert(
                        {
                            "locode":    locode,
                            "port_name": (doc.get("name") or locode).strip(),
                            "region":    doc.get("region") or "",
                        },
                        on_conflict="locode"
                    ).execute()
                except Exception:
                    pass

                vb = doc.get("vessels_berthed")
                rows.append({
                    "locode":          locode,
                    "snapshot_date":   today,
                    "sort_type":       sort_type,
                    "page_rank":       i + 1,
                    "econdb_rank":     doc.get("rank"),
                    "schedule":        doc.get("schedule"),
                    "delay_percent":   doc.get("delay_percent"),
                    "port_congestion": doc.get("port_congestion"),
                    "import_dwell":    doc.get("import_dwell_time"),
                    "export_dwell":    doc.get("export_dwell_time"),
                    "ts_dwell":        doc.get("ts_dwell_time"),
                    "last_import_teu": doc.get("last_import_teu"),
                    "last_export_teu": doc.get("last_export_teu"),
                    "turnaround":      doc.get("turnaround"),
                    "vessels_berthed": int(vb) if vb is not None else None,
                    "raw_json":        json.dumps(doc),
                })

            if rows:
                sb.table("econdb_port_snapshots").upsert(
                    rows, on_conflict="locode,snapshot_date,sort_type"
                ).execute()
            logger.info("[snapshots] %s: %d개", sort_type, len(rows))

        except Exception as e:
            logger.error("[snapshots] %s 실패: %s", sort_type, e)
        time.sleep(1)


# ── [D] 90일 이전 정리 ───────────────────────────────────────────────────────

def job_cleanup(sb: Client) -> None:
    """90일 이전 timeseries 행 삭제."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    try:
        sb.table("econdb_port_timeseries").delete()\
          .lt("ts", cutoff).execute()
        logger.info("[cleanup] %s 이전 timeseries 삭제 완료", cutoff)
    except Exception as e:
        logger.error("[cleanup] 실패: %s", e)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="EconDB 수집 파이프라인")
    parser.add_argument(
        "--job",
        choices=["regions", "ports", "snapshots", "cleanup"],
        required=True,
        help="실행할 job: regions | ports | snapshots | cleanup",
    )
    args = parser.parse_args()

    sb = get_sb()

    if args.job == "regions":
        job_regions(sb)
    elif args.job == "ports":
        job_ports(sb)
    elif args.job == "snapshots":
        job_snapshots(sb)
    elif args.job == "cleanup":
        job_cleanup(sb)


if __name__ == "__main__":
    main()
