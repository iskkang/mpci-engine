"""
MPCI Engine — 이상 감지 + 7일 기준선 갱신
GitHub Actions: 매 2시간 실행
"""

import logging
import os
from datetime import datetime, timedelta, timezone, date

from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

ANOMALY_RATIO_HIGH   = 2.5  # 기준선 대비 250% 이상 = HIGH
ANOMALY_RATIO_MEDIUM = 1.8  # 기준선 대비 180% 이상 = MEDIUM
MIN_SAMPLE_COUNT     = 5    # 기준선 최소 샘플 수
HISTORY_RETAIN_DAYS  = 90   # port_history 보관 기간


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def update_baselines(supabase: Client) -> dict[str, dict]:
    """
    port_history에서 최근 7일 데이터를 가져와 기준선 갱신.
    반환: {port_code: {avg_anchored_7d, avg_berthed_7d, avg_tpfs_7d, sample_count}}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    resp = (
        supabase.table("port_history")
        .select("port_code,vessels_anchored,vessels_berthed,tpfs")
        .gte("snapshot_at", cutoff)
        .execute()
    )
    rows = resp.data or []

    # 항만별 집계
    buckets: dict[str, list] = {}
    for r in rows:
        pc = r["port_code"]
        buckets.setdefault(pc, []).append(r)

    baselines: dict[str, dict] = {}
    now_str = datetime.now(timezone.utc).isoformat()
    upsert_rows = []

    for port_code, samples in buckets.items():
        n = len(samples)
        avg_a = sum(s["vessels_anchored"] for s in samples) / n
        avg_b = sum(s["vessels_berthed"]  for s in samples) / n
        avg_t = sum(s["tpfs"]            for s in samples) / n

        baselines[port_code] = {
            "avg_anchored_7d": avg_a,
            "avg_berthed_7d":  avg_b,
            "avg_tpfs_7d":     avg_t,
            "sample_count":    n,
        }
        upsert_rows.append({
            "port_code":       port_code,
            "updated_at":      now_str,
            "avg_anchored_7d": round(avg_a, 2),
            "avg_berthed_7d":  round(avg_b, 2),
            "avg_tpfs_7d":     round(avg_t, 2),
            "sample_count":    n,
        })

    if upsert_rows:
        supabase.table("port_baselines").upsert(
            upsert_rows, on_conflict="port_code"
        ).execute()
        logger.info(f"Updated baselines for {len(upsert_rows)} ports.")

    return baselines


def detect_ais_anomalies(supabase: Client, baselines: dict[str, dict]) -> None:
    """
    최근 2시간 평균 anchored를 기준선과 비교해 이상 감지.
    - avg_anchored_2h > baseline * ANOMALY_RATIO_HIGH → HIGH
    - avg_anchored_2h > baseline * ANOMALY_RATIO_MEDIUM → MEDIUM
    """
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    today_str = date.today().isoformat()

    for port_code, baseline in baselines.items():
        if baseline["sample_count"] < MIN_SAMPLE_COUNT:
            continue

        base_anchored = baseline["avg_anchored_7d"]
        if base_anchored == 0:
            continue

        # 최근 2시간 데이터 조회
        recent_resp = (
            supabase.table("port_history")
            .select("vessels_anchored")
            .eq("port_code", port_code)
            .gte("snapshot_at", two_hours_ago)
            .execute()
        )
        recent = recent_resp.data or []
        if not recent:
            continue

        avg_recent = sum(r["vessels_anchored"] for r in recent) / len(recent)
        ratio = avg_recent / base_anchored

        if ratio < ANOMALY_RATIO_MEDIUM:
            continue

        severity = "HIGH" if ratio >= ANOMALY_RATIO_HIGH else "MEDIUM"

        # 오늘 같은 port_code 미확인 AIS 플래그 중복 방지
        existing_resp = (
            supabase.table("anomaly_flags")
            .select("id")
            .eq("target_code", port_code)
            .eq("source", "AIS")
            .eq("acknowledged", False)
            .gte("detected_at", today_str + "T00:00:00+00:00")
            .execute()
        )
        if existing_resp.data:
            logger.info(f"  Skipping duplicate AIS anomaly flag for {port_code}")
            continue

        # 항만 이름 조회
        snap_resp = (
            supabase.table("port_snapshots")
            .select("port_name")
            .eq("port_code", port_code)
            .maybe_single()
            .execute()
        )
        port_name = ""
        if snap_resp.data:
            port_name = snap_resp.data.get("port_name", "")

        flag = {
            "source":      "AIS",
            "target_code": port_code,
            "target_name": port_name,
            "severity":    severity,
            "ratio":       round(ratio, 4),
            "detail": (
                f"Anchored vessels: 2h avg={avg_recent:.1f}, "
                f"7d baseline={base_anchored:.1f}, ratio={ratio:.2f}"
            ),
        }
        supabase.table("anomaly_flags").insert(flag).execute()
        logger.warning(
            f"ANOMALY [{severity}] AIS {port_code} ({port_name}): "
            f"ratio={ratio:.2f}, avg_recent={avg_recent:.1f}, base={base_anchored:.1f}"
        )


def cleanup_old_history(supabase: Client) -> None:
    """90일 이전 port_history 데이터 삭제"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETAIN_DAYS)).isoformat()
    resp = (
        supabase.table("port_history")
        .delete()
        .lt("snapshot_at", cutoff)
        .execute()
    )
    deleted = len(resp.data) if resp.data else 0
    logger.info(f"Deleted {deleted} old port_history rows (before {cutoff[:10]})")


def main() -> None:
    logger.info("anomaly_detector.py starting...")
    supabase = get_supabase()

    # 1. 기준선 갱신
    baselines = update_baselines(supabase)

    # 2. 이상 감지
    detect_ais_anomalies(supabase, baselines)

    # 3. 오래된 이력 정리
    cleanup_old_history(supabase)

    logger.info("anomaly_detector.py completed.")


if __name__ == "__main__":
    main()
