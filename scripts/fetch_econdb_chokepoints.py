"""
MPCI Engine — EconDB 해협 통과량 + 파나마 수위 수집기
GitHub Actions: 일 1회 실행

수집 대상:
  - chokepoints_list: 해협 목록 (Suez/Panama/Cape/Malacca/Bosphorus/Hormuz)
  - chokepoint-pass: 방향별 TEU 주간 통과량 (unit=teu, group_by=direction)
      → series 코드를 동적으로 읽음 (N/S·E/W 등 해협마다 다름)
  - chokepoint-water-level: 파나마 운하 일별 수위
  - latest_crossings: 최근 통과 컨테이너선 목록
  - chokepoint_index: 파생 집계 (pct_of_normal, status, narrative)

데이터 함정:
  1. 부분주: max(week_start) <= today-7인 것만 완전주(is_partial=false) 처리
  2. 기준선: 고정 참조기간 REFERENCE_MEDIAN (trailing 기준선 사용 금지)
  3. 수위 미래 null: 최신 non-null "Current year" 값만 사용
  4. 방향 코드: series[].code 에서 동적 추출 — N/S 하드코딩 금지

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


def fetch_pass_plot(client, chokepoint_name: str) -> dict | None:
    """
    chokepoint-pass 전체 plot dict 반환.
    series 코드(N/S·E/W 등)를 읽기 위해 plot 전체를 반환한다.
    """
    url = (
        f"{ECONDB_BASE}/widgets/chokepoint-pass/data/"
        f"?unit=teu&group_by=direction&chokepoint_name={chokepoint_name}"
    )
    resp = region_get(client, url)
    if not resp:
        return None
    plots = resp.get("plots") or []
    if not plots:
        return None
    plot = plots[0] if isinstance(plots[0], dict) else None
    return plot


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


def fetch_latest_crossings(client, chokepoint_name: str) -> list[dict]:
    """최근 통과 컨테이너선 목록 (EconDB latest_crossings)."""
    url = (
        f"{ECONDB_BASE}/maritime/latest_crossings/"
        f"?chokepoint_name={chokepoint_name}"
    )
    resp = region_get(client, url)
    if not resp:
        return []
    data = resp.get("data") or []
    return data if isinstance(data, list) else []


def extract_series_codes(plot: dict) -> tuple[str, str] | None:
    """
    plot["series"] 에서 방향 코드 2개를 동적 추출.
    예: [{"code":"N",...},{"code":"S",...}] → ("N","S")
        [{"code":"E",...},{"code":"W",...}] → ("E","W")
    시리즈가 2개 미만이면 None 반환 (스킵 신호).
    """
    series = plot.get("series") or []
    codes = []
    for s in series:
        if isinstance(s, dict) and s.get("code"):
            codes.append(str(s["code"]))
    if len(codes) < 2:
        return None
    return codes[0], codes[1]


def parse_pass_rows(
    plot: dict,
    dir_a: str,
    dir_b: str,
    today: date,
) -> tuple[list[dict], list[dict]]:
    """
    EconDB chokepoint-pass plot 파싱.
    dir_a / dir_b: 동적으로 추출한 시리즈 코드 (예: "N","S" 또는 "E","W").
    완전주 판별: week_start <= today - 7 → is_partial = False
    반환: (complete_rows, all_rows) 모두 week_start 내림차순
    """
    cutoff = today - timedelta(days=7)
    raw = plot.get("data") or []
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

        teu_a = row.get(dir_a)
        teu_b = row.get(dir_b)
        total: Optional[float] = None
        if teu_a is not None or teu_b is not None:
            total = float(teu_a or 0) + float(teu_b or 0)

        rows.append(
            {
                "week_start":  week_start,
                "dir_a_code":  dir_a,
                "dir_b_code":  dir_b,
                "teu_dir_a":   float(teu_a) if teu_a is not None else None,
                "teu_dir_b":   float(teu_b) if teu_b is not None else None,
                # 하위 호환: N/S 해협이면 teu_north/teu_south도 채움
                "teu_north":   float(teu_a) if dir_a == "N" and teu_a is not None else None,
                "teu_south":   float(teu_b) if dir_b == "S" and teu_b is not None else None,
                "teu_total":   total,
                "is_partial":  week_start > cutoff,
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
            "dir_a_code":  r["dir_a_code"],
            "dir_b_code":  r["dir_b_code"],
            "teu_dir_a":   r["teu_dir_a"],
            "teu_dir_b":   r["teu_dir_b"],
            "teu_north":   r["teu_north"],   # 하위 호환 (N/S 해협)
            "teu_south":   r["teu_south"],   # 하위 호환 (N/S 해협)
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


def upsert_latest_crossings(
    supabase: Client, chokepoint: str, rows: list[dict]
) -> None:
    """
    chokepoint_latest_crossings upsert.
    teu는 int() 캐스팅 (부동소수 방지).
    """
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for r in rows:
        mmsi = r.get("mmsi")
        start_date = r.get("start_date")
        if not mmsi or not start_date:
            continue
        try:
            mmsi_int = int(mmsi)
        except (TypeError, ValueError):
            logger.warning("latest_crossings: invalid mmsi=%s — skip", mmsi)
            continue
        teu_raw = r.get("teu")
        teu_int = None
        if teu_raw is not None:
            try:
                teu_int = int(teu_raw)
            except (TypeError, ValueError):
                teu_int = None
        records.append(
            {
                "chokepoint":  chokepoint,
                "mmsi":        mmsi_int,
                "start_date":  str(start_date),   # timestamptz — Postgres가 파싱
                "name":        r.get("name"),
                "teu":         teu_int,
                "direction":   r.get("direction"),
                "operator":    r.get("operator"),
                "country":     r.get("country"),
                "updated_at":  now,
            }
        )
    if not records:
        return
    supabase.table("chokepoint_latest_crossings").upsert(
        records, on_conflict="chokepoint,mmsi,start_date"
    ).execute()
    logger.info(
        "Upserted %s chokepoint_latest_crossings rows for %s",
        len(records), chokepoint,
    )


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
    dir_a_code = latest["dir_a_code"]
    dir_b_code = latest["dir_b_code"]
    teu_dir_a  = latest["teu_dir_a"]
    teu_dir_b  = latest["teu_dir_b"]
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
        "dir_a_code":       dir_a_code,
        "dir_b_code":       dir_b_code,
        "teu_dir_a":        teu_dir_a,
        "teu_dir_b":        teu_dir_b,
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
        "Upserted chokepoint_index: %s dir=%s/%s pct=%.1f status=%s",
        cp_name, dir_a_code, dir_b_code, pct_of_normal or 0, status,
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

        # 3. 해협별 주간 통과량 + latest_crossings + index 파생
        for cp in chokepoints:
            cp_name = (cp.get("name") or "").strip()
            if not cp_name:
                continue

            # ── chokepoint-pass ─────────────────────────────────────────────
            plot = fetch_pass_plot(client, cp_name)
            if not plot:
                logger.warning("No pass plot for %s — skip", cp_name)
                continue

            codes = extract_series_codes(plot)
            if codes is None:
                logger.warning(
                    "%s: series 코드 < 2 → pass 스킵 (응답 확인 필요)", cp_name
                )
                continue

            dir_a, dir_b = codes
            logger.info("%s: series codes = [%s, %s]", cp_name, dir_a, dir_b)

            complete_rows, all_rows = parse_pass_rows(plot, dir_a, dir_b, today)
            logger.info(
                "%s: %s total rows, %s complete weeks",
                cp_name, len(all_rows), len(complete_rows),
            )

            upsert_pass_weekly(supabase, cp_name, all_rows)

            # ── latest_crossings (버그2 추가) ───────────────────────────────
            lc_rows = fetch_latest_crossings(client, cp_name)
            if lc_rows:
                upsert_latest_crossings(supabase, cp_name, lc_rows)
            else:
                logger.info("%s: no latest_crossings data (ok if not available)", cp_name)

            # ── chokepoint_index 파생 (완전주만 사용) ───────────────────────
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
