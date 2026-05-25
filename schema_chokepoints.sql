-- ============================================================
-- MPCI Engine — Chokepoint 통합 스키마
-- EconDB 6해협 + ShipFinder Hormuz/Persian Gulf
-- Supabase SQL Editor에서 한 번 실행 (멱등)
-- ============================================================

-- ── EconDB 테이블 ───────────────────────────────────────────

create table if not exists chokepoint_catalog (
  chokepoint  text        primary key,
  econdb_id   integer,
  updated_at  timestamptz not null default now()
);

create table if not exists chokepoint_pass_weekly (
  chokepoint  text        not null,
  week_start  date        not null,
  teu_north   double precision,
  teu_south   double precision,
  teu_total   double precision,
  is_partial  boolean     not null default false,
  updated_at  timestamptz not null default now(),
  primary key (chokepoint, week_start)
);

create table if not exists chokepoint_water_level (
  chokepoint        text        not null default 'Panama',
  obs_date          date        not null,
  level_current     double precision,
  level_avg         double precision,
  level_range_low   double precision,
  level_range_high  double precision,
  updated_at        timestamptz not null default now(),
  primary key (chokepoint, obs_date)
);

-- 해협별 최신 집계 (pct_of_normal, status, narrative 등)
create table if not exists chokepoint_index (
  chokepoint        text        primary key,
  teu_total         double precision,
  teu_north         double precision,
  teu_south         double precision,
  pct_of_normal     double precision,
  trend_pct         double precision,
  status            text,
  water_level       double precision,
  water_level_avg   double precision,
  water_level_low   double precision,
  water_level_high  double precision,
  draft_status      text,
  narrative_ko      text,
  narrative_en      text,
  data_week         date,
  updated_at        timestamptz not null default now()
);

-- ── ShipFinder 테이블 (source 컬럼 명시) ────────────────────

create table if not exists hormuz_gulf_count_daily (
  obs_date    date        primary key,
  ship_cnt    integer,
  source      text        not null default 'ShipFinder',
  updated_at  timestamptz not null default now()
);

create table if not exists hormuz_transit_daily (
  obs_date      date        primary key,
  inbound_cnt   integer,
  outbound_cnt  integer,
  total_cnt     integer,
  source        text        not null default 'ShipFinder',
  updated_at    timestamptz not null default now()
);

-- 해협 통과 선박 로그 (어제자, PK: obs_date + mmsi + time_exit)
create table if not exists hormuz_transit_log (
  obs_date          date        not null,
  mmsi              bigint      not null,
  time_exit         timestamptz not null,
  time_enter        timestamptz,
  direction         text,
  shipname          text,
  shiptype          text,
  dwt               double precision,
  country_code      text,
  country_name      text,
  owner_company     text,
  operator_company  text,
  source            text        not null default 'ShipFinder',
  updated_at        timestamptz not null default now(),
  primary key (obs_date, mmsi, time_exit)
);

-- 최신 거시지표 (WTI/Brent/BDTI/CTFI)
create table if not exists hormuz_macro_latest (
  index_type  text        primary key,
  data_date   date,
  value       double precision,
  change_rate text,
  source      text        not null default 'ShipFinder',
  updated_at  timestamptz not null default now()
);

-- 30일 거시지표 일별 (null=결측일 → 행 없음)
create table if not exists hormuz_macro_daily (
  obs_date    date        not null,
  index_type  text        not null,
  value       double precision,
  source      text        not null default 'ShipFinder',
  updated_at  timestamptz not null default now(),
  primary key (obs_date, index_type)
);

-- 뉴스 (제목+출처+URL+시각만, 본문 복제 금지)
create table if not exists hormuz_news (
  id          bigint      primary key,
  title       text,
  source      text,
  url         text,
  news_time   timestamptz,
  summary     text,
  fetched_at  timestamptz not null default now()
);

-- AI 브리핑 (Elane AI 생성, 원문 저장, UI에서 link-out 기본)
create table if not exists hormuz_ai_brief (
  brief_date  date        primary key,
  body        text,
  source      text        not null default 'ShipFinder · Elane AI',
  caveat      text        not null default '외부 AI 생성 · 검증 필요',
  source_url  text,
  fetched_at  timestamptz not null default now()
);

-- ── RLS 활성화 (프론트 anon read-only) ─────────────────────

alter table chokepoint_catalog        enable row level security;
alter table chokepoint_pass_weekly    enable row level security;
alter table chokepoint_water_level    enable row level security;
alter table chokepoint_index          enable row level security;
alter table hormuz_gulf_count_daily   enable row level security;
alter table hormuz_transit_daily      enable row level security;
alter table hormuz_transit_log        enable row level security;
alter table hormuz_macro_latest       enable row level security;
alter table hormuz_macro_daily        enable row level security;
alter table hormuz_news               enable row level security;
alter table hormuz_ai_brief           enable row level security;

-- ── anon SELECT 정책 (중복 실행 시 무시) ──────────────────

do $$ begin
  execute 'create policy "anon_read_chokepoint_catalog" on chokepoint_catalog for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_chokepoint_pass_weekly" on chokepoint_pass_weekly for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_chokepoint_water_level" on chokepoint_water_level for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_chokepoint_index" on chokepoint_index for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_gulf_count_daily" on hormuz_gulf_count_daily for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_transit_daily" on hormuz_transit_daily for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_transit_log" on hormuz_transit_log for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_macro_latest" on hormuz_macro_latest for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_macro_daily" on hormuz_macro_daily for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_news" on hormuz_news for select to anon using (true)';
exception when duplicate_object then null;
end $$;

do $$ begin
  execute 'create policy "anon_read_hormuz_ai_brief" on hormuz_ai_brief for select to anon using (true)';
exception when duplicate_object then null;
end $$;
