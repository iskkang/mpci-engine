"""
MPCI Engine — FastAPI ETA Calculation API
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .models import ETARequest, ETAResponse, ETABreakdown
from .db import (
    fetch_port_snapshot,
    fetch_port_baseline,
    fetch_active_override,
    fetch_carrier_bias,
    save_eta_calculation,
    get_supabase,
)
from .eta_engine import (
    calc_t_base,
    calc_delta_weather,
    calc_delta_port,
    calc_sigma,
    calculate_eta,
)

app = FastAPI(
    title="MPCI ETA Engine",
    version="1.0.0",
    description="MPCI 기반 P10/P50/P90 ETA 범위 계산 REST API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # 추후 mtl_link 도메인으로 제한
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────
# 헬스체크
# ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ──────────────────────────────────────────────────────────
# 항만 MPCI 조회
# ──────────────────────────────────────────────────────────

@app.get("/ports/{port_code}/mpci")
def get_port_mpci(port_code: str):
    """특정 항만의 현재 MPCI 조회"""
    snap = fetch_port_snapshot(port_code.upper())
    if not snap:
        raise HTTPException(status_code=404, detail=f"Port {port_code} not found")
    return snap


@app.get("/ports/")
def list_ports():
    """전체 항만 MPCI 목록 조회 (tpfs 내림차순)"""
    sb = get_supabase()
    res = (
        sb.table("port_snapshots")
        .select("port_code,port_name,country,level,tpfs,updated_at")
        .order("tpfs", desc=True)
        .execute()
    )
    return res.data or []


# ──────────────────────────────────────────────────────────
# ETA 계산 메인 엔드포인트
# ──────────────────────────────────────────────────────────

@app.post("/eta/calculate", response_model=ETAResponse)
def calculate(req: ETARequest):
    """
    ETA 보정 계산 메인 엔드포인트.
    입력: 도착항, 선사, 출발일, 잔여거리, 속도, 날씨
    출력: P10 / P50 / P90 도착 예정 범위
    """
    port_code = req.port_code.upper()
    warnings: list[str] = []

    # 1. DB 데이터 조회
    snap     = fetch_port_snapshot(port_code)
    baseline = fetch_port_baseline(port_code)
    override = fetch_active_override(port_code)

    if req.carrier:
        carrier_bias = fetch_carrier_bias(req.carrier, port_code)
    else:
        carrier_bias = 1.15  # 선사 미입력 시 기본 낙관 편향

    if not snap:
        warnings.append(f"항만 {port_code} 데이터 없음 — 기본값으로 계산")
        port_name = port_code
    else:
        port_name = snap.get("port_name", port_code)

    # 2. 오버라이드 확인
    delta_override_h = 0.0
    if override:
        delta_override_h = float(override["delay_days"]) * 24.0
        reason = override.get("reason", "관리자 설정")
        warnings.append(
            f"수동 버퍼 적용: +{override['delay_days']}일 ({reason})"
        )

    # 3. 이상/혼잡 플래그 확인
    has_anomaly = False
    has_chokepoint_risk = False

    if snap and snap.get("level") == "CONGESTED":
        has_anomaly = True
        warnings.append(f"{port_name} 혼잡 CONGESTED — 불확실성 구간 확대")

    # 4. ETA 계산
    t_base_h = calc_t_base(req.distance_nm, req.v_design)

    delta_weather_h = calc_delta_weather(
        req.distance_nm, req.v_design, req.bn, req.heading_deg
    )

    delta_port_h, debug = calc_delta_port(snap, baseline, carrier_bias)

    sigma_h = calc_sigma(
        delta_weather_h, delta_port_h, has_anomaly, has_chokepoint_risk
    )

    etd_dt = datetime.combine(
        req.etd, datetime.min.time()
    ).replace(tzinfo=timezone.utc)

    p10, p50, p90 = calculate_eta(
        etd_dt, t_base_h, delta_weather_h,
        delta_port_h, delta_override_h, sigma_h,
    )

    # 선사 공식 ETA = ETD + T_Base (순항 기준)
    carrier_eta  = etd_dt + timedelta(hours=t_base_h)
    p50_delay_h  = (p50 - carrier_eta).total_seconds() / 3600

    # 5. 결과 저장 (실패해도 응답 반환)
    try:
        save_eta_calculation({
            "port_code":         port_code,
            "carrier":           req.carrier,
            "etd":               req.etd.isoformat(),
            "distance_nm":       req.distance_nm,
            "t_base_hours":      round(t_base_h, 2),
            "delta_weather_h":   round(delta_weather_h, 2),
            "delta_port_h":      round(delta_port_h, 2),
            "delta_override_h":  round(delta_override_h, 2),
            "sigma_hours":       round(sigma_h, 2),
            "mpci_at_calc":      debug.get("mpci_score", 0),
            "p10_datetime":      p10.isoformat(),
            "p50_datetime":      p50.isoformat(),
            "p90_datetime":      p90.isoformat(),
            "source":            "api",
        })
    except Exception:
        pass

    return ETAResponse(
        port_code=port_code,
        port_name=port_name,
        carrier=req.carrier,
        etd=req.etd,
        p10=p10,
        p50=p50,
        p90=p90,
        p50_delay_hours=round(p50_delay_h, 1),
        breakdown=ETABreakdown(
            t_base_hours=round(t_base_h, 2),
            delta_weather_hours=round(delta_weather_h, 2),
            delta_port_hours=round(delta_port_h, 2),
            delta_override_hours=round(delta_override_h, 2),
            sigma_hours=round(sigma_h, 2),
            carrier_bias_factor=round(carrier_bias, 3),
            **{k: round(v, 3) for k, v in debug.items()},
        ),
        warnings=warnings,
        calculated_at=datetime.now(timezone.utc),
    )
