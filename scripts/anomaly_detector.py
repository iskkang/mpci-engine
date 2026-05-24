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
AIS_RECENT_HOURS     = 24   # AIS 보조 지표 최근 평균 윈도우
AIS_DAILY_RETAIN_DAYS = 395 # 일별 AIS 롤업 보관 기간
PAGE_SIZE            = 1000 # PostgREST 기본 행 제한 회피용 페이지 크기


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _num(value) -> float:
    """DB 값(None/문자열 가능)을 안전하게 float 로. 결측/비정상은 0.0."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _select_all(build_query) -> list[dict]:
    """페이지네이션으로 전량 조회 (PostgREST 1000행 제한 회피).

    build_query(start, end) 는 필터·정렬이 적용된 쿼리에 .range(start, end) 까지
    붙여 반환해야 한다. 안정적 페이징을 위해 호출부에서 .order() 로 결정적 순서를 줄 것.
    """
    rows: list[dict] = []
    start = 0
    while True:
        resp = build_query(start, start + PAGE_SIZE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


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
    rows = _select_all(
        lambda s, e: (
            supabase.table("port_history")
            .select("port_code,vessels_anchored,vessels_berthed,tpfs")
            .gte("snapshot_at", cutoff)
            .order("snapshot_at").order("port_code")  # 결정적 순서로 안정 페이징
            .range(s, e)
        )
    )

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
        avg_a = sum(_num(s.get("vessels_anchored")) for s in samples) / n
        avg_b = sum(_num(s.get("vessels_berthed")) for s in samples) / n
        avg_t = sum(_num(s.get("tpfs")) for s in samples) / n

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

        avg_recent = sum(_num(r.get("vessels_anchored")) for r in recent) / len(recent)
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


def update_ais_supplement(supabase: Client, baselines: dict[str, dict]) -> None:
    """
    Compute AIS supplemental signal from aggregated port_history only.
    This updates port_snapshots for dashboard/ETA context without changing
    EconDB-derived MPCI directly.
    """
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(hours=AIS_RECENT_HOURS)).isoformat()
    now_str = datetime.now(timezone.utc).isoformat()
    updates = 0

    for port_code, baseline in baselines.items():
        if baseline["sample_count"] < MIN_SAMPLE_COUNT:
            continue

        recent_resp = (
            supabase.table("port_history")
            .select("vessels_anchored,vessels_berthed,tpfs")
            .eq("port_code", port_code)
            .gte("snapshot_at", recent_cutoff)
            .execute()
        )
        recent = recent_resp.data or []
        if not recent:
            continue

        avg_recent_anchored = sum(_num(r.get("vessels_anchored")) for r in recent) / len(recent)
        avg_recent_berthed = sum(_num(r.get("vessels_berthed")) for r in recent) / len(recent)
        base_anchored = float(baseline["avg_anchored_7d"])

        if base_anchored > 0:
            ratio = avg_recent_anchored / base_anchored
            wait_index = clamp(50.0 + (ratio - 1.0) * 35.0, 0.0, 100.0)
        else:
            ratio = None
            wait_index = clamp(avg_recent_anchored * 20.0, 0.0, 100.0)

        if ratio is not None and ratio >= ANOMALY_RATIO_HIGH:
            level = "HIGH"
        elif ratio is not None and ratio >= ANOMALY_RATIO_MEDIUM:
            level = "MEDIUM"
        elif wait_index >= 70:
            level = "WATCH"
        else:
            level = "NORMAL"

        data = {
            "port_code": port_code,
            "ais_recent_anchored_avg": round(avg_recent_anchored, 2),
            "ais_recent_berthed_avg": round(avg_recent_berthed, 2),
            "ais_baseline_anchored_avg": round(base_anchored, 2),
            "ais_wait_ratio": round(ratio, 4) if ratio is not None else None,
            "ais_wait_index": round(wait_index, 1),
            "ais_anomaly_level": level,
            "ais_sample_count_7d": baseline["sample_count"],
            "ais_updated_at": now_str,
        }
        try:
            # update().eq() 는 row 가 없으면 조용히 0건 no-op 이므로 upsert 로 변경.
            supabase.table("port_snapshots").upsert(data, on_conflict="port_code").execute()
            updates += 1
        except Exception as e:
            logger.warning(f"Failed to upsert AIS supplement for {port_code}: {e}")

    logger.info(f"Updated AIS supplemental metrics for {updates} ports.")


def update_ais_daily_rollups(supabase: Client) -> None:
    """Roll up recent 15-minute AIS aggregates into daily rows."""
    start_date = date.today() - timedelta(days=1)
    cutoff = start_date.isoformat() + "T00:00:00+00:00"
    rows = _select_all(
        lambda s, e: (
            supabase.table("port_history")
            .select("port_code,snapshot_at,vessels_anchored,vessels_berthed,tpfs")
            .gte("snapshot_at", cutoff)
            .order("snapshot_at").order("port_code")  # 결정적 순서로 안정 페이징
            .range(s, e)
        )
    )

    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        snapshot_at = row.get("snapshot_at", "")
        observed_date = snapshot_at[:10]
        if not observed_date:
            continue
        key = (row["port_code"], observed_date)
        buckets.setdefault(key, []).append(row)

    now_str = datetime.now(timezone.utc).isoformat()
    upserts = []
    for (port_code, observed_date), samples in buckets.items():
        n = len(samples)
        upserts.append({
            "port_code": port_code,
            "observed_date": observed_date,
            "avg_vessels_anchored": round(sum(_num(s.get("vessels_anchored")) for s in samples) / n, 2),
            "avg_vessels_berthed": round(sum(_num(s.get("vessels_berthed")) for s in samples) / n, 2),
            # max_vessels_* 컬럼은 INTEGER 이므로 int() 캐스팅 (float 281.0 → 22P02 방지)
            "max_vessels_anchored": int(max(_num(s.get("vessels_anchored")) for s in samples)),
            "max_vessels_berthed": int(max(_num(s.get("vessels_berthed")) for s in samples)),
            "avg_tpfs": round(sum(_num(s.get("tpfs")) for s in samples) / n, 2),
            "sample_count": n,
            "updated_at": now_str,
        })

    if upserts:
        supabase.table("port_ais_daily").upsert(
            upserts, on_conflict="port_code,observed_date"
        ).execute()
        logger.info(f"Upserted {len(upserts)} AIS daily rollup rows.")


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

    daily_cutoff = (date.today() - timedelta(days=AIS_DAILY_RETAIN_DAYS)).isoformat()
    daily_resp = (
        supabase.table("port_ais_daily")
        .delete()
        .lt("observed_date", daily_cutoff)
        .execute()
    )
    daily_deleted = len(daily_resp.data) if daily_resp.data else 0
    logger.info(f"Deleted {daily_deleted} old port_ais_daily rows (before {daily_cutoff})")


def main() -> None:
    logger.info("anomaly_detector.py starting...")
    supabase = get_supabase()

    # 1. 기준선 갱신
    baselines = update_baselines(supabase)

    # 2. 이상 감지
    detect_ais_anomalies(supabase, baselines)

    # 3. AIS 보조 지표/일별 롤업 갱신
    update_ais_supplement(supabase, baselines)
    update_ais_daily_rollups(supabase)

    # 4. 오래된 이력 정리
    cleanup_old_history(supabase)

    logger.info("anomaly_detector.py completed.")


if __name__ == "__main__":
    main()