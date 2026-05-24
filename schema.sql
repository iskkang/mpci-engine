-- ================================================================
-- MPCI Engine — Supabase Schema v1
-- ================================================================

-- 1. 항만 최신 현황 (AIS 기반, 항만당 1행 유지)
CREATE TABLE IF NOT EXISTS port_snapshots (
  port_code        VARCHAR(10)  PRIMARY KEY,
  port_name        VARCHAR(100),
  country          VARCHAR(5),
  region           VARCHAR(30),
  lat              FLOAT,
  lon              FLOAT,
  updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  vessels_anchored INTEGER      DEFAULT 0,
  vessels_berthed  INTEGER      DEFAULT 0,
  tpfs             FLOAT        DEFAULT 0,
  level            VARCHAR(20)  DEFAULT 'LOW'
);

-- 2. AIS 시계열 이력
CREATE TABLE IF NOT EXISTS port_history (
  id               BIGSERIAL    PRIMARY KEY,
  port_code        VARCHAR(10)  NOT NULL,
  snapshot_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  vessels_anchored INTEGER      DEFAULT 0,
  vessels_berthed  INTEGER      DEFAULT 0,
  tpfs             FLOAT        DEFAULT 0,
  level            VARCHAR(20)  DEFAULT 'LOW'
);
CREATE INDEX IF NOT EXISTS idx_history_port_time
  ON port_history (port_code, snapshot_at DESC);

-- 3. 7일 롤링 기준선 (anomaly 비교용)
CREATE TABLE IF NOT EXISTS port_baselines (
  port_code         VARCHAR(10)  PRIMARY KEY,
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  avg_anchored_7d   FLOAT        DEFAULT 0,
  avg_berthed_7d    FLOAT        DEFAULT 0,
  avg_tpfs_7d       FLOAT        DEFAULT 0,
  sample_count      INTEGER      DEFAULT 0
);

-- 4. PortWatch 초크포인트 일별 데이터
CREATE TABLE IF NOT EXISTS chokepoint_daily (
  id               BIGSERIAL    PRIMARY KEY,
  portid           VARCHAR(30)  NOT NULL,
  portname         VARCHAR(100),
  recorded_date    DATE         NOT NULL,
  n_container      INTEGER      DEFAULT 0,
  n_total          INTEGER      DEFAULT 0,
  capacity_container BIGINT     DEFAULT 0,
  fetched_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  UNIQUE (portid, recorded_date)
);
CREATE INDEX IF NOT EXISTS idx_chokepoint_portid_date
  ON chokepoint_daily (portid, recorded_date DESC);

-- 5. 이상 플래그
CREATE TABLE IF NOT EXISTS anomaly_flags (
  id               BIGSERIAL    PRIMARY KEY,
  source           VARCHAR(20)  NOT NULL,
  target_code      VARCHAR(30)  NOT NULL,
  target_name      VARCHAR(100),
  detected_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  severity         VARCHAR(10)  NOT NULL,
  ratio            FLOAT,
  detail           TEXT,
  acknowledged     BOOLEAN      DEFAULT FALSE,
  ack_at           TIMESTAMPTZ,
  ack_note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_anomaly_target_time
  ON anomaly_flags (target_code, detected_at DESC);

-- 6. 관리자 수동 오버라이드
CREATE TABLE IF NOT EXISTS port_overrides (
  id               BIGSERIAL    PRIMARY KEY,
  port_code        VARCHAR(10)  NOT NULL,
  delay_days       INTEGER      NOT NULL,
  reason           VARCHAR(50),
  memo             TEXT,
  start_date       DATE         NOT NULL,
  end_date         DATE,
  created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  is_active        BOOLEAN      DEFAULT TRUE
);

-- RLS 설정
ALTER TABLE port_snapshots     ENABLE ROW LEVEL SECURITY;
ALTER TABLE port_history       ENABLE ROW LEVEL SECURITY;
ALTER TABLE port_baselines     ENABLE ROW LEVEL SECURITY;
ALTER TABLE chokepoint_daily   ENABLE ROW LEVEL SECURITY;
ALTER TABLE anomaly_flags      ENABLE ROW LEVEL SECURITY;
ALTER TABLE port_overrides     ENABLE ROW LEVEL SECURITY;

-- anon 읽기 허용 (프론트엔드)
CREATE POLICY "anon_read" ON port_snapshots   FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON port_history     FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON port_baselines   FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON chokepoint_daily FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON anomaly_flags    FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON port_overrides   FOR SELECT TO anon USING (true);

-- service_role 쓰기 허용
CREATE POLICY "svc_all" ON port_snapshots   FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON port_history     FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON port_baselines   FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON chokepoint_daily FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON anomaly_flags    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON port_overrides   FOR ALL TO service_role USING (true) WITH CHECK (true);

-- EconDB 컬럼 추가 (fetch_econdb.py 에서 사용)
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS econdb_congestion    FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS econdb_delay_pct     FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS econdb_turnaround    FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS econdb_updated_at    TIMESTAMPTZ;
