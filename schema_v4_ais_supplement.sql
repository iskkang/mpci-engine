-- ================================================================
-- MPCI Engine - Schema v4: AIS supplemental signal
-- Supabase SQL Editor에서 실행
-- ================================================================

ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_recent_anchored_avg FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_recent_berthed_avg FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_baseline_anchored_avg FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_wait_ratio FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_wait_index FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_anomaly_level VARCHAR(20);
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_sample_count_7d INTEGER DEFAULT 0;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS ais_updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS port_ais_daily (
  id                    BIGSERIAL PRIMARY KEY,
  port_code             VARCHAR(10) NOT NULL,
  observed_date         DATE NOT NULL,
  avg_vessels_anchored  FLOAT DEFAULT 0,
  avg_vessels_berthed   FLOAT DEFAULT 0,
  max_vessels_anchored  INTEGER DEFAULT 0,
  max_vessels_berthed   INTEGER DEFAULT 0,
  avg_tpfs              FLOAT DEFAULT 0,
  sample_count          INTEGER DEFAULT 0,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (port_code, observed_date)
);

CREATE INDEX IF NOT EXISTS idx_port_ais_daily_port_date
  ON port_ais_daily (port_code, observed_date DESC);

ALTER TABLE port_ais_daily ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read" ON port_ais_daily
  FOR SELECT TO anon USING (true);

CREATE POLICY "svc_all" ON port_ais_daily
  FOR ALL TO service_role USING (true) WITH CHECK (true);
