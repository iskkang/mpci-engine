"""
scripts/econdb_collector.py

EconDB Port Congestion Collector

사용법:
  python scripts/econdb_collector.py sync-ports          # region → locode 수집
  python scripts/econdb_collector.py create-jobs         # job queue 생성 (전체)
  python scripts/econdb_collector.py create-jobs --max-ports 30 --hours-to-spread 24
  python scripts/econdb_collector.py run-jobs            # due job 처리 (기본 10개)
  python scripts/econdb_collector.py run-jobs --max-jobs 30 --sleep-seconds 3

구조:
  congestion_region_ports → locode 확보 → econdb_ports / econdb_port_regions
  locode × 3 endpoint    → job queue → econdb_widget_raw_snapshots / econdb_widget_timeseries

가드레일:
  - 403/429 수신 즉시 중단 (재시도 없음)
  - 5xx는 최대 3회 지수 백오프 재시도
  - raw_json 반드시 저장 (파싱 로직이 달라져도 복구 가능)
  - 값 날조/추정 금지
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client, Client


# ── Supabase 연결 ──────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")

# SUPABASE_SERVICE_ROLE_KEY 와 SUPABASE_SERVICE_KEY 양쪽 지원
SUPABASE_SERVICE_ROLE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "Missing env vars: SUPABASE_URL and "
        "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY)"
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ── 상수 ──────────────────────────────────────────────────────────────────────
REGIONS = [
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
    "Central America",
    "Oceania",
]

ENDPOINTS = {
    "containers_in_terminal":
        "https://www.econdb.com/widgets/containers-in-terminal/data/",
    "omissions_time_series":
        "https://www.econdb.com/widgets/omissions-time-series/data/",
    "weekly_schedule_profile":
        "https://www.econdb.com/widgets/weekly-schedule-profile/data/",
}

# 명확한 식별용 User-Agent (숨기기 목적 아님)
HEADERS = {
    "User-Agent": "MTL-Link-PortCongestionCollector/0.1 contact=info@mtlb.co.kr",
    "Accept": "application/json,text/plain,*/*",
}


# ── 유틸리티 ──────────────────────────────────────────────────────────────────
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_locode(locode: str) -> tuple[str | None, str | None]:
    """'SG SIN' → ('SG', 'SIN')"""
    if not locode:
        return None, None
    parts = locode.strip().split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def fetch_json(url: str, params: dict | None = None, max_retries: int = 3) -> dict:
    """
    정중한 수집용 fetch.
    403/429: 즉시 실패 (재시도 없음).
    5xx: 지수 백오프 재시도 (최대 max_retries회).
    """
    for attempt in range(1, max_retries + 1):
        response = requests.get(url, params=params, headers=HEADERS, timeout=30)
        status = response.status_code

        if status in (403, 429):
            raise RuntimeError(f"Blocked or rate-limited. HTTP {status}")

        if 500 <= status < 600:
            if attempt == max_retries:
                raise RuntimeError(f"Server error. HTTP {status}: {response.text[:300]}")
            sleep_s = 3 * attempt
            print(f"  [warn] HTTP {status}, retrying in {sleep_s}s (attempt {attempt}/{max_retries})")
            time.sleep(sleep_s)
            continue

        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {response.text[:300]}")

        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"Invalid JSON response: {exc}")

    raise RuntimeError("fetch_json: all retries exhausted")


# ── [1] sync-ports ─────────────────────────────────────────────────────────────
def sync_region_ports() -> None:
    """
    region별 port list → econdb_ports / econdb_port_regions 저장.
    congestion_region_ports endpoint 응답: {"ports":[{"locode":"SG SIN","name":"Singapore"},...]}
    """
    total_ports_seen = 0

    for region in REGIONS:
        print(f"[sync-ports] region={region}")
        url = "https://www.econdb.com/maritime/congestion_region_ports/"

        try:
            payload = fetch_json(url, params={"region": region})
        except RuntimeError as exc:
            print(f"  [error] {exc}")
            # 403/429면 전체 중단
            if "403" in str(exc) or "429" in str(exc) or "rate-limited" in str(exc):
                print("[sync-ports] stopping due to block signal")
                break
            time.sleep(1.5)
            continue

        ports = payload.get("ports", [])
        if not isinstance(ports, list):
            print(f"  [warn] unexpected response keys={list(payload.keys())}")
            time.sleep(1.5)
            continue

        for port in ports:
            locode = port.get("locode")
            port_name = port.get("name")
            if not locode or not port_name:
                continue

            country_code, port_code = parse_locode(locode)

            try:
                supabase.table("econdb_ports").upsert(
                    {
                        "locode":       locode,
                        "country_code": country_code,
                        "port_code":    port_code,
                        "port_name":    port_name,
                        "last_seen_at": utc_now_iso(),
                        "is_active":    True,
                    },
                    on_conflict="locode",
                ).execute()

                supabase.table("econdb_port_regions").upsert(
                    {"locode": locode, "region": region},
                    on_conflict="locode,region",
                ).execute()

                total_ports_seen += 1
            except Exception as exc:
                print(f"  [error] upsert failed locode={locode}: {exc}")

        print(f"  → {len(ports)}개 처리")
        time.sleep(1.5)

    print(f"[sync-ports] 완료. total={total_ports_seen}")


# ── [2] create-jobs ────────────────────────────────────────────────────────────
def create_fetch_jobs(
    endpoint_types: list[str] | None = None,
    hours_to_spread: int = 72,
    max_ports: int | None = None,
) -> None:
    """
    전체 port × endpoint_type job 생성.
    hours_to_spread=72 → 3일에 나눠서 예약.
    """
    if endpoint_types is None:
        endpoint_types = list(ENDPOINTS.keys())

    result = (
        supabase.table("econdb_ports")
        .select("locode, port_name")
        .eq("is_active", True)
        .order("locode")
        .execute()
    )
    ports = result.data or []

    if max_ports:
        ports = ports[:max_ports]

    if not ports:
        print("[create-jobs] econdb_ports가 비어있습니다. sync-ports를 먼저 실행하세요.")
        return

    jobs: list[dict] = []
    total_jobs = len(ports) * len(endpoint_types)
    now = datetime.now(timezone.utc)
    spread_seconds = max(hours_to_spread * 3600, total_jobs)
    step_seconds = spread_seconds / total_jobs

    for idx, port in enumerate(ports):
        locode = port["locode"]
        for ep_idx, endpoint_type in enumerate(endpoint_types):
            i = idx * len(endpoint_types) + ep_idx
            scheduled_at = now + timedelta(seconds=int(i * step_seconds))
            jobs.append({
                "locode":        locode,
                "endpoint_type": endpoint_type,
                "status":        "pending",
                "scheduled_at":  scheduled_at.isoformat(),
            })

    batch_size = 500
    inserted = 0
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i : i + batch_size]
        try:
            supabase.table("econdb_fetch_jobs").upsert(
                batch,
                on_conflict="locode,endpoint_type,scheduled_at",
            ).execute()
            inserted += len(batch)
        except Exception as exc:
            print(f"[create-jobs] batch 실패: {exc}")

    print(
        f"[create-jobs] 완료. ports={len(ports)} "
        f"endpoints={endpoint_types} jobs={len(jobs)} upserted={inserted}"
    )


# ── [3] run-jobs ───────────────────────────────────────────────────────────────
def build_widget_params(endpoint_type: str, locode: str) -> dict:
    if endpoint_type == "weekly_schedule_profile":
        return {"locode": locode, "metric": "TEU", "size": "ALL"}
    return {"locode": locode}


def save_raw_snapshot(locode: str, endpoint_type: str, payload: dict) -> None:
    h = payload_hash(payload)
    supabase.table("econdb_widget_raw_snapshots").upsert(
        {
            "locode":        locode,
            "endpoint_type": endpoint_type,
            "payload_hash":  h,
            "raw_json":      payload,
        },
        on_conflict="locode,endpoint_type,payload_hash",
    ).execute()


def parse_and_save_timeseries(locode: str, endpoint_type: str, payload: dict) -> int:
    """
    widget 응답 구조:
    {
      "plots": [{
        "title": "...",
        "series": [{"code":"TEU","name":"TEU current year"},...],
        "data": [{"Date":"2026-05-11T00:00:00","TEU":281308.875,...}]
      }]
    }
    """
    plots = payload.get("plots", [])
    if not isinstance(plots, list):
        return 0

    rows_to_upsert: list[dict] = []

    for plot in plots:
        plot_title = plot.get("title") or ""
        series_list = plot.get("series", [])
        data_list = plot.get("data", [])

        if not isinstance(series_list, list) or not isinstance(data_list, list):
            continue

        # series code → name 매핑
        series_map: dict[str, str] = {
            s["code"]: s.get("name", s["code"])
            for s in series_list
            if s.get("code")
        }

        for point in data_list:
            observed_at = (
                point.get("Date") or
                point.get("date") or
                point.get("datetime")
            )
            if not observed_at:
                continue

            for series_code, series_name in series_map.items():
                value = point.get(series_code)
                if value is None:
                    continue

                rows_to_upsert.append({
                    "locode":        locode,
                    "endpoint_type": endpoint_type,
                    "plot_title":    plot_title,
                    "series_code":   series_code,
                    "series_name":   series_name,
                    "observed_at":   observed_at,
                    "value":         value,
                    "raw_point":     point,
                })

    if not rows_to_upsert:
        return 0

    saved = 0
    batch_size = 500
    for i in range(0, len(rows_to_upsert), batch_size):
        batch = rows_to_upsert[i : i + batch_size]
        supabase.table("econdb_widget_timeseries").upsert(
            batch,
            on_conflict="locode,endpoint_type,plot_title,series_code,observed_at",
        ).execute()
        saved += len(batch)

    return saved


def mark_job_started(job_id: int) -> None:
    supabase.table("econdb_fetch_jobs").update({
        "status":        "running",
        "started_at":    utc_now_iso(),
        "attempt_count": 1,
    }).eq("id", job_id).execute()


def mark_job_done(job_id: int) -> None:
    supabase.table("econdb_fetch_jobs").update({
        "status":           "done",
        "finished_at":      utc_now_iso(),
        "last_error":       None,
        "last_http_status": 200,
    }).eq("id", job_id).execute()


def mark_job_failed(job: dict, error: str, retry_after_minutes: int = 180) -> None:
    attempt_count = int(job.get("attempt_count") or 0) + 1

    if attempt_count >= 3:
        status = "failed"
        scheduled_at = job["scheduled_at"]  # 더 이상 재예약 안 함
    else:
        status = "pending"
        scheduled_at = (
            datetime.now(timezone.utc) + timedelta(minutes=retry_after_minutes)
        ).isoformat()

    supabase.table("econdb_fetch_jobs").update({
        "status":        status,
        "finished_at":   utc_now_iso(),
        "attempt_count": attempt_count,
        "last_error":    error[:1000],
        "scheduled_at":  scheduled_at,
    }).eq("id", job["id"]).execute()


def run_due_jobs(max_jobs: int = 10, sleep_seconds: float = 3.0) -> None:
    """due 상태 job을 최대 max_jobs개 처리."""
    now = utc_now_iso()

    result = (
        supabase.table("econdb_fetch_jobs")
        .select("*")
        .eq("status", "pending")
        .lte("scheduled_at", now)
        .order("scheduled_at")
        .limit(max_jobs)
        .execute()
    )
    jobs = result.data or []

    if not jobs:
        print("[run-jobs] due job 없음")
        return

    print(f"[run-jobs] due_jobs={len(jobs)}")

    for job in jobs:
        job_id      = job["id"]
        locode      = job["locode"]
        endpoint_type = job["endpoint_type"]

        if endpoint_type not in ENDPOINTS:
            mark_job_failed(job, f"Unknown endpoint_type={endpoint_type}")
            continue

        print(f"  job_id={job_id} locode={locode} endpoint={endpoint_type}")

        try:
            mark_job_started(job_id)

            url    = ENDPOINTS[endpoint_type]
            params = build_widget_params(endpoint_type, locode)
            payload = fetch_json(url, params=params)

            save_raw_snapshot(locode, endpoint_type, payload)
            saved_rows = parse_and_save_timeseries(locode, endpoint_type, payload)

            mark_job_done(job_id)
            print(f"  → success rows={saved_rows}")

        except Exception as exc:
            error = str(exc)
            print(f"  → failed: {error}")

            # 403/429: 전체 중단 + 재예약 6시간 후
            if "403" in error or "429" in error or "rate-limited" in error:
                mark_job_failed(job, error, retry_after_minutes=360)
                print("[run-jobs] block/rate-limit 감지, 중단")
                break

            mark_job_failed(job, error)

        time.sleep(sleep_seconds)

    print("[run-jobs] 완료")


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="EconDB Port Congestion Collector")
    parser.add_argument(
        "command",
        choices=["sync-ports", "create-jobs", "run-jobs"],
    )
    parser.add_argument("--max-jobs",        type=int,   default=10)
    parser.add_argument("--sleep-seconds",   type=float, default=3.0)
    parser.add_argument("--hours-to-spread", type=int,   default=72)
    parser.add_argument("--max-ports",       type=int,   default=None)

    args = parser.parse_args()

    if args.command == "sync-ports":
        sync_region_ports()
    elif args.command == "create-jobs":
        create_fetch_jobs(
            hours_to_spread=args.hours_to_spread,
            max_ports=args.max_ports,
        )
    elif args.command == "run-jobs":
        run_due_jobs(
            max_jobs=args.max_jobs,
            sleep_seconds=args.sleep_seconds,
        )


if __name__ == "__main__":
    main()
