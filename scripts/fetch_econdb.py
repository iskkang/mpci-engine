"""
MPCI Engine — EconDB 항만 지표 4시간 수집
GitHub Actions: 매 4시간 실행

주의: EconDB를 1시간보다 짧은 주기로 호출하지 않는다.
"""

import logging
import os
import random
import time
from datetime import datetime, timezone

import httpx
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ECONDB_URL = (
    "https://www.econdb.com/maritime/search/ports/"
    "?ab=-90,-180,90,180&center=0,0"
)
HEADERS = {"User-Agent": "MTLLink-MPCI-Monitor/1.0"}

# GitHub Actions 정각 회피: 0~60초 랜덤 대기
JITTER_MAX_SECONDS = 60


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def fetch_econdb_ports() -> list[dict]:
    """EconDB API에서 전체 포트 데이터 fetch"""
    try:
        resp = httpx.get(ECONDB_URL, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"EconDB fetch failed: {e}")
        return []

    # 응답 구조: {"ports": [...]} 또는 직접 리스트
    if isinstance(data, list):
        ports = data
    elif isinstance(data, dict):
        ports = data.get("ports", data.get("results", data.get("data", [])))
    else:
        logger.error(f"Unexpected EconDB response type: {type(data)}")
        return []

    logger.info(f"Fetched {len(ports)} ports from EconDB")
    return ports


def update_supabase(supabase: Client, ports: list[dict]) -> None:
    """
    port_snapshots 테이블에서 LOCODE가 일치하는 행의 EconDB 컬럼 업데이트.
    컬럼: econdb_congestion, econdb_delay_pct, econdb_turnaround, econdb_updated_at
    """
    now_str = datetime.now(timezone.utc).isoformat()
    updated = 0

    for port in ports:
        locode = (
            port.get("locode") or
            port.get("LOCODE") or
            port.get("port_locode") or
            port.get("un_locode", "")
        )
        if not locode:
            continue

        locode = locode.strip().upper()

        congestion  = port.get("port_congestion")
        delay_pct   = port.get("delay_percent")
        turnaround  = port.get("turnaround")

        # 값이 하나도 없으면 스킵
        if congestion is None and delay_pct is None and turnaround is None:
            continue

        update_data: dict = {"econdb_updated_at": now_str}
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

        try:
            supabase.table("port_snapshots").update(update_data).eq(
                "port_code", locode
            ).execute()
            updated += 1
        except Exception as e:
            logger.warning(f"Failed to update {locode}: {e}")

    logger.info(f"Updated EconDB data for {updated} ports.")


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
