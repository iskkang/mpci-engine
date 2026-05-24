"""
MPCI Engine - FastAPI ETA calculation API.
"""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .db import (
    fetch_active_override,
    fetch_port_baseline,
    fetch_port_snapshot,
    get_supabase,
    save_eta_calculation,
)
from .eta_engine import (
    calc_delta_port,
    calc_delta_weather,
    calc_econdb_mpci,
    calc_sigma,
    calc_t_base,
    calculate_eta,
)
from .models import ETABreakdown, ETARequest, ETAResponse

app = FastAPI(
    title="MPCI ETA Engine",
    version="1.0.0",
    description="EconDB-based P10/P50/P90 ETA range calculation API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/ports/{port_code}/mpci")
def get_port_mpci(port_code: str):
    snap = fetch_port_snapshot(port_code.upper())
    if not snap:
        raise HTTPException(status_code=404, detail=f"Port {port_code} not found")
    return {**snap, "mpci": calc_econdb_mpci(snap)}


@app.get("/ports/")
def list_ports():
    sb = get_supabase()
    res = (
        sb.table("port_snapshots")
        .select(
            "port_code,port_name,country,region,updated_at,"
            "vessels_anchored,vessels_berthed,"
            "econdb_congestion,econdb_delay_pct,econdb_turnaround,econdb_updated_at,"
            "econdb_current_index,historic_percentile_index,trend_change_index,"
            "final_mpci,mpci_confidence,mpci_history_days,mpci_delta_prev,mpci_delta_pct_prev,"
            "ais_recent_anchored_avg,ais_baseline_anchored_avg,ais_wait_ratio,"
            "ais_wait_index,ais_anomaly_level,ais_updated_at"
        )
        .order("port_code")
        .execute()
    )
    rows = res.data or []
    return [{**row, "mpci": calc_econdb_mpci(row)} for row in rows]


@app.post("/eta/calculate", response_model=ETAResponse)
def calculate(req: ETARequest):
    port_code = req.port_code.upper()
    warnings: list[str] = []

    snap = fetch_port_snapshot(port_code)
    baseline = fetch_port_baseline(port_code)
    override = fetch_active_override(port_code)

    # Phase 1 removes unverified carrier on-time bias.
    carrier_bias = 1.0

    if not snap:
        warnings.append(f"Port {port_code} has no data; default buffer applied")
        port_name = port_code
    else:
        port_name = snap.get("port_name", port_code)

    delta_override_h = 0.0
    if override:
        delta_override_h = float(override["delay_days"]) * 24.0
        reason = override.get("reason", "manual override")
        warnings.append(f"Manual buffer applied: +{override['delay_days']} days ({reason})")

    mpci = calc_econdb_mpci(snap)
    ais_level = snap.get("ais_anomaly_level") if snap else None
    has_anomaly = (mpci is not None and mpci >= 75) or ais_level in {"HIGH", "MEDIUM"}
    has_chokepoint_risk = False
    if has_anomaly:
        warnings.append(f"{port_name} is CONGESTED; uncertainty range expanded")

    t_base_h = calc_t_base(req.distance_nm, req.v_design)
    delta_weather_h = calc_delta_weather(
        req.distance_nm, req.v_design, req.bn, req.heading_deg
    )
    delta_port_h, debug = calc_delta_port(snap, baseline, carrier_bias)
    sigma_h = calc_sigma(
        delta_weather_h, delta_port_h, has_anomaly, has_chokepoint_risk
    )

    etd_dt = datetime.combine(req.etd, datetime.min.time()).replace(tzinfo=timezone.utc)
    p10, p50, p90 = calculate_eta(
        etd_dt,
        t_base_h,
        delta_weather_h,
        delta_port_h,
        delta_override_h,
        sigma_h,
    )

    carrier_eta = etd_dt + timedelta(hours=t_base_h)
    p50_delay_h = (p50 - carrier_eta).total_seconds() / 3600

    try:
        save_eta_calculation(
            {
                "port_code": port_code,
                "carrier": None,
                "etd": req.etd.isoformat(),
                "distance_nm": req.distance_nm,
                "t_base_hours": round(t_base_h, 2),
                "delta_weather_h": round(delta_weather_h, 2),
                "delta_port_h": round(delta_port_h, 2),
                "delta_override_h": round(delta_override_h, 2),
                "sigma_hours": round(sigma_h, 2),
                "mpci_at_calc": debug.get("mpci_score", 0),
                "p10_datetime": p10.isoformat(),
                "p50_datetime": p50.isoformat(),
                "p90_datetime": p90.isoformat(),
                "source": "api",
            }
        )
    except Exception:
        pass

    return ETAResponse(
        port_code=port_code,
        port_name=port_name,
        carrier=None,
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
