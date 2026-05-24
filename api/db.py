import os
from supabase import create_client


def get_supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


def fetch_port_snapshot(port_code: str) -> dict | None:
    """port_snapshots에서 항만 현황 조회"""
    sb = get_supabase()
    try:
        res = (
            sb.table("port_snapshots")
            .select("*")
            .eq("port_code", port_code)
            .single()
            .execute()
        )
        return res.data
    except Exception:
        return None


def fetch_port_baseline(port_code: str) -> dict | None:
    """port_baselines에서 7일 기준선 조회"""
    sb = get_supabase()
    try:
        res = (
            sb.table("port_baselines")
            .select("*")
            .eq("port_code", port_code)
            .single()
            .execute()
        )
        return res.data
    except Exception:
        return None


def fetch_active_override(port_code: str) -> dict | None:
    """port_overrides에서 현재 활성 오버라이드 조회"""
    from datetime import date

    today = date.today().isoformat()
    sb = get_supabase()
    try:
        res = (
            sb.table("port_overrides")
            .select("*")
            .eq("port_code", port_code)
            .eq("is_active", True)
            .lte("start_date", today)
            .execute()
        )
        rows = res.data or []
    except Exception:
        return None

    # end_date가 없거나 오늘 이후인 것만
    for row in rows:
        if row.get("end_date") is None or row["end_date"] >= today:
            return row
    return None


def fetch_carrier_bias(carrier: str, port_code: str) -> float:
    """
    carrier_port_ontime에서 선사 적시율 조회 → 낙관 편향 계수 반환.
    - ontime_pct = 1.0 → bias = 1.0 (보정 없음)
    - ontime_pct = 0.5 → bias = 1.5 (50% 추가)
    """
    sb = get_supabase()
    try:
        res = (
            sb.table("carrier_port_ontime")
            .select("ontime_pct")
            .eq("carrier", carrier)
            .eq("port_code", port_code)
            .single()
            .execute()
        )
        if res.data:
            ontime = res.data["ontime_pct"]
            return 2.0 - ontime
    except Exception:
        pass
    # 데이터 없을 때 기본값: ontime 65% → bias 1.35
    return 2.0 - 0.65


def save_eta_calculation(data: dict) -> None:
    """eta_calculations 테이블에 계산 결과 저장"""
    sb = get_supabase()
    sb.table("eta_calculations").insert(data).execute()
