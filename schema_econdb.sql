-- ============================================================
-- MPCI Engine — EconDB 전체 파이프라인 스키마
-- econdb_ports / econdb_port_snapshots / econdb_port_timeseries
-- Supabase SQL Editor에서 한 번 실행 (멱등)
-- ============================================================

-- ── ① 항만 마스터 테이블 ─────────────────────────────────────

create table if not exists public.econdb_ports (
  locode            text        primary key,   -- 'SG SIN' (공백 포함 원본)
  port_name         text        not null,
  country_code      text,
  region            text,
  lat               double precision,
  lon               double precision,
  last_collected_at timestamptz,              -- per-port 3개 endpoint 마지막 수집 시각
  first_seen_at     timestamptz default now()
);

-- ── ② Top 20 일별 스냅샷 (search/ports 전용) ─────────────────

create table if not exists public.econdb_port_snapshots (
  id              bigserial   primary key,
  locode          text        not null,
  snapshot_date   date        not null,
  sort_type       text        not null,        -- 'congestion' | 'volume'
  page_rank       integer,                     -- 해당 sort 기준 1~20위
  econdb_rank     integer,                     -- 글로벌 물동량 순위
  schedule        integer,
  delay_percent   double precision,
  port_congestion double precision,
  import_dwell    double precision,
  export_dwell    double precision,
  ts_dwell        double precision,
  last_import_teu double precision,
  last_export_teu double precision,
  turnaround      double precision,
  vessels_berthed integer,
  raw_json        jsonb,
  unique (locode, snapshot_date, sort_type)
);

-- ── ③ 항만별 시계열 (3개 endpoint 공용) ──────────────────────

create table if not exists public.econdb_port_timeseries (
  id           bigserial   primary key,
  locode       text        not null,
  series_type  text        not null,  -- 'containers' | 'omissions' | 'schedule'
  ts           timestamptz not null,
  value_a      double precision,      -- containers/schedule: TEU current  | omissions: Blanked TEU
  value_b      double precision,      -- containers/schedule: TEU last year | omissions: Actual TEU
  fetched_at   timestamptz default now(),
  unique (locode, series_type, ts)
);

-- ── 인덱스 ────────────────────────────────────────────────────

create index if not exists idx_econdb_ports_lat_lon
  on econdb_ports (lat, lon)
  where lat is not null and lon is not null;

create index if not exists idx_econdb_port_snapshots_date_sort
  on econdb_port_snapshots (snapshot_date desc, sort_type, page_rank);

create index if not exists idx_econdb_port_timeseries_locode_type_ts
  on econdb_port_timeseries (locode, series_type, ts desc);

-- ── RLS 활성화 ────────────────────────────────────────────────

alter table econdb_ports           enable row level security;
alter table econdb_port_snapshots  enable row level security;
alter table econdb_port_timeseries enable row level security;

-- ── anon SELECT 정책 (중복 실행 시 무시) ─────────────────────

do $$ begin
  execute 'create policy "anon_read" on econdb_ports for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read" on econdb_port_snapshots for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read" on econdb_port_timeseries for select to anon using (true)';
exception when duplicate_object then null;
end $$;
