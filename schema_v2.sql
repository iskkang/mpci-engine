-- ================================================================
-- MPCI Engine — Schema v2 마이그레이션
-- Supabase SQL Editor에서 실행
-- ================================================================

-- 선사별 항만 적시율 (EconDB 이력 기반 사전 계산)
CREATE TABLE IF NOT EXISTS carrier_port_ontime (
  id            BIGSERIAL    PRIMARY KEY,
  carrier       VARCHAR(50)  NOT NULL,
  port_code     VARCHAR(10)  NOT NULL,
  ontime_pct    FLOAT        NOT NULL DEFAULT 0.5,  -- 0.0 ~ 1.0
  sample_count  INTEGER      DEFAULT 0,
  updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (carrier, port_code)
);

-- ETA 계산 이력 (API 호출 결과 누적)
CREATE TABLE IF NOT EXISTS eta_calculations (
  id               BIGSERIAL    PRIMARY KEY,
  calculated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  -- 입력값
  port_code        VARCHAR(10)  NOT NULL,
  carrier          VARCHAR(50),
  etd              DATE,
  distance_nm      FLOAT,
  -- 계산 중간값
  t_base_hours     FLOAT,
  delta_weather_h  FLOAT,
  delta_port_h     FLOAT,
  delta_override_h FLOAT        DEFAULT 0,
  sigma_hours      FLOAT,
  mpci_at_calc     FLOAT,
  -- 출력값
  p10_datetime     TIMESTAMPTZ,
  p50_datetime     TIMESTAMPTZ,
  p90_datetime     TIMESTAMPTZ,
  -- 메타
  source           VARCHAR(20)  DEFAULT 'api'  -- 'api' | 'mtl_link'
);

-- RLS
ALTER TABLE carrier_port_ontime  ENABLE ROW LEVEL SECURITY;
ALTER TABLE eta_calculations     ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read" ON carrier_port_ontime  FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON eta_calculations     FOR SELECT TO anon USING (true);
CREATE POLICY "svc_all"   ON carrier_port_ontime  FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all"   ON eta_calculations     FOR ALL TO service_role USING (true) WITH CHECK (true);

-- 초기 선사 적시율 데이터 (EconDB 이력 기반 추정값)
INSERT INTO carrier_port_ontime (carrier, port_code, ontime_pct, sample_count) VALUES
  ('Hapag-Lloyd', 'SGSIN', 0.88, 100), ('Hapag-Lloyd', 'KRPUS', 0.82, 100),
  ('Hapag-Lloyd', 'NLRTM', 0.78, 100), ('Hapag-Lloyd', 'BEANR', 0.80, 100),
  ('Hapag-Lloyd', 'INNSA', 0.70, 100), ('Hapag-Lloyd', 'USLAX', 0.86, 100),
  ('Maersk',      'SGSIN', 0.82, 100), ('Maersk',      'KRPUS', 0.76, 100),
  ('Maersk',      'NLRTM', 0.80, 100), ('Maersk',      'BEANR', 0.82, 100),
  ('Maersk',      'INNSA', 0.64, 100), ('Maersk',      'USLAX', 0.80, 100),
  ('MSC',         'SGSIN', 0.70, 100), ('MSC',         'KRPUS', 0.64, 100),
  ('MSC',         'NLRTM', 0.68, 100), ('MSC',         'BEANR', 0.66, 100),
  ('MSC',         'INNSA', 0.56, 100), ('MSC',         'USLAX', 0.67, 100),
  ('CMA CGM',     'SGSIN', 0.66, 100), ('CMA CGM',     'KRPUS', 0.60, 100),
  ('CMA CGM',     'NLRTM', 0.64, 100), ('CMA CGM',     'BEANR', 0.62, 100),
  ('CMA CGM',     'INNSA', 0.52, 100), ('CMA CGM',     'USLAX', 0.65, 100),
  ('Evergreen',   'SGSIN', 0.60, 100), ('Evergreen',   'KRPUS', 0.54, 100),
  ('Evergreen',   'NLRTM', 0.58, 100), ('Evergreen',   'BEANR', 0.56, 100),
  ('Evergreen',   'INNSA', 0.46, 100), ('Evergreen',   'USLAX', 0.55, 100),
  ('ONE',         'SGSIN', 0.54, 100), ('ONE',         'KRPUS', 0.48, 100),
  ('ONE',         'NLRTM', 0.52, 100), ('ONE',         'BEANR', 0.50, 100),
  ('ONE',         'INNSA', 0.40, 100), ('ONE',         'USLAX', 0.48, 100),
  ('HMM',         'SGSIN', 0.50, 100), ('HMM',         'KRPUS', 0.70, 100),
  ('HMM',         'NLRTM', 0.55, 100), ('HMM',         'BEANR', 0.58, 100),
  ('HMM',         'INNSA', 0.44, 100), ('HMM',         'USLAX', 0.52, 100),
  ('ZIM',         'SGSIN', 0.74, 100), ('ZIM',         'KRPUS', 0.68, 100),
  ('ZIM',         'NLRTM', 0.72, 100), ('ZIM',         'BEANR', 0.70, 100),
  ('ZIM',         'INNSA', 0.60, 100), ('ZIM',         'USLAX', 0.85, 100)
ON CONFLICT (carrier, port_code) DO NOTHING;
