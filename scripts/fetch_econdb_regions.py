"""
MPCI Engine — EconDB Region 지표 수집기 (1/2)
==============================================
4개 region widget 엔드포인트를 대상 region 전체에 대해 수집·파싱하고
Supabase에 업서트합니다.

엔드포인트(단건 GET, WAF 없음):
  1. maritime/congestion_region_ports/?region=X        → econdb_port_catalog
  2. widgets/containers-in-terminal/data/?region=X     → region_yard_dwell_weekly
                                                          region_congestion_weekly
  3. widgets/omissions-time-series/data/?region=X      → region_omissions_weekly
  4. widgets/weekly-schedule-profile/data/?region=X&metric=TEU&size=ALL
                                                       → region_schedule_profile

Phase 2 (이 스크립트 내부):
  - region_index (파생 지표, 룰 기반, 예측 모델 없음)
  - port_snapshots.yard_congestion_index (상위 8개 항만)

실행: python scripts/fetch_econdb_regions.py
환경: SUPABASE_URL, SUPABASE_SERVICE_KEY

가드레일:
  - 응답 스키마 불일치 → 로그+스킵, 값 날조 금지
  - is_partial / is_forecast 로 미완성·미래 구분
  - region 간 합산 금지 (각 region 독립)
  - per-port MPCI는 search 유지, 야드는 상위 8개 보강
  - 운임/딜레이는 룰 기반 지표 (예측 모델 없음)
  - Central America / Oceania 제외 (TODO: 별도 스키마 필요)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ── sys.path 설정 (standalone & module 모두 지원) ────────────────────────────
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import httpx
from supabase import create_client, Client

from _econdb_http import (
    ECONDB_BASE,
    ECONDB_LOCODE_ALIAS,
    normalize_locode,
    make_region_client,
    region_get,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── 대상 region ──────────────────────────────────────────────────────────────
REGIONS: list[str] = [
    "East Asia",
    "Middle East",
    "Indian Subcontinent",
    "Mediterranean",
    "East Med",
    "West Med",
    "Northwest Europe",
    "Baltic",
    "West Africa",
    "East Africa",
    "South Africa",
    "North America East",
    "North America West",
    "US GOM",
    "East South America",
    "West South America",
]
# TODO: "Central America", "Oceania" — page_metadata/?view=maritime&mode=congestion&code=X
#       스키마가 위 4개 widget과 달라 별도 처리 필요. 현재 범위 제외.

# ── 임계값 상수 (초기 휴리스틱, 운영 데이터로 보정 필요) ──────────────────────
BLANK_PCT_HIGH: float = 15.0      # % — 블랭크세일링 높음 기준
BLANK_PCT_LOW: float = 5.0        # % — 블랭크세일링 낮음 기준
CONGESTION_HIGH: float = 70.0     # /100 — 야드 적체 높음 기준
CONGESTION_MODERATE: float = 40.0  # /100 — 야드 적체 중간 기준


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def mean_vals(values: list) -> Optional[float]:
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


def encode_region(region: str) -> str:
    """공백을 + 로 인코딩 (EconDB region URL 규칙)."""
    return region.replace(" ", "+")


def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def latest_complete_cutoff(ref_date: date) -> date:
    """최신 완전주 커트라인: week_start <= ref_date - 7일."""
    return ref_date - timedelta(days=7)


def parse_week_date(value: str) -> Optional[date]:
    """'2026-05-12' 또는 '2026-05-12T00:00:00' → date."""
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        logger.warning("Cannot parse week date: %r", value)
        return None


def parse_ts(value: str) -> Optional[datetime]:
    """'2026-05-12T00:00:00' 또는 '2026-05-12' → timezone-aware datetime (UTC)."""
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value)
        else:
            dt = datetime.fromisoformat(value + "T00:00:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        logger.warning("Cannot parse timestamp: %r", value)
        return None


# ── 수집기 ───────────────────────────────────────────────────────────────────

def fetch_port_catalog(client: httpx.Client, region: str) -> list[dict]:
    """
    congestion_region_ports → econdb_port_catalog 행 목록.
    locode는 ECONDB_LOCODE_ALIAS 적용 후 내부 코드로 저장.
    """
    url = f"{ECONDB_BASE}/maritime/congestion_region_ports/?region={encode_region(region)}"
    data = region_get(client, url)
    if data is None:
        return []

    ports_raw = data.get("ports")
    if not isinstance(ports_raw, list):
        logger.warning(
            "Unexpected congestion_region_ports response for %s — keys: %s",
            region, list(data.keys()),
        )
        return []

    result: list[dict] = []
    for p in ports_raw:
        raw_locode = p.get("locode")
        if not raw_locode:
            continue
        locode = normalize_locode(str(raw_locode))
        locode = ECONDB_LOCODE_ALIAS.get(locode, locode)  # → 내부 코드
        name = p.get("name") or ""
        result.append({"locode": locode, "port_name": name, "region": region})

    logger.info("  [%s] catalog: %s ports", region, len(result))
    return result


def fetch_yard_dwell(
    client: httpx.Client,
    region: str,
    today: date,
) -> tuple[list[dict], list[dict]]:
    """
    containers-in-terminal →
      (region_yard_dwell_weekly rows, region_congestion_weekly rows)

    파싱 계약:
      - codes = [s["code"] for s in plot["series"]]  # 공백 locode + Others/min/max
      - port_vals = {normalize_locode(c): row[c]} (Others/min/max 제외)
      - total = sum(port_vals) + Others
      - congestion_index = clamp((total-min)/(max-min)*100, 0, 100)
      - min/max = region 총량의 주별 시즌 밴드 (per-port 밴드 아님)

    데이터 함정:
      - 최신 주 미완성 → is_partial = week_start > cutoff(today-7일)
    """
    url = f"{ECONDB_BASE}/widgets/containers-in-terminal/data/?region={encode_region(region)}"
    data = region_get(client, url)
    if data is None:
        return [], []

    plots = data.get("plots")
    if not isinstance(plots, list) or not plots:
        logger.warning("No plots in containers-in-terminal for %s", region)
        return [], []

    plot = plots[0]
    series = plot.get("series") or []
    # raw codes: ["CN SHA", "CN NGB", ..., "Others", "min", "max"]
    codes: list[str] = [s.get("code", "") for s in series]

    cutoff = latest_complete_cutoff(today)
    now_str = datetime.now(timezone.utc).isoformat()

    yard_rows: list[dict] = []
    congestion_rows: list[dict] = []

    for row in plot.get("data") or []:
        raw_date = row.get("Date")
        if not raw_date:
            continue
        week_start = parse_week_date(str(raw_date))
        if week_start is None:
            continue

        is_partial = week_start > cutoff

        band_min = row.get("min")
        band_max = row.get("max")
        others_raw = row.get("Others")

        # 개별 항만 값 (상위 8개만 시리즈에 포함됨)
        port_vals: dict[str, float] = {}
        for c in codes:
            if not c or c in ("Others", "min", "max"):
                continue
            val = row.get(c)
            if val is None:
                continue
            try:
                norm = normalize_locode(c)
                norm = ECONDB_LOCODE_ALIAS.get(norm, norm)  # → 내부 코드
                port_vals[norm] = float(val)
            except (TypeError, ValueError):
                pass

        # total: 개별 항만 합 + Others (region 독립)
        total: Optional[float] = None
        if port_vals:
            total = sum(port_vals.values())
            if others_raw is not None:
                try:
                    total += float(others_raw)
                except (TypeError, ValueError):
                    pass

        # per-port yard 행
        for locode, dwell_k in port_vals.items():
            yard_rows.append({
                "region": region,
                "week_start": week_start.isoformat(),
                "locode": locode,
                "dwell_teu_k": dwell_k,
                "is_partial": is_partial,
                "updated_at": now_str,
            })

        # Others 행
        if others_raw is not None:
            try:
                yard_rows.append({
                    "region": region,
                    "week_start": week_start.isoformat(),
                    "locode": "OTHERS",
                    "dwell_teu_k": float(others_raw),
                    "is_partial": is_partial,
                    "updated_at": now_str,
                })
            except (TypeError, ValueError):
                pass

        # region 집계 + congestion_index
        congestion_index: Optional[float] = None
        try:
            if (
                band_min is not None
                and band_max is not None
                and total is not None
                and float(band_max) > float(band_min)
            ):
                congestion_index = clamp(
                    (total - float(band_min)) / (float(band_max) - float(band_min)) * 100,
                    0.0, 100.0,
                )
        except (TypeError, ValueError):
            pass

        congestion_rows.append({
            "region": region,
            "week_start": week_start.isoformat(),
            "total_dwell_k": total,
            "band_min_k": float(band_min) if band_min is not None else None,
            "band_max_k": float(band_max) if band_max is not None else None,
            "congestion_index": round(congestion_index, 4) if congestion_index is not None else None,
            "is_partial": is_partial,
            "updated_at": now_str,
        })

    logger.info(
        "  [%s] yard: %s port-week rows, %s region-week rows",
        region, len(yard_rows), len(congestion_rows),
    )
    return yard_rows, congestion_rows


def fetch_omissions(client: httpx.Client, region: str, today: date) -> list[dict]:
    """
    omissions-time-series → region_omissions_weekly rows.

    데이터 함정:
      - 미래 날짜 행 = 예약/announced blank sailing → is_forecast=True, 버리지 말 것.
      - blank_pct = blanked / (blanked + actual) * 100
    """
    url = f"{ECONDB_BASE}/widgets/omissions-time-series/data/?region={encode_region(region)}"
    data = region_get(client, url)
    if data is None:
        return []

    plots = data.get("plots")
    if not isinstance(plots, list) or not plots:
        logger.warning("No plots in omissions-time-series for %s", region)
        return []

    plot = plots[0]
    now_date = today
    now_str = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    for row in plot.get("data") or []:
        raw_date = row.get("Date")
        if not raw_date:
            continue
        week_start = parse_week_date(str(raw_date))
        if week_start is None:
            continue

        is_forecast = week_start > now_date

        blanked = row.get("Blanked capacity (TEU)")
        actual = row.get("Actual capacity")

        blank_pct: Optional[float] = None
        if blanked is not None and actual is not None:
            try:
                b = float(blanked)
                a = float(actual)
                total = b + a
                if total > 0:
                    blank_pct = round(b / total * 100, 4)
            except (TypeError, ValueError):
                pass

        rows.append({
            "region": region,
            "week_start": week_start.isoformat(),
            "blanked_teu": float(blanked) if blanked is not None else None,
            "actual_teu": float(actual) if actual is not None else None,
            "blank_pct": blank_pct,
            "is_forecast": is_forecast,
            "updated_at": now_str,
        })

    logger.info("  [%s] omissions: %s rows", region, len(rows))
    return rows


def fetch_schedule_profile(client: httpx.Client, region: str, today: date) -> list[dict]:
    """
    weekly-schedule-profile → region_schedule_profile rows.

    데이터 함정:
      - 미래 날짜 = 스케줄/예약 → is_forecast=True, 버리지 말 것.
      - yoy_ratio = teu / teu_last_year (0 또는 None이면 None)
    """
    url = (
        f"{ECONDB_BASE}/widgets/weekly-schedule-profile/data/"
        f"?region={encode_region(region)}&metric=TEU&size=ALL"
    )
    data = region_get(client, url)
    if data is None:
        return []

    plots = data.get("plots")
    if not isinstance(plots, list) or not plots:
        logger.warning("No plots in weekly-schedule-profile for %s", region)
        return []

    plot = plots[0]
    now_dt = datetime.now(timezone.utc)

    rows: list[dict] = []
    for row in plot.get("data") or []:
        raw_ts = row.get("Date")
        if not raw_ts:
            continue
        ts = parse_ts(str(raw_ts))
        if ts is None:
            continue

        is_forecast = ts > now_dt

        teu = row.get("TEU")
        teu_ly = row.get("TEU_Last_Year")

        yoy_ratio: Optional[float] = None
        if teu is not None and teu_ly is not None:
            try:
                t = float(teu)
                ly = float(teu_ly)
                if ly > 0:
                    yoy_ratio = round(t / ly, 6)
            except (TypeError, ValueError):
                pass

        rows.append({
            "region": region,
            "ts": ts.isoformat(),
            "teu": float(teu) if teu is not None else None,
            "teu_last_year": float(teu_ly) if teu_ly is not None else None,
            "yoy_ratio": yoy_ratio,
            "is_forecast": is_forecast,
            "updated_at": now_dt.isoformat(),
        })

    logger.info("  [%s] schedule: %s rows", region, len(rows))
    return rows


# ── Supabase 업서트 ──────────────────────────────────────────────────────────

def upsert_catalog(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        supabase.table("econdb_port_catalog").upsert(
            rows, on_conflict="locode,region"
        ).execute()
        logger.info("    Catalog upsert: %s rows", len(rows))
    except Exception as exc:
        logger.error("Catalog upsert failed: %s", exc)


def upsert_yard(
    supabase: Client, yard_rows: list[dict], congestion_rows: list[dict]
) -> None:
    _CHUNK = 500
    if yard_rows:
        try:
            for i in range(0, len(yard_rows), _CHUNK):
                supabase.table("region_yard_dwell_weekly").upsert(
                    yard_rows[i : i + _CHUNK],
                    on_conflict="region,week_start,locode",
                ).execute()
            logger.info("    Yard dwell upsert: %s rows", len(yard_rows))
        except Exception as exc:
            logger.error("Yard dwell upsert failed: %s", exc)

    if congestion_rows:
        try:
            supabase.table("region_congestion_weekly").upsert(
                congestion_rows, on_conflict="region,week_start"
            ).execute()
            logger.info("    Region congestion upsert: %s rows", len(congestion_rows))
        except Exception as exc:
            logger.error("Region congestion upsert failed: %s", exc)


def upsert_omissions(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        supabase.table("region_omissions_weekly").upsert(
            rows, on_conflict="region,week_start"
        ).execute()
        logger.info("    Omissions upsert: %s rows", len(rows))
    except Exception as exc:
        logger.error("Omissions upsert failed: %s", exc)


def upsert_schedule(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    _CHUNK = 500
    try:
        for i in range(0, len(rows), _CHUNK):
            supabase.table("region_schedule_profile").upsert(
                rows[i : i + _CHUNK], on_conflict="region,ts"
            ).execute()
        logger.info("    Schedule profile upsert: %s rows", len(rows))
    except Exception as exc:
        logger.error("Schedule profile upsert failed: %s", exc)


# ── 카탈로그 alias 자동화 (1-4) ──────────────────────────────────────────────

def validate_catalog_coverage(all_catalog: list[dict], watchlist: set[str]) -> None:
    """
    watchlist 항만이 카탈로그에 있는지 검증.
    없으면 port_name 유사도로 후보 제안 → WARNING 로그(ECONDB_LOCODE_ALIAS 자동 수정 금지).
    """
    catalog_locodes = {r["locode"] for r in all_catalog}
    missing = watchlist - catalog_locodes
    if not missing:
        logger.info("Catalog coverage OK: all %s watchlist ports found.", len(watchlist))
        return

    logger.warning(
        "Watchlist ports not found in catalog (%s/%s missing): %s",
        len(missing), len(watchlist), sorted(missing),
    )

    # 항만 이름 유사도로 후보 제안 (country prefix 매칭)
    catalog_by_name: dict[str, str] = {}
    for r in all_catalog:
        name_key = (r.get("port_name") or "").lower().strip()
        if name_key:
            catalog_by_name[name_key] = r["locode"]

    for code in sorted(missing):
        country_prefix = code[:2].upper()
        # 같은 국가 코드로 시작하는 카탈로그 항만 후보
        candidates = [
            (r["locode"], r.get("port_name", ""))
            for r in all_catalog
            if r["locode"].startswith(country_prefix)
        ][:5]
        if candidates:
            logger.warning(
                "  %s 후보 제안(수동 ECONDB_LOCODE_ALIAS 추가 필요): %s",
                code, candidates,
            )
        else:
            logger.warning("  %s: 카탈로그에서 동일 국가 항만 없음.", code)

    # ECONDB_LOCODE_ALIAS 자동 덮어쓰기 금지 — 제안만 로그


def detect_duplicate_regions(all_catalog: list[dict]) -> None:
    """
    같은 locode가 여러 region에 있으면
    항만 수가 적은(더 구체적) region을 primary로 제안 → WARNING 로그.
    """
    from collections import Counter

    region_port_count: Counter = Counter()
    locode_regions: dict[str, list[str]] = defaultdict(list)

    for r in all_catalog:
        locode_regions[r["locode"]].append(r["region"])
        region_port_count[r["region"]] += 1

    for locode, regions in locode_regions.items():
        if len(regions) <= 1:
            continue
        primary = min(regions, key=lambda reg: region_port_count.get(reg, 999))
        logger.warning(
            "Locode %s in multiple regions %s → suggested primary: %s (smallest)",
            locode, regions, primary,
        )


# ── Phase 2: 파생 지표 ──────────────────────────────────────────────────────

def compute_region_index(
    region: str,
    congestion_rows: list[dict],
    omission_rows: list[dict],
    schedule_rows: list[dict],
    today: date,
) -> Optional[dict]:
    """
    region_index 1행 계산 (최신 완전주 기준).
    데이터 부족 시 None 반환(날조 금지).

    지표 설명:
      congestion_index       — 최신 완전주 clamp((total-min)/(max-min)*100, 0, 100)
      congestion_trend_pct   — (이번주 total - 전주 total) / 전주 total * 100
      blank_sailing_pct      — 최신 완전주 blank_pct
      blank_sailing_trend_pp — 이번주 blank_pct - 전주 blank_pct (%p)
      blank_sailing_fwd_pct  — is_forecast=True 행 blank_pct 평균(announced 전망)
      schedule_yoy_pct       — 직전 48h 비예측 구간 mean(yoy_ratio-1)*100

    freight_pressure / delay_risk: 룰 기반, 임계값 = 초기 휴리스틱(보정 필요)
    narrative: 위 숫자만으로 만드는 템플릿 (LLM/날조 금지)
    """
    cutoff = latest_complete_cutoff(today)
    now_dt = datetime.now(timezone.utc)

    # ── 야드 적체 ────────────────────────────────────────────────────
    complete_cong = sorted(
        [
            r for r in congestion_rows
            if not r.get("is_partial", True)
            and r.get("congestion_index") is not None
        ],
        key=lambda r: r["week_start"],
    )

    congestion_index: Optional[float] = None
    congestion_trend_pct: Optional[float] = None
    data_week: Optional[str] = None

    if complete_cong:
        latest_c = complete_cong[-1]
        congestion_index = latest_c.get("congestion_index")
        data_week = latest_c["week_start"]
        if len(complete_cong) >= 2:
            prev_c = complete_cong[-2]
            prev_total = prev_c.get("total_dwell_k")
            curr_total = latest_c.get("total_dwell_k")
            if prev_total and curr_total and float(prev_total) > 0:
                congestion_trend_pct = round(
                    (float(curr_total) - float(prev_total)) / float(prev_total) * 100, 2
                )

    # ── 블랭크세일링 ─────────────────────────────────────────────────
    complete_omit = sorted(
        [
            r for r in omission_rows
            if not r.get("is_forecast", True)
            and r.get("blank_pct") is not None
            and r["week_start"] <= cutoff.isoformat()  # 완전주만
        ],
        key=lambda r: r["week_start"],
    )

    blank_sailing_pct: Optional[float] = None
    blank_sailing_trend_pp: Optional[float] = None

    if complete_omit:
        latest_o = complete_omit[-1]
        blank_sailing_pct = latest_o.get("blank_pct")
        if len(complete_omit) >= 2:
            prev_bp = complete_omit[-2].get("blank_pct")
            if blank_sailing_pct is not None and prev_bp is not None:
                blank_sailing_trend_pp = round(float(blank_sailing_pct) - float(prev_bp), 4)

    # 향후 blank_pct 평균 (announced blank sailing 전망)
    fwd_omit = [
        r for r in omission_rows
        if r.get("is_forecast", False) and r.get("blank_pct") is not None
    ]
    blank_sailing_fwd_pct = mean_vals([r["blank_pct"] for r in fwd_omit])
    if blank_sailing_fwd_pct is not None:
        blank_sailing_fwd_pct = round(blank_sailing_fwd_pct, 4)

    # ── 스케줄 YoY ──────────────────────────────────────────────────
    # 직전 48h 비예측 구간 mean(yoy_ratio - 1) * 100
    cutoff_48h = now_dt - timedelta(hours=48)
    recent_sched = [
        r for r in schedule_rows
        if not r.get("is_forecast", True)
        and r.get("yoy_ratio") is not None
        and parse_ts(r["ts"]) is not None
        and parse_ts(r["ts"]) >= cutoff_48h  # type: ignore[operator]
    ]

    # 48h 내 데이터가 없으면 가장 최근 비예측 행 사용 (fallback)
    if not recent_sched:
        all_complete_sched = sorted(
            [r for r in schedule_rows if not r.get("is_forecast", True) and r.get("yoy_ratio") is not None],
            key=lambda r: r["ts"],
        )
        if all_complete_sched:
            recent_sched = [all_complete_sched[-1]]

    schedule_yoy_pct: Optional[float] = None
    if recent_sched:
        yoy_ratios = [r["yoy_ratio"] for r in recent_sched if r.get("yoy_ratio") is not None]
        m = mean_vals(yoy_ratios)
        if m is not None:
            schedule_yoy_pct = round((m - 1) * 100, 4)

    # ── 지표 (룰 기반, 예측 모델 아님) ─────────────────────────────
    # 초기 휴리스틱, 운영 데이터로 보정 필요
    freight_pressure = "neutral"
    if blank_sailing_pct is not None and blank_sailing_trend_pp is not None:
        if float(blank_sailing_pct) >= BLANK_PCT_HIGH and float(blank_sailing_trend_pp) > 0:
            freight_pressure = "building"
        elif float(blank_sailing_pct) <= BLANK_PCT_LOW and float(blank_sailing_trend_pp) < 0:
            freight_pressure = "easing"

    delay_risk = "low"
    ci_val = float(congestion_index) if congestion_index is not None else 0.0
    bp_val = float(blank_sailing_pct) if blank_sailing_pct is not None else 0.0
    if congestion_index is not None or blank_sailing_pct is not None:
        if ci_val >= CONGESTION_HIGH or bp_val >= 20.0:
            delay_risk = "elevated"
        elif ci_val >= CONGESTION_MODERATE or bp_val >= 10.0:
            delay_risk = "moderate"

    # ── 내러티브 (룰 기반 템플릿, LLM/날조 금지) ────────────────────
    ci = congestion_index or 0.0
    trend = congestion_trend_pct or 0.0
    bp = blank_sailing_pct or 0.0
    tp = blank_sailing_trend_pp or 0.0
    fwd = blank_sailing_fwd_pct or 0.0
    yoy = schedule_yoy_pct or 0.0

    narrative_ko = (
        f"{region}: 야드 적체 {ci:.0f}/100(전주 {trend:+.0f}%), "
        f"블랭크세일링 {bp:.1f}%({tp:+.1f}%p, 향후 {fwd:.1f}%), "
        f"선석 점유 전년비 {yoy:+.1f}%."
    )
    narrative_en = (
        f"{region}: Yard congestion {ci:.0f}/100 (prev week {trend:+.0f}%), "
        f"blank sailings {bp:.1f}% ({tp:+.1f}%p, fwd {fwd:.1f}%), "
        f"berth occupancy YoY {yoy:+.1f}%."
    )

    if congestion_index is None and blank_sailing_pct is None:
        logger.warning(
            "[%s] Insufficient data for region_index (no congestion/omissions). Skipping.",
            region,
        )
        return None

    return {
        "region": region,
        "congestion_index": round(float(congestion_index), 4) if congestion_index is not None else None,
        "congestion_trend_pct": congestion_trend_pct,
        "blank_sailing_pct": blank_sailing_pct,
        "blank_sailing_trend_pp": blank_sailing_trend_pp,
        "blank_sailing_fwd_pct": blank_sailing_fwd_pct,
        "schedule_yoy_pct": schedule_yoy_pct,
        "freight_pressure": freight_pressure,
        "delay_risk": delay_risk,
        "narrative_ko": narrative_ko,
        "narrative_en": narrative_en,
        "data_week": data_week,
        "updated_at": now_dt.isoformat(),
    }


def compute_yard_congestion_per_port(
    port_yard_history: dict[str, list[dict]],
    today: date,
) -> dict[str, dict]:
    """
    각 항만(containers-in-terminal 상위 8개)의 야드 적체 지수 계산.
    항만 자기 이력의 백분위(0–100) 사용 (region 밴드 아님).

    Args:
        port_yard_history: {locode: [{"week_start": str, "dwell_teu_k": float}, ...]}
        today: UTC 오늘 날짜

    Returns:
        {locode: {"yard_congestion_index": float, "yard_congestion_at": str}}

    비상위 항만(widget에 없는 항만)은 포함되지 않음 → port_snapshots는 NULL 유지.
    """
    cutoff = latest_complete_cutoff(today)
    result: dict[str, dict] = {}

    for locode, rows in port_yard_history.items():
        if locode == "OTHERS":
            continue

        valid_rows = sorted(
            [r for r in rows if r.get("dwell_teu_k") is not None],
            key=lambda r: r["week_start"],
        )
        if not valid_rows:
            continue

        # 최신 완전주 값
        complete_rows = [r for r in valid_rows if r["week_start"] <= cutoff.isoformat()]
        if not complete_rows:
            logger.warning("  [port=%s] No complete weeks for yard percentile. Skipping.", locode)
            continue

        latest_val = float(complete_rows[-1]["dwell_teu_k"])
        all_vals = [float(r["dwell_teu_k"]) for r in valid_rows]  # 자기 이력 전체

        below_eq = sum(1 for v in all_vals if v <= latest_val)
        percentile = clamp(round(below_eq / len(all_vals) * 100, 1), 0.0, 100.0)

        result[locode] = {
            "yard_congestion_index": percentile,
            "yard_congestion_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "  [port=%s] yard_congestion_index=%.1f (n_history=%s, latest_val=%.3f)",
            locode, percentile, len(all_vals), latest_val,
        )

    return result


# ── 메인 ────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("fetch_econdb_regions.py starting (regions=%s)...", len(REGIONS))

    supabase = get_supabase()
    client = make_region_client()
    today = today_utc()

    # watchlist (alias 검증용, 없어도 수집은 진행)
    watchlist: set[str] = set()
    try:
        from fetch_econdb import DEFAULT_WATCHLIST  # type: ignore[import]
        watchlist = set(DEFAULT_WATCHLIST)
        logger.info("Loaded watchlist: %s ports", len(watchlist))
    except Exception as exc:
        logger.warning(
            "Could not import DEFAULT_WATCHLIST from fetch_econdb (%s); "
            "skipping alias coverage check.",
            exc,
        )

    all_catalog: list[dict] = []
    # per-port 야드 이력: {locode: [{"week_start": str, "dwell_teu_k": float}]}
    port_yard_history: dict[str, list[dict]] = defaultdict(list)

    # region_index 계산용 in-memory 저장
    region_congestion_map: dict[str, list[dict]] = {}
    region_omission_map: dict[str, list[dict]] = {}
    region_schedule_map: dict[str, list[dict]] = {}

    try:
        for region in REGIONS:
            logger.info("── Processing region: %s", region)

            # 1. 카탈로그
            catalog_rows = fetch_port_catalog(client, region)
            all_catalog.extend(catalog_rows)
            upsert_catalog(supabase, catalog_rows)
            time.sleep(0.5)

            # 2. 야드 적체
            yard_rows, congestion_rows = fetch_yard_dwell(client, region, today)
            region_congestion_map[region] = congestion_rows

            # per-port 야드 이력 집계 (상위 8개)
            for r in yard_rows:
                if r["locode"] != "OTHERS" and r.get("dwell_teu_k") is not None:
                    port_yard_history[r["locode"]].append({
                        "week_start": r["week_start"],
                        "dwell_teu_k": r["dwell_teu_k"],
                    })

            upsert_yard(supabase, yard_rows, congestion_rows)
            time.sleep(0.5)

            # 3. 블랭크세일링
            omission_rows = fetch_omissions(client, region, today)
            region_omission_map[region] = omission_rows
            upsert_omissions(supabase, omission_rows)
            time.sleep(0.5)

            # 4. 스케줄 프로파일
            schedule_rows = fetch_schedule_profile(client, region, today)
            region_schedule_map[region] = schedule_rows
            upsert_schedule(supabase, schedule_rows)
            time.sleep(0.5)

    finally:
        try:
            client.close()
        except Exception:
            pass

    # ── 카탈로그 alias 자동화 (1-4) ─────────────────────────────────────────
    if watchlist:
        validate_catalog_coverage(all_catalog, watchlist)
    detect_duplicate_regions(all_catalog)

    # ── Phase 2: region_index ─────────────────────────────────────────────
    logger.info("Computing region_index for %s regions...", len(REGIONS))
    region_index_rows: list[dict] = []

    for region in REGIONS:
        cong_rows = region_congestion_map.get(region, [])
        omit_rows = region_omission_map.get(region, [])
        sched_rows = region_schedule_map.get(region, [])

        idx = compute_region_index(region, cong_rows, omit_rows, sched_rows, today)
        if idx is not None:
            region_index_rows.append(idx)
            logger.info(
                "[%s] ci=%.1f trend=%s bp=%s fwd=%s yoy=%s | %s / %s",
                region,
                idx.get("congestion_index") or 0.0,
                idx.get("congestion_trend_pct"),
                idx.get("blank_sailing_pct"),
                idx.get("blank_sailing_fwd_pct"),
                idx.get("schedule_yoy_pct"),
                idx.get("freight_pressure"),
                idx.get("delay_risk"),
            )

    if region_index_rows:
        try:
            supabase.table("region_index").upsert(
                region_index_rows, on_conflict="region"
            ).execute()
            logger.info("region_index upsert: %s regions", len(region_index_rows))
        except Exception as exc:
            logger.error("region_index upsert failed: %s", exc)

    # ── Phase 2: per-port 야드 지수 (상위 8개 항만) ────────────────────────
    logger.info(
        "Computing per-port yard congestion index (%s unique ports)...",
        len([k for k in port_yard_history if k != "OTHERS"]),
    )
    yard_per_port = compute_yard_congestion_per_port(port_yard_history, today)

    updated_ports = 0
    for locode, yard_data in yard_per_port.items():
        try:
            supabase.table("port_snapshots").update(yard_data).eq(
                "port_code", locode
            ).execute()
            updated_ports += 1
        except Exception as exc:
            logger.error("Failed to update port_snapshots yard for %s: %s", locode, exc)

    logger.info(
        "fetch_econdb_regions.py completed. "
        "regions=%s region_index=%s port_yard_updates=%s",
        len(REGIONS), len(region_index_rows), updated_ports,
    )


if __name__ == "__main__":
    main()
