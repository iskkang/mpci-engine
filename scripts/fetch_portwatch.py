"""
MPCI Engine — PortWatch 초크포인트 일별 수집
GitHub Actions: 매일 KST 09:00 (UTC 00:00) 실행
"""

import logging
import os
import sys
from datetime import date, timedelta, timezone, datetime

import httpx
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
    "/Daily_Chokepoints_Data/FeatureServer/0/query"
)


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def fetch_chokepoints(target_date: date) -> list[dict]:
    """PortWatch ArcGIS API에서 특정 날짜의 초크포인트 데이터를 페이지네이션으로 수집"""
    date_str = target_date.strftime("%Y-%m-%d")
    records: list[dict] = []
    offset = 0
    page_size = 1000

    while True:
        params = {
            "where":              f"date='{date_str}'",
            "outFields":          "portid,portname,date,n_container,n_total,capacity_container",
            "f":                  "json",
            "resultRecordCount":  page_size,
            "resultOffset":       offset,
        }
        try:
            resp = httpx.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"API fetch error at offset {offset}: {e}")
            break

        features = data.get("features", [])
        for feat in features:
            attr = feat.get("attributes", {})
            records.append({
                "portid":              attr.get("portid", ""),
                "portname":            attr.get("portname", ""),
                "recorded_date":       date_str,
                "n_container":         int(attr.get("n_container") or 0),
                "n_total":             int(attr.get("n_total") or 0),
                "capacity_container":  int(attr.get("capacity_container") or 0),
            })

        exceeded = data.get("exceededTransferLimit", False)
        if exceeded:
            offset += page_size
            logger.info(f"  Paginating: fetched {len(records)} so far...")
        else:
            break

    logger.info(f"Fetched {len(records)} chokepoint records for {date_str}")
    return records


def upsert_records(supabase: Client, records: list[dict]) -> None:
    """chokepoint_daily에 upsert (unique: portid + recorded_date)"""
    if not records:
        logger.info("No records to upsert.")
        return
    supabase.table("chokepoint_daily").upsert(
        records, on_conflict="portid,recorded_date"
    ).execute()
    logger.info(f"Upserted {len(records)} chokepoint records.")


def detect_anomalies(supabase: Client, records: list[dict], target_date: date) -> None:
    """
    각 portid의 최근 30일 평균과 비교해 이상 감지.
    - ratio < 0.4: HIGH
    - ratio < 0.6: MEDIUM
    - 오늘 같은 portid 미확인 플래그 있으면 스킵
    """
    today_str = target_date.strftime("%Y-%m-%d")
    thirty_ago = (target_date - timedelta(days=30)).strftime("%Y-%m-%d")

    for rec in records:
        portid = rec["portid"]
        today_n = rec["n_container"]

        # 30일 이력 조회
        history_resp = (
            supabase.table("chokepoint_daily")
            .select("n_container")
            .eq("portid", portid)
            .gte("recorded_date", thirty_ago)
            .lt("recorded_date", today_str)
            .execute()
        )
        history = history_resp.data or []
        if len(history) < 14:
            # 데이터 부족, 스킵
            continue

        avg_n = sum(r["n_container"] for r in history) / len(history)
        if avg_n == 0:
            continue

        ratio = today_n / avg_n

        if ratio >= 0.6:
            # 정상
            continue

        severity = "HIGH" if ratio < 0.4 else "MEDIUM"

        # 오늘 해당 portid 미확인 플래그가 이미 있으면 중복 생성 안 함
        existing_resp = (
            supabase.table("anomaly_flags")
            .select("id")
            .eq("target_code", portid)
            .eq("source", "PORTWATCH")
            .eq("acknowledged", False)
            .gte("detected_at", today_str + "T00:00:00+00:00")
            .execute()
        )
        if existing_resp.data:
            logger.info(f"  Skipping duplicate anomaly flag for {portid}")
            continue

        flag = {
            "source":      "PORTWATCH",
            "target_code": portid,
            "target_name": rec["portname"],
            "severity":    severity,
            "ratio":       round(ratio, 4),
            "detail": (
                f"Container throughput dropped to {ratio:.1%} of 30-day avg "
                f"({today_n} vs avg {avg_n:.0f})"
            ),
        }
        supabase.table("anomaly_flags").insert(flag).execute()
        logger.warning(
            f"ANOMALY [{severity}] {portid} ({rec['portname']}): "
            f"ratio={ratio:.2f}, n_container={today_n}, avg={avg_n:.0f}"
        )


def main() -> None:
    # 어제 날짜 (KST 기준 실행이므로 UTC 어제 = KST 오늘 오전 9시 기준 어제)
    target_date = date.today() - timedelta(days=1)
    logger.info(f"Fetching PortWatch data for: {target_date}")

    supabase = get_supabase()
    records = fetch_chokepoints(target_date)
    upsert_records(supabase, records)
    detect_anomalies(supabase, records, target_date)
    logger.info("fetch_portwatch.py completed successfully.")


if __name__ == "__main__":
    main()
