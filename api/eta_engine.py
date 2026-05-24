"""
MPCI Engine - ETA calculation core.

Phase 1 uses EconDB as the trusted port congestion source. AIS-derived
anchored/berthed counts remain supplemental context, not MPCI inputs.
"""

import math
from datetime import datetime, timedelta

try:
    from .mpci_core import compute_mpci  # 패키지 컨텍스트(FastAPI 앱)
except ImportError:  # standalone 컨텍스트
    from mpci_core import compute_mpci

CHOKEPOINT_PENALTY = {
    "suez": 6800,
    "bab_mandeb": 6800,
    "panama": 8000,
    "hormuz": 4200,
    "malacca": 1100,
}

CASCADE_FACTOR = {
    "SGSIN": {"bab_mandeb": 1.4, "suez": 1.3},
    "MYTPP": {"bab_mandeb": 1.35},
    "DJJIB": {"bab_mandeb": 1.8},
    "EGPSD": {"suez": 2.0},
    "JOAQJ": {"suez": 1.5},
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def calc_econdb_mpci(port_snapshot: dict | None) -> float | None:
    """Calculate MPCI from EconDB congestion, delay rate, and turnaround."""
    if not port_snapshot:
        return None

    final_mpci = port_snapshot.get("final_mpci")
    if final_mpci is not None:
        try:
            return round(_clamp(float(final_mpci), 0.0, 100.0), 1)
        except (TypeError, ValueError):
            pass

    # 산식은 mpci_core 한 곳에만. 키 이름은 스냅샷 스키마(econdb_*)에 맞춰 전달.
    return compute_mpci(
        port_snapshot.get("econdb_congestion"),
        port_snapshot.get("econdb_delay_pct"),
        port_snapshot.get("econdb_turnaround"),
    )


def calc_t_base(distance_nm: float, v_design: float) -> float:
    return distance_nm / v_design


def calc_delta_weather(
    distance_nm: float,
    v_design: float,
    bn: int,
    heading_deg: float,
) -> float:
    """Approximate weather delay from Beaufort number and relative heading."""
    if bn == 0:
        return 0.0

    eta_hull = 0.7
    theta_rad = math.radians(heading_deg)

    # 방향 계수: 0°(정면파)=1.0 → 90°(측면파)=0.5 → 180°(순풍)=0.0.
    # 기존 abs(cos)는 180°에서도 1.0이 되어 순풍을 역풍처럼 최대 감속시켰음.
    direction_factor = (1.0 + math.cos(theta_rad)) / 2.0
    f_bn_theta = (0.01 + 0.0025 * bn**2) * direction_factor
    speed_loss_ratio = min(eta_hull * f_bn_theta, 0.40)

    v_actual = max(v_design * (1.0 - speed_loss_ratio), v_design * 0.5)

    return distance_nm / v_actual - distance_nm / v_design


def calc_delta_port(
    port_snapshot: dict | None,
    port_baseline: dict | None,
    carrier_bias: float,
) -> tuple[float, dict]:
    """Estimate port delay from EconDB MPCI and turnaround.

    야드 보강 (1편 산출물 활용):
      - port_snapshot에 yard_congestion_index(0–100)가 있으면
        search 기반 MPCI와 가중 평균(search 60 : yard 40)해 더 높은 confidence로 사용.
      - 없으면(None) 기존 동작 100% 동일 → 회귀 없음.
      - 새 confidence 티어: breakdown.yard_congestion_index 노출.
      - 초기 가중치(60:40)는 초기 휴리스틱 — 운영 데이터로 보정 필요.
    """
    debug: dict = {
        "mpci_score": 0.0,
        "base_wait_h": 0.0,
        "turnaround_h": 0.0,
    }

    mpci = calc_econdb_mpci(port_snapshot)
    if mpci is None:
        return 24.0 * carrier_bias, debug

    # ── 야드 적체 지수 교차검증 ──────────────────────────────────────────────
    # yard_congestion_index: 항만 자기 이력 백분위(0–100), 1편 fetch_econdb_regions.py 산출.
    # 상위 8개 항만만 채워짐. 없으면(None) effective_mpci = mpci → 기존 동작 그대로.
    yard_idx: float | None = None
    if port_snapshot:
        raw_yard = port_snapshot.get("yard_congestion_index")
        if raw_yard is not None:
            try:
                yard_idx = _clamp(float(raw_yard), 0.0, 100.0)
            except (TypeError, ValueError):
                yard_idx = None

    # yard 없으면 기존 search 기반 MPCI 그대로 — 회귀 없음
    effective_mpci = mpci
    if yard_idx is not None:
        # 초기 가중치: search 60%, yard 40% — 운영 데이터로 보정 필요
        effective_mpci = _clamp(mpci * 0.60 + yard_idx * 0.40, 0.0, 100.0)

    debug["mpci_score"] = effective_mpci

    if effective_mpci >= 75:
        base_wait_h = 72.0
    elif effective_mpci >= 50:
        base_wait_h = 36.0
    elif effective_mpci >= 25:
        base_wait_h = 18.0
    else:
        base_wait_h = 8.0

    turnaround_days = float(port_snapshot.get("econdb_turnaround") or 1.0)
    turnaround_h = max(turnaround_days * 24.0, 1.0)

    debug["base_wait_h"] = round(base_wait_h, 3)
    debug["turnaround_h"] = round(turnaround_h, 3)

    # yard 있을 때만 breakdown에 노출 (없으면 ETABreakdown default=None)
    if yard_idx is not None:
        debug["yard_congestion_index"] = round(yard_idx, 1)

    delta_port = base_wait_h * 0.45 + turnaround_h * 0.55
    return max(delta_port, 1.0) * carrier_bias, debug


def calc_sigma(
    delta_weather_h: float,
    delta_port_h: float,
    has_anomaly: bool,
    has_chokepoint_risk: bool,
) -> float:
    sigma_wx = delta_weather_h * 0.3
    sigma_port = delta_port_h * 0.4
    sigma = math.sqrt(sigma_wx**2 + sigma_port**2)

    if has_anomaly:
        sigma *= 1.5
    if has_chokepoint_risk:
        sigma *= 1.3

    return max(sigma, 6.0)


def calculate_eta(
    etd_datetime: datetime,
    t_base_h: float,
    delta_weather_h: float,
    delta_port_h: float,
    delta_override_h: float,
    sigma_h: float,
) -> tuple[datetime, datetime, datetime]:
    total_h = t_base_h + delta_weather_h + delta_port_h + delta_override_h
    p50 = etd_datetime + timedelta(hours=total_h)
    p10 = p50 - timedelta(hours=1.28 * sigma_h)
    p90 = p50 + timedelta(hours=1.28 * sigma_h)
    return p10, p50, p90
