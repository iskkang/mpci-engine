"""
MPCI Engine - ETA calculation core.

Phase 1 uses EconDB as the trusted port congestion source. AIS-derived
anchored/berthed counts remain supplemental context, not MPCI inputs.
"""

import math
from datetime import datetime, timedelta

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

MAX_BERTHS = 20


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

    cong = port_snapshot.get("econdb_congestion")
    delay_pct = port_snapshot.get("econdb_delay_pct")
    turnaround = port_snapshot.get("econdb_turnaround")
    if cong is None or delay_pct is None or turnaround is None:
        return None

    try:
        cong_score = _clamp((float(cong) / 18.0) * 100.0, 0.0, 100.0)
        delay_score = _clamp(float(delay_pct), 0.0, 100.0)
        turn_score = _clamp(((float(turnaround) - 0.5) / 4.5) * 100.0, 0.0, 100.0)
    except (TypeError, ValueError):
        return None

    mpci = cong_score * 0.35 + delay_score * 0.35 + turn_score * 0.30
    return round(_clamp(mpci, 0.0, 100.0), 1)


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

    f_bn_theta = (0.01 + 0.0025 * bn**2) * abs(math.cos(theta_rad))
    speed_loss_ratio = min(eta_hull * f_bn_theta, 0.40)

    v_actual = max(v_design * (1.0 - speed_loss_ratio), v_design * 0.5)

    return distance_nm / v_actual - distance_nm / v_design


def _erlang_c(c: float, a: float) -> float:
    c = int(min(max(c, 1), MAX_BERTHS))
    if a <= 0:
        return 0.0

    rho = a / c
    if rho >= 1.0:
        return 1.0

    try:
        sum_terms = sum((a**k) / math.factorial(k) for k in range(c))
        last_term = (a**c) / (math.factorial(c) * (1.0 - rho))
        p0 = 1.0 / (sum_terms + last_term)
        erlang_c = (a**c * p0) / (math.factorial(c) * (1.0 - rho))
    except (OverflowError, ZeroDivisionError):
        return 1.0

    return min(erlang_c, 1.0)


def calc_delta_port(
    port_snapshot: dict | None,
    port_baseline: dict | None,
    carrier_bias: float,
) -> tuple[float, dict]:
    """Estimate port delay from EconDB MPCI and turnaround only."""
    debug: dict = {
        "lambda_arrivals": 0.0,
        "mu_service_rate": 0.0,
        "c_berths": 0.0,
        "rho_utilization": 0.0,
        "erlang_c_prob": 0.0,
        "mpci_score": 0.0,
    }

    mpci = calc_econdb_mpci(port_snapshot)
    if mpci is None:
        return 24.0 * carrier_bias, debug

    debug["mpci_score"] = mpci

    if mpci >= 75:
        base_wait_h = 72.0
    elif mpci >= 50:
        base_wait_h = 36.0
    elif mpci >= 25:
        base_wait_h = 18.0
    else:
        base_wait_h = 8.0

    turnaround_days = float(port_snapshot.get("econdb_turnaround") or 1.0)
    turnaround_h = max(turnaround_days * 24.0, 1.0)
    congestion = float(port_snapshot.get("econdb_congestion") or 0.0)
    delay_pct = float(port_snapshot.get("econdb_delay_pct") or 0.0)

    debug.update(
        {
            "lambda_arrivals": round(congestion, 3),
            "mu_service_rate": round(1.0 / max(turnaround_days, 0.5), 3),
            "c_berths": float(port_snapshot.get("vessels_berthed") or 0.0),
            "rho_utilization": round(_clamp(delay_pct / 100.0, 0.0, 0.99), 3),
            "erlang_c_prob": 0.0,
        }
    )

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
