"""
MPCI Engine — ShipFinder Hormuz / Persian Gulf 데이터 수집기
GitHub Actions: 일 1회 실행

⚠️ 출처/법적 원칙 (필수):
  1. 모든 적재 행에 source='ShipFinder' 명시
  2. AI 브리핑(Elane AI 생성)·뉴스: 링크+제목+출처만 저장(본문 복제 금지).
     UI에서 link-out 기본.
  3. ShipFinder ToS/공식 API 약관 준수. 스크랩 실패 시 skip+log, 파이프라인 중단 금지.
  4. SHIPFINDER_BASE_URL 환경변수로 베이스 URL 설정 가능.

상수:
  - direction: 0=inbound(IN), 1=outbound(OUT)
  - detail date: 항상 어제(UTC today-1)
  - 정수 컬럼: safe_int() 로 int() 캐스팅 (22P02 방지)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# ── sys.path 설정 (standalone & module 모두 지원) ──────────────────────────
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import httpx
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SOURCE       = "ShipFinder"
MAX_RETRIES  = 3

# 기본 베이스 URL — 환경변수로 재정의 가능
# ⚠️ 버그 수정: .co → .com (정상 도메인)
SHIPFINDER_BASE = os.getenv("SHIPFINDER_BASE_URL", "https://www.shipfinder.com").rstrip("/")

# shiptype 중→영 매핑 (미매핑은 원문 그대로)
SHIPTYPE_MAP: dict[str, str] = {
    "帆船":    "Sailing",
    "货船":    "Cargo",
    "干散货船": "Bulk",
    "油船":    "Tanker",
    "原油船":  "Crude Tanker",
    "LNG":     "LNG",
    "渔船":    "Fishing",
    "拖引":    "Tug",
    "拖轮":    "Tug",
    "客船":    "Passenger",
    "滚装船":  "Ro-Ro",
    "游艇":    "Yacht",
    "其他类型船": "Other",
}

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # AJAX 클라이언트가 정상적으로 보내는 헤더 — 봇 차단 우회
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SHIPFINDER_BASE}/",
    "X-Requested-With": "XMLHttpRequest",
}


# ── Supabase ───────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


# ── HTTP 클라이언트 ────────────────────────────────────────────────────────
def make_sf_client() -> httpx.Client:
    return httpx.Client(
        headers=_BROWSER_HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


def sf_get(
    client: httpx.Client,
    path: str,
    params: dict | None = None,
) -> dict | None:
    """
    ShipFinder 단건 GET + 지수 백오프 재시도.
    실패 시 None 반환(로그+스킵, 파이프라인 중단 금지).
    """
    url = f"{SHIPFINDER_BASE}/{path.lstrip('/')}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, params=params, timeout=60.0)
            if resp.status_code in (403, 404, 429):
                logger.warning(
                    "ShipFinder blocked/not-found status=%s url=%s", resp.status_code, url
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                logger.warning(
                    "Unexpected response type %s url=%s", type(data).__name__, url
                )
                return None
            return data
        except Exception as exc:
            if attempt == MAX_RETRIES:
                logger.error(
                    "ShipFinder GET failed after %s attempts url=%s: %s",
                    MAX_RETRIES, url, exc,
                )
                return None
            sleep_s = 2 ** attempt
            logger.warning(
                "Retrying ShipFinder (attempt %s/%s) in %ss url=%s: %s",
                attempt, MAX_RETRIES, sleep_s, url, exc,
            )
            time.sleep(sleep_s)
    return None


# ── 타입 변환 유틸 ─────────────────────────────────────────────────────────
def safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def map_shiptype(raw: str | None) -> str:
    if not raw:
        return "Other"
    return SHIPTYPE_MAP.get(str(raw).strip(), str(raw).strip())


def map_direction(v) -> str:
    """0=inbound(in), 1=outbound(out)."""
    try:
        return "in" if int(v) == 0 else "out"
    except (TypeError, ValueError):
        return str(v) if v is not None else "unknown"


def parse_ts(raw) -> Optional[str]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")
        ).isoformat()
    except ValueError:
        return str(raw)


# ── Fetch 함수 (실패 시 빈 값 반환, 파이프라인 중단 금지) ──────────────────
def fetch_gulf_stats(client: httpx.Client) -> list[dict]:
    resp = sf_get(client, "Special/ShipsInPersianGulfStats")
    return (resp or {}).get("data") or [] if resp else []


def fetch_hormuz_stats(client: httpx.Client) -> list[dict]:
    resp = sf_get(client, "Special/CrossStraitOfHormuzStats")
    return (resp or {}).get("data") or [] if resp else []


def fetch_hormuz_detail(client: httpx.Client, dt: date) -> list[dict]:
    resp = sf_get(
        client, "Special/CrossStraitOfHormuzDetail",
        params={"date": dt.isoformat()},
    )
    return (resp or {}).get("data") or [] if resp else []


def fetch_macro_latest(client: httpx.Client) -> list[dict]:
    resp = sf_get(client, "Special/GetMacroIndexLatest")
    return (resp or {}).get("data") or [] if resp else []


def fetch_macro_30(client: httpx.Client) -> dict | None:
    resp = sf_get(client, "Special/GetMacroIndex30Days")
    if not resp:
        return None
    return resp.get("data")


def fetch_news(client: httpx.Client) -> list[dict]:
    resp = sf_get(
        client, "Special/GetHormuzNewsRecent",
        params={"skip": 0, "limit": 50},
    )
    return (resp or {}).get("data") or [] if resp else []


def fetch_ai_brief(client: httpx.Client) -> dict | None:
    """AI 브리핑 (구조 미확인 — 방어적 처리)."""
    return sf_get(client, "Special/CallAiToJudge")


# ── Upsert 함수 ────────────────────────────────────────────────────────────
def upsert_gulf_count(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for r in rows:
        d   = r.get("dt")
        cnt = safe_int(r.get("ship_cnt"))
        if not d or cnt is None:
            continue
        records.append(
            {"obs_date": str(d)[:10], "ship_cnt": cnt, "source": SOURCE, "updated_at": now}
        )
    if not records:
        return
    supabase.table("hormuz_gulf_count_daily").upsert(
        records, on_conflict="obs_date"
    ).execute()
    logger.info("Upserted %s hormuz_gulf_count_daily rows", len(records))


def upsert_hormuz_transit(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for r in rows:
        d = r.get("dt")
        if not d:
            continue
        records.append(
            {
                "obs_date":      str(d)[:10],
                "inbound_cnt":   safe_int(r.get("entry_ship_cnt")),
                "outbound_cnt":  safe_int(r.get("exit_ship_cnt")),
                "total_cnt":     safe_int(r.get("total_ship_cnt")),
                "source":        SOURCE,
                "updated_at":    now,
            }
        )
    if not records:
        return
    supabase.table("hormuz_transit_daily").upsert(
        records, on_conflict="obs_date"
    ).execute()
    logger.info("Upserted %s hormuz_transit_daily rows", len(records))


def upsert_hormuz_log(
    supabase: Client, obs_date: date, rows: list[dict]
) -> None:
    if not rows:
        logger.info("No hormuz_transit_log rows for %s — skip", obs_date)
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for r in rows:
        mmsi = safe_int(r.get("mmsi"))
        # time1 = exit 시각 (PK 구성요소)
        time_exit_raw = r.get("time1")
        if not mmsi or not time_exit_raw:
            continue
        time_exit  = parse_ts(time_exit_raw) or str(time_exit_raw)
        time_enter = parse_ts(r.get("time2"))

        records.append(
            {
                "obs_date":        obs_date.isoformat(),
                "mmsi":            mmsi,
                "time_exit":       time_exit,
                "time_enter":      time_enter,
                "direction":       map_direction(r.get("direction")),
                "shipname":        r.get("shipname_en") or r.get("shipname"),
                "shiptype":        map_shiptype(r.get("shiptype")),
                "dwt":             safe_float(r.get("dwt")),
                "country_code":    r.get("country_code"),
                "country_name":    r.get("countryname_en") or r.get("countryname"),
                "owner_company":   r.get("owner_company"),
                "operator_company": r.get("operator_company"),
                "source":          SOURCE,
                "updated_at":      now,
            }
        )
    if not records:
        return
    supabase.table("hormuz_transit_log").upsert(
        records, on_conflict="obs_date,mmsi,time_exit"
    ).execute()
    logger.info(
        "Upserted %s hormuz_transit_log rows for %s", len(records), obs_date
    )


def upsert_macro_latest(supabase: Client, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for r in rows:
        idx_type = r.get("DateType")
        if not idx_type:
            continue
        dd = r.get("DataDate")
        records.append(
            {
                "index_type":  str(idx_type).upper(),
                "data_date":   str(dd)[:10] if dd else None,
                "value":       safe_float(r.get("IndicatorValue")),
                "change_rate": str(r["ChangeRate"]) if r.get("ChangeRate") is not None else None,
                "source":      SOURCE,
                "updated_at":  now,
            }
        )
    if not records:
        return
    supabase.table("hormuz_macro_latest").upsert(
        records, on_conflict="index_type"
    ).execute()
    logger.info("Upserted %s hormuz_macro_latest rows", len(records))


def upsert_macro_30(supabase: Client, data: dict) -> None:
    """
    data = {"dates": [...], "series": {"wti": [...], "brent": [...], ...}}
    null = 결측일 → 스킵
    """
    dates  = data.get("dates") or []
    series = data.get("series") or {}
    if not dates or not series:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for i, d in enumerate(dates):
        if not d:
            continue
        for idx_type, values in series.items():
            if not isinstance(values, list) or i >= len(values):
                continue
            v = values[i]
            if v is None:
                continue  # null = 결측일
            records.append(
                {
                    "obs_date":   str(d)[:10],
                    "index_type": str(idx_type).upper(),
                    "value":      safe_float(v),
                    "source":     SOURCE,
                    "updated_at": now,
                }
            )
    if not records:
        return
    supabase.table("hormuz_macro_daily").upsert(
        records, on_conflict="obs_date,index_type"
    ).execute()
    logger.info("Upserted %s hormuz_macro_daily rows", len(records))


def upsert_news(supabase: Client, rows: list[dict]) -> None:
    """
    제목+출처+URL+시각만 저장. 본문 복제 금지 (summary 허용 — 요약이지 본문 아님).
    UI에서 link-out 기본.
    """
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for r in rows:
        nid = safe_int(r.get("id"))
        if not nid:
            continue
        records.append(
            {
                "id":         nid,
                "title":      r.get("title"),
                "source":     r.get("source"),
                "url":        r.get("url"),
                "news_time":  parse_ts(r.get("news_time")),
                "summary":    r.get("summary"),   # 짧은 요약만, link-out 기본
                "fetched_at": now,
            }
        )
    if not records:
        return
    supabase.table("hormuz_news").upsert(records, on_conflict="id").execute()
    logger.info("Upserted %s hormuz_news rows", len(records))


def upsert_ai_brief(
    supabase: Client, data: dict | None, brief_date: date
) -> None:
    """
    AI 브리핑: 원문 그대로 저장(재서술 금지). UI에서 link-out 기본.
    "출처: ShipFinder · Elane AI 생성 · 검증 필요" 필드 명시.
    """
    if not data or not isinstance(data, dict):
        return
    # 구조 미확인 — 가능한 키 순차 시도
    body = (
        data.get("content")
        or data.get("text")
        or data.get("body")
        or data.get("result")
    )
    if not body:
        logger.info("AI brief: no parseable body — skip")
        return
    now = datetime.now(timezone.utc).isoformat()
    supabase.table("hormuz_ai_brief").upsert(
        [
            {
                "brief_date":  brief_date.isoformat(),
                "body":        str(body)[:10_000],  # 길이 제한
                "source":      "ShipFinder · Elane AI",
                "caveat":      "외부 AI 생성 · 검증 필요",
                "source_url":  data.get("url"),
                "fetched_at":  now,
            }
        ],
        on_conflict="brief_date",
    ).execute()
    logger.info("Upserted hormuz_ai_brief for %s", brief_date)


# ── main ────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("fetch_shipfinder_hormuz.py starting... (base=%s)", SHIPFINDER_BASE)
    today     = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    client = make_sf_client()
    try:
        # 각 수집 단계 독립 — 한쪽 실패가 다른 쪽을 막지 않음
        gulf_rows    = fetch_gulf_stats(client)
        hormuz_rows  = fetch_hormuz_stats(client)
        detail_rows  = fetch_hormuz_detail(client, yesterday)
        macro_latest = fetch_macro_latest(client)
        macro_30     = fetch_macro_30(client)
        news_rows    = fetch_news(client)
        ai_data      = fetch_ai_brief(client)
    finally:
        try:
            client.close()
        except Exception:
            pass

    supabase = get_supabase()

    upsert_gulf_count(supabase, gulf_rows)
    upsert_hormuz_transit(supabase, hormuz_rows)
    upsert_hormuz_log(supabase, yesterday, detail_rows)
    upsert_macro_latest(supabase, macro_latest)
    if macro_30:
        upsert_macro_30(supabase, macro_30)
    upsert_news(supabase, news_rows)
    upsert_ai_brief(supabase, ai_data, today)

    logger.info("fetch_shipfinder_hormuz.py completed successfully.")


if __name__ == "__main__":
    main()
