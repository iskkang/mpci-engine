"""
MPCI Engine — EconDB 해협 통과량 + 파나마 수위 수집기
GitHub Actions: 일 1회 실행

수집 대상:
  - chokepoints_list: 해협 목록 (Suez/Panama/Cape/Malacca/Bosphorus/Hormuz)
  - chokepoint-pass: 방향별 TEU 주간 통과량 (unit=teu, group_by=direction)
  - chokepoint-water-level: 파나마 운하 일별 수위
  - chokepoint_index: 파생 집계 (pct_of_normal, trend, status, narrative)

데이터 함정:
  1. 부분주: max(week_start) <= today-7인 것만 완전주(is_partial=false) 처리
  2. 기준선: 고정 참조기간 REFERENCE_MEDIAN (trailing 기준선 사용 금지)
  3. 수위 미래 null: 최신 non-null "Current year" 값만 사용
  4. status/draft_status: 룰 기반 (예측·지정학 추측 금지) + "보정 필요" 상수

가드레일:
  - 응답 스키마 불일치 → 로그+스킵, 값 날조/추정 채움 금지
  - on_conflict PK 기준 멱등 upsert
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ── sys.path 설정 (standalone & module 모두 지원) ──────────────────────────
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from supabase import create_client, Client
from _econdb_http import ECONDB_BASE, make_region_client, region_get

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 고정 참조기간: 2022-03-07 ~ 2023-11-30 (홍해 사태 이전 정상기간) ──────
# 실제 EconDB 이력으로 계산한 주간 total TEU 중앙값 (초기 휴리스틱 — 운영 데이터로 보정 필요)
REFERENCE_MEDIAN: dict[str, float] = {
    "Suez":              198_000,
    "Panama":            128_000,
    "Cape of Good Hope":  52_000,
    "Malacca":           820_000,
    "Turkish Straits":    88_000,  # Bosphorus
    "Hormuz":             96_000,
}

# EconDB 응답 해협 이름 → REFERENCE_MEDIAN 키 (대소문자 불일치 대응)
_CP_ALIAS: dict[str, str] = {
    "suez":                 "Suez",
    "suez canal":           "Suez",
    "panama":               "Panama",
    "panama canal":         "Panama",
    "cape":                 "Cape of Good Hope",
    "cape of good hope":    "Cape of Good Hope",
    "malacca":              "Malacca",
    "malacca strait":       "Malacca",
    "bosphorus":            "Turkish Straits",
    "turkish straits":      "Turkish Straits",
    "hormuz":               "Hormuz",
    "strait of hormuz":     "Hormuz",
}

# 상태 임계값 (초기 휴리스틱 — 보정 필요)
_STATUS_SEVERE   = 60.0
_STATUS_ELEVATED = 80.0


# ── Supabase ───────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


# ── EconDB 응답 파싱 ────────────────────────────────────────────────────────
def _ref_key(cp_name: str) -> str:
    return _CP_ALIAS.get(cp_name.strip().lower(), cp_name.strip())


def fetch_chokepoint_list(client) -> list[dict]:
    url = f"{ECONDB_BASE}/maritime/chokepoints_list/"
    resp = region_get(client, url)
    if not resp:
        return []
    chokepoints = resp.get("chokepoints", [])
    if not isinstance(chokepoints, list):
        logger.warning("Unexpected chokepoints_list schema")
        return []
    logger.info("chokepoints_list: %s entries", len(chokepoints))
    return chokepoints


def fetch_pass_data(client, chokepoint_name: str) -> list[dict]:
    url = (
        f"{ECONDB_BASE}/widgets/chokepoint-pass/data/"
        f"?unit=teu&group_by=direction&chokepoint_name={chokepoint_name}"
    )
    resp = region_get(client, url)
    if not resp:
        return []
    plots = resp.get("plots") or []
    if not plots:
        return []
    plot = plots[0] if isinstance(plots[0], dict) else {}
    data = plot.get("data") or []
    return data if isinstance(data, list) else []


def fetch_water_level(client) -> list[dict]:
    url = f"{ECONDB_BASE}/widgets/chokepoint-water-level/data/"
    resp = region_get(client, url)
    if not resp:
        return []
    plots = resp.get("plots") or []
    if not plots:
        return []
    plot = plots[0] if isinstance(plots[0], dict) else {}
    data = plot.get("data") or []
    return data if isinstance(data, list) else []


def parse_pass_rows(
    raw: list[dict], today: date
) -> tuple[list[dict], list[dict]]:
    """
    EconDB chokepoint-pass rows 파싱.
    완전주 판별: week_start <= today - 7 → is_partial = False
    반환: (complete_rows, all_rows) 모두 week_start 내림차순
    """
    cutoff = today - timedelta(days=7)
    rows: list[dict] = []

    for row in raw:
        d = row.get("Date")
        if not d:
            continue
        try:
            week_start = date.fromisoformat(str(d)[:10])
        except ValueError:
            logger.warning("Unparseable week_start: %s", d)
            continue

        n = row.get("N")
        s = row.get("S")
        total: Optional[float] = None
        if n is not None or s is not None:
            total = float(n or 0) + float(s or 0)

        rows.append(
            {
                "week_start": week_start,
                "teu_north":  float(n) if n is not None else None,
                "teu_south":  float(s) if s is not None else None,
                "teu_total":  total,
                "is_partial": week_start > cutoff,
            }
        )

    rows.sort(key=lambda x: x["week_start"], reverse=True)
    complete = [r for r in rows if not r["is_partial"]]
    return complete, rows


def parse_water_level_rows(raw: list[dict]) -> list[dict]:
    """파나마 수위 파싱. obs_date 내림차순 반환."""
    rows: list[dict] = []
    for row in raw:
        d = row.get("Date")
        if not d:
            continue
        try:
            obs_date = date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        cur = row.get("Current year")   # 미래=null
        avg = row.get("Average")
        rng = row.get("range") or []
        lo  = rng[0] if len(rng) > 0 else None
        hi  = rng[1] if len(rng) > 1 else None
        rows.append(
            {
                "obs_date":         obs_date,
                "level_current":    float(cur) if cur is not None else None,
                "level_avg":        float(avg) if avg is not None else None,
                "level_range_low":  float(lo)  if lo  is not None else None,
                "level_range_high": float(hi)  if hi  is not None else None,
            }
        )
    rows.sort(key=lambda x: x["obs_date"], reverse=True)
    return rows


# ── 내러티브 생성 (룰 기반, LLM/날조 금지) ─────────────────────────────────
def _narrative(
    cp_name: str,
    pct: Optional[float],
    trend: Optional[float],
    status: str,
    lang: str,
) -> str:
    if pct is None:
        return "수집 중" if lang == "ko" else "Collecting data"

    pct_r = round(pct, 1)
    trend_str = ""
    if trend is not None:
        sign = "+" if trend >= 0 else ""
        if lang == "ko":
            trend_str = f" (전주 대비 {sign}{round(trend,1)}%)"
        else:
            trend_str = f" ({sign}{round(trend,1)}% vs prev week)"

    if lang == "ko":
        if status == "severe":
            return (
                f"{cp_name} 통과량이 정상 대비 {pct_r}% 수준으로 심각하게 감소했습니다"
                f"{trend_str}. 항로 우회 가능성이 높습니다. (임계값 보정 필요)"
            )
        elif status == "elevated":
            return (
                f"{cp_name} 통과량이 정상 대비 {pct_r}% 수준으로 감소했습니다"
                f"{trend_str}. 지속 모니터링이 필요합니다. (임계값 보정 필요)"
            )
        else:
            return (
                f"{cp_name} 통과량이 정상 대비 {pct_r}% 수준입니다"
                f"{trend_str}. 현재 정상 범위입니다. (임계값 보정 필요)"
            )
    else:
        if status == "severe":
            return (
                f"{cp_name} transit volume is at {pct_r}% of normal, severely reduced"
                f"{trend_str}. Rerouting risk is high. (threshold subject to calibration)"
            )
        elif status == "elevated":
            return (
                f"{cp_name} transit volume is at {pct_r}% of normal, below average"
                f"{trend_str}. Continued monitoring advised. (threshold subject to calibration)"
            )
        else:
            return (
                f"{cp_name} transit volume is at {pct_r}% of normal"
                f"{trend_str}. Currently within normal range. (threshold subject to calibration)"
            )


# ── Supabase upsert ────────────────────────────────────────────────────────
def upsert_catalog(supabase: Client, chokepoints: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for cp in chokepoints:
        name = (cp.get("name") or "").strip()
        cid  = cp.get("id")
        if not name:
            continue
        rows.append(
            {
                "chokepoint":  name,
                "econdb_id":   int(cid) if cid is not None else None,
                "updated_at":  now,
            }
        )
    if not rows:
        return
    supabase.table("chokepoint_catalog").upsert(rows, on_conflict="chokepoint").execute()
    logger.info("Upserted %s chokepoint_catalog rows", len(rows))


def upsert_pass_weekly(
    supabase: Client, chokepoint: str, rows: list[dict]
) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = [
        {
            "chokepoint":  chokepoint,
            "week_start":  r["week_start"].isoformat(),
            "teu_north":   r["teu_north"],
            "teu_south":   r["teu_south"],
            "teu_total":   r["teu_total"],
            "is_partial":  r["is_partial"],
            "updated_at":  now,
        }
        for r in rows
    ]
    supabase.table("chokepoint_pass_weekly").upsert(
        records, on_conflict="chokepoint,week_start"
    ).execute()
    logger.info(
        "Upserted %s chokepoint_pass_weekly rows for %s", len(records), chokepoint
    )


def upsert_water_level(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = [
        {
            "chokepoint":        "Panama",
            "obs_date":          r["obs_date"].isoformat(),
            "level_current":     r["level_current"],
            "level_avg":         r["level_avg"],
            "level_range_low":   r["level_range_low"],
            "level_range_high":  r["level_range_high"],
            "updated_at":        now,
        }
        for r in rows
    ]
    supabase.table("chokepoint_water_level").upsert(
        records, on_conflict="chokepoint,obs_date"
    ).execute()
    logger.info("Upserted %s chokepoint_water_level rows", len(records))


# ── Phase 2: chokepoint_index 파생 ─────────────────────────────────────────
def compute_and_upsert_index(
    supabase: Client,
    cp_name: str,
    complete_rows: list[dict],
    water_rows: list[dict],
) -> None:
    """
    최신 완전주 기반으로 chokepoint_index 1행 생성·upsert.
    파나마 수위: 최신 non-null "Current year" 값 사용.
    """
    now = datetime.now(timezone.utc).isoformat()

    if not complete_rows:
        logger.warning("No complete rows for %s — skipping index", cp_name)
        return

    latest = complete_rows[0]
    prev   = complete_rows[1] if len(complete_rows) > 1 else None

    teu_total  = latest["teu_total"]
    teu_north  = latest["teu_north"]
    teu_south  = latest["teu_south"]
    data_week  = latest["week_start"].isoformat()

    # 정상 대비 % — 고정 참조기간 중앙값 기준
    ref_key    = _ref_key(cp_name)
    ref_median = REFERENCE_MEDIAN.get(ref_key)
    pct_of_normal: Optional[float] = None
    if teu_total is not None and ref_median and ref_median > 0:
        pct_of_normal = round(teu_total / ref_median * 100.0, 1)

    # 전주 추세 %
    trend_pct: Optional[float] = None
    if (
        prev is not None
        and prev["teu_total"] is not None
        and teu_total is not None
        and (prev["teu_total"] or 0) > 0
    ):
        trend_pct = round(
            (teu_total - prev["teu_total"]) / prev["teu_total"] * 100.0, 1
        )

    # 상태 (룰 기반)
    if pct_of_normal is None:
        status = "unknown"
    elif pct_of_normal < _STATUS_SEVERE:
        status = "severe"
    elif pct_of_normal < _STATUS_ELEVATED:
        status = "elevated"
    else:
        status = "normal"

    # 파나마 수위
    water_level = water_level_avg = water_level_low = water_level_high = None
    draft_status: Optional[str] = None
    if ref_key == "Panama" and water_rows:
        for wr in water_rows:
            if wr["level_current"] is not None:
                water_level      = wr["level_current"]
                water_level_avg  = wr["level_avg"]
                water_level_low  = wr["level_range_low"]
                water_level_high = wr["level_range_high"]
                break
        if water_level is not None and water_level_low is not None:
            draft_status = "low" if water_level < water_level_low else "normal"

    row = {
        "chokepoint":       cp_name,
        "teu_total":        teu_total,
        "teu_north":        teu_north,
        "teu_south":        teu_south,
        "pct_of_normal":    pct_of_normal,
        "trend_pct":        trend_pct,
        "status":           status,
        "water_level":      water_level,
        "water_level_avg":  water_level_avg,
        "water_level_low":  water_level_low,
        "water_level_high": water_level_high,
        "draft_status":     draft_status,
        "narrative_ko":     _narrative(cp_name, pct_of_normal, trend_pct, status, "ko"),
        "narrative_en":     _narrative(cp_name, pct_of_normal, trend_pct, status, "en"),
        "data_week":        data_week,
        "updated_at":       now,
    }
    supabase.table("chokepoint_index").upsert([row], on_conflict="chokepoint").execute()
    logger.info(
        "Upserted chokepoint_index: %s pct=%.1f status=%s",
        cp_name, pct_of_normal or 0, status,
    )


# ── main ────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("fetch_econdb_chokepoints.py starting...")
    today = datetime.now(timezone.utc).date()

    client = make_region_client()
    try:
        # 1. 해협 목록
        chokepoints = fetch_chokepoint_list(client)
        if not chokepoints:
            logger.error("No chokepoints fetched — aborting")
            return

        supabase = get_supabase()
        upsert_catalog(supabase, chokepoints)

        # 2. 파나마 수위 (공통 — 해협 루프 전 미리 수집)
        water_raw  = fetch_water_level(client)
        water_rows = parse_water_level_rows(water_raw)
        if water_rows:
            upsert_water_level(supabase, water_rows)
            latest_nonnull = next(
                (r["obs_date"] for r in water_rows if r["level_current"] is not None),
                None,
            )
            logger.info(
                "Water level rows: %s (latest non-null: %s)",
                len(water_rows), latest_nonnull,
            )

        # 3. 해협별 주간 통과량 + index 파생
        for cp in chokepoints:
            cp_name = (cp.get("name") or "").strip()
            if not cp_name:
                continue

            raw = fetch_pass_data(client, cp_name)
            if not raw:
                logger.warning("No pass data for %s — skip", cp_name)
                continue

            complete_rows, all_rows = parse_pass_rows(raw, today)
            logger.info(
                "%s: %s total rows, %s complete weeks",
                cp_name, len(all_rows), len(complete_rows),
            )

            upsert_pass_weekly(supabase, cp_name, all_rows)

            # Phase 2: chokepoint_index 파생 (완전주만 사용)
            # 파나마만 water_rows 전달, 나머지는 빈 리스트 → 수위 필드 null
            is_panama = _ref_key(cp_name) == "Panama"
            compute_and_upsert_index(
                supabase, cp_name, complete_rows,
                water_rows if is_panama else [],
            )

    finally:
        try:
            client.close()
        except Exception:
            pass

    logger.info("fetch_econdb_chokepoints.py completed successfully.")


if __name__ == "__main__":
    main()
