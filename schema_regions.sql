-- ================================================================
-- MPCI Engine — Schema: EconDB Region 지표 (1/2)
-- Supabase SQL Editor에서 실행
-- 멱등: IF NOT EXISTS / on_conflict 전제
--
-- 포함 테이블:
--   econdb_port_catalog       — region별 항만 디렉터리 (alias 자동화)
--   region_yard_dwell_weekly  — 항만별(상위 8개) + OTHERS 야드 적체 주간
--   region_congestion_weekly  — region 집계 + 시즌 밴드 + 적체 지수 주간
--   region_omissions_weekly   — 블랭크세일링 주간 (미래=announced 전망 포함)
--   region_schedule_profile   — 선석 점유 (미래=스케줄 예약 포함)
--   region_index              — region별 파생 지표 집약 (룰 기반)
--
-- port_snapshots 컬럼 추가:
--   yard_congestion_index     — 상위 8개 항만 야드 지수 (2편 ETA 사용)
--   yard_congestion_at        — 야드 지수 업데이트 시각
-- ================================================================

-- ── 1. EconDB 항만 카탈로그 ──────────────────────────────────────────────────
-- congestion_region_ports 응답 → locode/region/port_name 매핑.
-- 같은 locode가 여러 region에 등장할 수 있으므로 (locode, region) PK.
-- locode는 ECONDB_LOCODE_ALIAS 적용 후 내부 코드.
CREATE TABLE IF NOT EXISTS econdb_port_catalog (
  locode      TEXT NOT NULL,
  port_name   TEXT,
  region      TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (locode, region)
);

-- ── 2. 항만별 야드 적체 주간 ──────────────────────────────────────────────────
-- containers-in-terminal widget → 항만별(상위 8개) + 'OTHERS' 합산.
-- dwell_teu_k: 단위 = 천 TEU (widget 원본 단위).
-- is_partial: 최신 주 미완성 표시(week_start > today-7일).
-- per-port 코어 MPCI는 search 유지, 야드는 보강용.
CREATE TABLE IF NOT EXISTS region_yard_dwell_weekly (
  region       TEXT NOT NULL,
  week_start   DATE NOT NULL,
  locode       TEXT NOT NULL,              -- 정규화된 내부 코드 또는 'OTHERS'
  dwell_teu_k  DOUBLE PRECISION,           -- 천 TEU
  is_partial   BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (region, week_start, locode)
);

CREATE INDEX IF NOT EXISTS idx_region_yard_region_week
  ON region_yard_dwell_weekly (region, week_start DESC);

CREATE INDEX IF NOT EXISTS idx_region_yard_locode_week
  ON region_yard_dwell_weekly (locode, week_start DESC);

-- ── 3. Region 집계 + 시즌 밴드 + 적체 지수 주간 ──────────────────────────────
-- containers-in-terminal widget → region 총량 집계.
-- band_min_k / band_max_k: 전체 region 총량의 시즌 밴드(항만별 밴드 아님).
-- congestion_index = clamp((total-min)/(max-min)*100, 0, 100).
-- is_partial: 최신 주 미완성(region_index 계산에서 제외).
CREATE TABLE IF NOT EXISTS region_congestion_weekly (
  region           TEXT NOT NULL,
  week_start       DATE NOT NULL,
  total_dwell_k    DOUBLE PRECISION,
  band_min_k       DOUBLE PRECISION,
  band_max_k       DOUBLE PRECISION,
  congestion_index DOUBLE PRECISION,        -- 0~100
  is_partial       BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (region, week_start)
);

CREATE INDEX IF NOT EXISTS idx_region_congestion_region_week
  ON region_congestion_weekly (region, week_start DESC);

-- ── 4. 블랭크세일링 주간 ─────────────────────────────────────────────────────
-- omissions-time-series widget.
-- is_forecast: week_start > 오늘 → announced blank sailing 전망, 버리지 말 것.
-- blank_pct = blanked_teu / (blanked_teu + actual_teu) * 100.
CREATE TABLE IF NOT EXISTS region_omissions_weekly (
  region      TEXT NOT NULL,
  week_start  DATE NOT NULL,
  blanked_teu DOUBLE PRECISION,
  actual_teu  DOUBLE PRECISION,
  blank_pct   DOUBLE PRECISION,             -- 0~100 (%)
  is_forecast BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (region, week_start)
);

CREATE INDEX IF NOT EXISTS idx_region_omissions_region_week
  ON region_omissions_weekly (region, week_start DESC);

-- ── 5. 선석 점유 스케줄 프로파일 ─────────────────────────────────────────────
-- weekly-schedule-profile widget (4시간 업데이트).
-- is_forecast: ts > now() → 스케줄/예약, 버리지 말 것.
-- yoy_ratio = teu / teu_last_year (0·null 제외).
CREATE TABLE IF NOT EXISTS region_schedule_profile (
  region        TEXT NOT NULL,
  ts            TIMESTAMPTZ NOT NULL,
  teu           DOUBLE PRECISION,
  teu_last_year DOUBLE PRECISION,
  yoy_ratio     DOUBLE PRECISION,           -- teu / teu_last_year
  is_forecast   BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (region, ts)
);

CREATE INDEX IF NOT EXISTS idx_region_schedule_region_ts
  ON region_schedule_profile (region, ts DESC);

-- ── 6. Region 파생 지표 집약 ─────────────────────────────────────────────────
-- region별 최신 완전주 기준 집계 지표.
-- freight_pressure / delay_risk: 룰 기반(임계값 초기 휴리스틱, 보정 필요).
-- narrative_ko / narrative_en: 룰 기반 템플릿 (LLM/날조 금지).
-- data_week: 계산에 사용된 최신 완전주 week_start.
CREATE TABLE IF NOT EXISTS region_index (
  region                   TEXT PRIMARY KEY,
  congestion_index         DOUBLE PRECISION,         -- 0~100
  congestion_trend_pct     DOUBLE PRECISION,         -- 전주 대비 변화율 (%)
  blank_sailing_pct        DOUBLE PRECISION,         -- 블랭크세일링 비율 (%)
  blank_sailing_trend_pp   DOUBLE PRECISION,         -- 전주 대비 변화 (%p)
  blank_sailing_fwd_pct    DOUBLE PRECISION,         -- announced 전망 평균 (%)
  schedule_yoy_pct         DOUBLE PRECISION,         -- 선석 점유 전년비 (%)
  freight_pressure         TEXT,                     -- 'building' | 'neutral' | 'easing'
  delay_risk               TEXT,                     -- 'elevated' | 'moderate' | 'low'
  narrative_ko             TEXT,                     -- 한국어 룰 기반 요약
  narrative_en             TEXT,                     -- 영어 룰 기반 요약
  data_week                DATE,                     -- 지표 기준 완전주
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── port_snapshots 컬럼 추가 (2편 ETA 사용) ──────────────────────────────────
-- 야드 적체 지수: containers-in-terminal 상위 8개 항만만 채워짐.
-- 비상위 항만(HKHKG 등) → NULL 유지.
-- yard_congestion_index: 0~100 (항만 자기 이력 백분위, region 밴드 아님).
ALTER TABLE port_snapshots
  ADD COLUMN IF NOT EXISTS yard_congestion_index DOUBLE PRECISION;

ALTER TABLE port_snapshots
  ADD COLUMN IF NOT EXISTS yard_congestion_at TIMESTAMPTZ;

-- ── RLS (Row Level Security) ─────────────────────────────────────────────────
ALTER TABLE econdb_port_catalog    ENABLE ROW LEVEL SECURITY;
ALTER TABLE region_yard_dwell_weekly ENABLE ROW LEVEL SECURITY;
ALTER TABLE region_congestion_weekly ENABLE ROW LEVEL SECURITY;
ALTER TABLE region_omissions_weekly  ENABLE ROW LEVEL SECURITY;
ALTER TABLE region_schedule_profile  ENABLE ROW LEVEL SECURITY;
ALTER TABLE region_index             ENABLE ROW LEVEL SECURITY;

-- anon 읽기 정책
CREATE POLICY "anon_read" ON econdb_port_catalog
  FOR SELECT TO anon USING (true);

CREATE POLICY "anon_read" ON region_yard_dwell_weekly
  FOR SELECT TO anon USING (true);

CREATE POLICY "anon_read" ON region_congestion_weekly
  FOR SELECT TO anon USING (true);

CREATE POLICY "anon_read" ON region_omissions_weekly
  FOR SELECT TO anon USING (true);

CREATE POLICY "anon_read" ON region_schedule_profile
  FOR SELECT TO anon USING (true);

CREATE POLICY "anon_read" ON region_index
  FOR SELECT TO anon USING (true);

-- service_role 전체 권한 정책
CREATE POLICY "svc_all" ON econdb_port_catalog
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "svc_all" ON region_yard_dwell_weekly
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "svc_all" ON region_congestion_weekly
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "svc_all" ON region_omissions_weekly
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "svc_all" ON region_schedule_profile
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "svc_all" ON region_index
  FOR ALL TO service_role USING (true) WITH CHECK (true);
