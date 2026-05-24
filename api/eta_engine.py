"""
MPCI Engine — ETA 계산 핵심 로직
Kwon(2008) 속도 손실 약산식 + M/M/c Erlang C 대기 이론
"""

import math
from datetime import datetime, timedelta

# ──────────────────────────────────────────────────────────
# 상수 테이블
# ──────────────────────────────────────────────────────────

# 초크포인트 우회 거리 패널티 (nm)
CHOKEPOINT_PENALTY = {
    "suez":       6800,
    "bab_mandeb": 6800,
    "panama":     8000,
    "hormuz":     4200,
    "malacca":    1100,
}

# 허브 항만별 초크포인트 연쇄 계수
CASCADE_FACTOR = {
    "SGSIN": {"bab_mandeb": 1.4, "suez": 1.3},
    "MYTPP": {"bab_mandeb": 1.35},
    "DJJIB": {"bab_mandeb": 1.8},
    "EGPSD": {"suez": 2.0},
    "JOAQJ": {"suez": 1.5},
}

# Erlang C 최대 선석 수 캡 (factorial 오버플로우 방지)
MAX_BERTHS = 20


# ──────────────────────────────────────────────────────────
# T_Base 계산
# ──────────────────────────────────────────────────────────

def calc_t_base(distance_nm: float, v_design: float) -> float:
    """
    T_Base = distance_nm / v_design (시간 단위)
    초크포인트 우회 여부는 입력 distance_nm에 포함된 것으로 가정.
    """
    return distance_nm / v_design


# ──────────────────────────────────────────────────────────
# ΔT_Weather 계산 (Kwon 2008 약산식)
# ──────────────────────────────────────────────────────────

def calc_delta_weather(
    distance_nm: float,
    v_design: float,
    bn: int,
    heading_deg: float,
) -> float:
    """
    Kwon(2008) 컨테이너선 속도 손실 약산식.

    V_actual = V_design × (1 - η × f(BN, θ))
    η = 0.7  (컨테이너선 선형계수)
    f(BN, θ) = (0.01 + 0.0025 × BN²) × |cos(θ)|

    ΔT_Weather = D/V_actual - D/V_design  (단위: 시간)
    """
    if bn == 0:
        return 0.0

    ETA_HULL = 0.7
    theta_rad = math.radians(heading_deg)

    # 속도 손실률
    f_bn_theta = (0.01 + 0.0025 * bn ** 2) * abs(math.cos(theta_rad))
    speed_loss_ratio = ETA_HULL * f_bn_theta
    speed_loss_ratio = min(speed_loss_ratio, 0.40)  # 최대 40% 손실 캡

    v_actual = v_design * (1.0 - speed_loss_ratio)
    v_actual = max(v_actual, v_design * 0.5)  # 최소 50% 속도 보장

    t_normal = distance_nm / v_design
    t_actual = distance_nm / v_actual
    return t_actual - t_normal


# ──────────────────────────────────────────────────────────
# Erlang C 내부 헬퍼
# ──────────────────────────────────────────────────────────

def _erlang_c(c: float, a: float) -> float:
    """
    Erlang C 대기 확률 계산.
    c: 서버(선석) 수, a: 트래픽 강도 (λ/μ)

    rho >= 1.0 이면 1.0 반환 (무한 대기 방지).
    math.factorial 오버플로우 방지: c 최대 MAX_BERTHS 캡.
    """
    c = int(min(max(c, 1), MAX_BERTHS))
    if a <= 0:
        return 0.0

    rho = a / c
    if rho >= 1.0:
        return 1.0

    try:
        sum_terms = sum((a ** k) / math.factorial(k) for k in range(c))
        last_term = (a ** c) / (math.factorial(c) * (1.0 - rho))
        p0 = 1.0 / (sum_terms + last_term)
        erlang_c = (a ** c * p0) / (math.factorial(c) * (1.0 - rho))
    except (OverflowError, ZeroDivisionError):
        return 1.0

    return min(erlang_c, 1.0)


# ──────────────────────────────────────────────────────────
# ΔT_Port 계산 (데이터 드리븐 + M/M/c 보조)
# ──────────────────────────────────────────────────────────

def calc_delta_port(
    port_snapshot: dict | None,
    port_baseline: dict | None,
    carrier_bias: float,
) -> tuple[float, dict]:
    """
    1차: port_snapshot의 MPCI(tpfs) 기반 롤링 평균 ΔT_Port 추정.
    2차: M/M/c Erlang C로 보조 검증.
    최종: 7:3 가중 평균 후 선사 낙관 편향 반영.

    반환: (delta_port_hours, debug_params)
    """
    debug: dict = {
        "lambda_arrivals": 0.0,
        "mu_service_rate": 0.0,
        "c_berths": 0.0,
        "rho_utilization": 0.0,
        "erlang_c_prob": 0.0,
        "mpci_score": 0.0,
    }

    if not port_snapshot:
        # 데이터 없음 → 기본 24h 버퍼
        return 24.0 * carrier_bias, debug

    # ── 1차: MPCI 기반 직접 추정 ──────────────────────────
    mpci = float(port_snapshot.get("tpfs") or 0.0)
    debug["mpci_score"] = mpci

    if mpci >= 75:
        base_wait_h = 72.0    # CONGESTED
    elif mpci >= 50:
        base_wait_h = 36.0    # BUSY
    elif mpci >= 25:
        base_wait_h = 18.0    # STABLE
    else:
        base_wait_h = 8.0     # LOW

    # 7일 기준선 대비 현재 배율 반영
    if port_baseline and (port_baseline.get("avg_tpfs_7d") or 0) > 0:
        ratio = mpci / port_baseline["avg_tpfs_7d"]
        ratio = max(0.5, min(ratio, 3.0))   # 0.5x ~ 3.0x 캡
        base_wait_h *= ratio

    # ── 2차: M/M/c Erlang C (보조) ────────────────────────
    schedule   = float(port_snapshot.get("econdb_congestion") or 5)
    turnaround = float(port_snapshot.get("econdb_turnaround") or 2.0)

    lam = max(schedule / 7.0, 0.1)                    # λ: 일일 입항 / 7
    mu  = max(1.0 / max(turnaround, 0.5), 0.1)        # μ: 1 / turnaround
    vessels_berthed = float(port_snapshot.get("vessels_berthed") or 3)
    c = max(vessels_berthed, 1.0)

    rho = lam / (c * mu)
    rho = min(rho, 0.99)

    a = lam / mu
    ec = _erlang_c(c, a)
    denom = c * mu - lam
    erlang_wait_h = ec / denom if denom > 0 else 48.0

    debug.update({
        "lambda_arrivals": round(lam, 3),
        "mu_service_rate": round(mu, 3),
        "c_berths": float(int(min(max(c, 1), MAX_BERTHS))),
        "rho_utilization": round(rho, 3),
        "erlang_c_prob": round(ec, 3),
    })

    # ── 가중 평균 (MPCI 7 : Erlang C 3) + 선사 편향 ──────
    delta_port = base_wait_h * 0.7 + erlang_wait_h * 0.3
    delta_port = max(delta_port, 1.0)   # 최소 1시간
    delta_port *= carrier_bias

    return delta_port, debug


# ──────────────────────────────────────────────────────────
# σ (불확실성) 계산
# ──────────────────────────────────────────────────────────

def calc_sigma(
    delta_weather_h: float,
    delta_port_h: float,
    has_anomaly: bool,
    has_chokepoint_risk: bool,
) -> float:
    """
    σ = √(σ_wx² + σ_port²) + 지정학 패널티
    σ_wx   = delta_weather_h × 0.3
    σ_port = delta_port_h   × 0.4
    """
    sigma_wx   = delta_weather_h * 0.3
    sigma_port = delta_port_h * 0.4
    sigma = math.sqrt(sigma_wx ** 2 + sigma_port ** 2)

    if has_anomaly:
        sigma *= 1.5
    if has_chokepoint_risk:
        sigma *= 1.3

    return max(sigma, 6.0)   # 최소 6시간


# ──────────────────────────────────────────────────────────
# 메인 계산 함수
# ──────────────────────────────────────────────────────────

def calculate_eta(
    etd_datetime: datetime,
    t_base_h: float,
    delta_weather_h: float,
    delta_port_h: float,
    delta_override_h: float,
    sigma_h: float,
) -> tuple[datetime, datetime, datetime]:
    """
    P50 = ETD + T_Base + ΔT_Weather + ΔT_Port + ΔT_Override
    P10 = P50 - 1.28σ  (낙관 시나리오, 10th percentile)
    P90 = P50 + 1.28σ  (보수 시나리오, 90th percentile)
    """
    total_h = t_base_h + delta_weather_h + delta_port_h + delta_override_h
    p50 = etd_datetime + timedelta(hours=total_h)
    p10 = p50 - timedelta(hours=1.28 * sigma_h)
    p90 = p50 + timedelta(hours=1.28 * sigma_h)
    return p10, p50, p90
