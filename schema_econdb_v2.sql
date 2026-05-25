-- ============================================================
-- MPCI Engine — EconDB Collector 스키마 v2
-- 기존 econdb_* 테이블 마이그레이션 + 5개 신규 테이블
-- Supabase SQL Editor에서 한 번 실행 (멱등)
-- ============================================================

-- ── 기존 테이블 정리 (cascade) ────────────────────────────────
-- schema_econdb.sql 로 생성된 테이블이 있으면 제거 후 재생성

drop table if exists public.econdb_port_timeseries cascade;
drop table if exists public.econdb_port_snapshots  cascade;
drop table if exists public.econdb_ports           cascade;

-- ── ① 항만 마스터 ─────────────────────────────────────────────
create table if not exists public.econdb_ports (
  locode        text        primary key,           -- 'SG SIN' (공백 포함 원본)
  country_code  text,                              -- 'SG'
  port_code     text,                              -- 'SIN'
  port_name     text        not null,
  lat           float,                             -- WGS84 위도 (지도 마커용)
  lon           float,                             -- WGS84 경도 (지도 마커용)
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  is_active     boolean     not null default true
);

-- 기존 테이블에 컬럼이 없을 경우 추가 (멱등)
alter table public.econdb_ports add column if not exists lat float;
alter table public.econdb_ports add column if not exists lon float;

create index if not exists idx_econdb_ports_coords
  on public.econdb_ports (lat, lon)
  where lat is not null and lon is not null;

-- ── ② 항만-region 매핑 (N:M) ─────────────────────────────────
create table if not exists public.econdb_port_regions (
  id         bigserial   primary key,
  locode     text        not null references public.econdb_ports(locode) on delete cascade,
  region     text        not null,
  created_at timestamptz not null default now(),
  unique (locode, region)
);

-- ── ③ 수집 job queue ─────────────────────────────────────────
create table if not exists public.econdb_fetch_jobs (
  id                bigserial   primary key,
  locode            text        not null references public.econdb_ports(locode) on delete cascade,
  endpoint_type     text        not null,    -- 'containers_in_terminal' | 'omissions_time_series' | 'weekly_schedule_profile'
  status            text        not null default 'pending',  -- pending | running | done | failed
  scheduled_at      timestamptz not null,
  started_at        timestamptz,
  finished_at       timestamptz,
  attempt_count     integer     not null default 0,
  last_http_status  integer,
  last_error        text,
  created_at        timestamptz not null default now(),
  unique (locode, endpoint_type, scheduled_at)
);

create index if not exists idx_econdb_fetch_jobs_due
  on public.econdb_fetch_jobs (status, scheduled_at);

-- ── ④ 원본 JSON 저장 ─────────────────────────────────────────
-- 파싱 로직이 변경돼도 원본으로 복구 가능
create table if not exists public.econdb_widget_raw_snapshots (
  id            bigserial   primary key,
  locode        text        not null references public.econdb_ports(locode) on delete cascade,
  endpoint_type text        not null,
  collected_at  timestamptz not null default now(),
  payload_hash  text        not null,          -- SHA-256 of raw_json (dedup)
  raw_json      jsonb       not null,
  unique (locode, endpoint_type, payload_hash)
);

create index if not exists idx_econdb_widget_raw_locode
  on public.econdb_widget_raw_snapshots (locode, endpoint_type, collected_at desc);

-- ── ⑤ 파싱된 시계열 ──────────────────────────────────────────
-- plots[].data[] 를 풀어서 저장
create table if not exists public.econdb_widget_timeseries (
  id            bigserial   primary key,
  locode        text        not null references public.econdb_ports(locode) on delete cascade,
  endpoint_type text        not null,
  plot_title    text        not null default '',  -- NULL 대신 '' (upsert 충돌 탐지용)
  series_code   text        not null,             -- 'TEU', 'TEU_Last_Year', ...
  series_name   text,
  observed_at   timestamptz not null,
  value         numeric,
  collected_at  timestamptz not null default now(),
  raw_point     jsonb,
  unique (locode, endpoint_type, plot_title, series_code, observed_at)
);

create index if not exists idx_econdb_widget_timeseries_query
  on public.econdb_widget_timeseries (locode, endpoint_type, observed_at desc);

-- ── RLS 활성화 ────────────────────────────────────────────────
alter table public.econdb_ports                  enable row level security;
alter table public.econdb_port_regions           enable row level security;
alter table public.econdb_fetch_jobs             enable row level security;
alter table public.econdb_widget_raw_snapshots   enable row level security;
alter table public.econdb_widget_timeseries      enable row level security;

-- ── anon SELECT 정책 (중복 실행 시 무시) ─────────────────────
do $$ begin
  execute 'create policy "anon_read" on public.econdb_ports for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read" on public.econdb_port_regions for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read" on public.econdb_fetch_jobs for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read" on public.econdb_widget_raw_snapshots for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read" on public.econdb_widget_timeseries for select to anon using (true)';
exception when duplicate_object then null;
end $$;
