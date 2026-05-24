from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, date


class ETARequest(BaseModel):
    port_code: str = Field(..., description="도착항 UN/LOCODE (예: NLRTM)")
    carrier: Optional[str] = Field(None, description="선사명 (예: Hapag-Lloyd)")
    etd: date = Field(..., description="출발일 (YYYY-MM-DD)")
    distance_nm: float = Field(..., ge=100, le=25000, description="잔여 항로 거리 (해리)")
    v_design: float = Field(20.0, ge=10, le=30, description="선박 설계속도 (kts)")
    bn: int = Field(4, ge=0, le=12, description="보포트 풍력계급 (0-12)")
    heading_deg: float = Field(
        90.0, ge=0, le=180,
        description="조우각 (0=정면파, 90=측면파, 180=순풍)"
    )


class ETABreakdown(BaseModel):
    t_base_hours: float
    delta_weather_hours: float
    delta_port_hours: float
    delta_override_hours: float
    sigma_hours: float
    lambda_arrivals: float      # 일일 입항 선박수
    mu_service_rate: float      # 선석당 처리율
    c_berths: float             # 유효 선석수
    rho_utilization: float      # 이용률
    erlang_c_prob: float        # 대기 확률
    mpci_score: float           # 계산 시점 MPCI
    carrier_bias_factor: float  # 선사 낙관 편향 계수


class ETAResponse(BaseModel):
    port_code: str
    port_name: str
    carrier: Optional[str]
    etd: date
    p10: datetime
    p50: datetime
    p90: datetime
    p50_delay_hours: float      # 선사 ETA 대비 P50 지연
    breakdown: ETABreakdown
    warnings: list[str]         # 이상 플래그, 오버라이드 적용 여부 등
    calculated_at: datetime
