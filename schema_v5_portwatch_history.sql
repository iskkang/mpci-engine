-- ================================================================
-- MPCI Engine - Schema v5: PortWatch port-level historical baseline
-- Supabase SQL Editor에서 실행
-- ================================================================

ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_portid VARCHAR(30);
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_updated_at TIMESTAMPTZ;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_history_days INTEGER DEFAULT 0;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_recent_activity FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_previous_activity FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_activity_percentile FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_trend_index FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_historic_index FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS portwatch_activity_delta_pct FLOAT;

CREATE TABLE IF NOT EXISTS portwatch_port_map (
  port_code          VARCHAR(10) PRIMARY KEY,
  portwatch_portid   VARCHAR(30) NOT NULL,
  portwatch_name     TEXT,
  portwatch_country  TEXT,
  portwatch_iso3     VARCHAR(3),
  locode             VARCHAR(20),
  match_method       VARCHAR(30),
  match_score        FLOAT,
  distance_km        FLOAT,
  is_active          BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS portwatch_port_daily (
  id                      BIGSERIAL PRIMARY KEY,
  port_code               VARCHAR(10) NOT NULL,
  portwatch_portid         VARCHAR(30) NOT NULL,
  recorded_date           DATE NOT NULL,
  portcalls               INTEGER,
  portcalls_container     INTEGER,
  portcalls_dry_bulk      INTEGER,
  portcalls_general_cargo INTEGER,
  portcalls_roro          INTEGER,
  portcalls_tanker        INTEGER,
  portcalls_cargo         INTEGER,
  import_total            INTEGER,
  export_total            INTEGER,
  import_container        INTEGER,
  export_container        INTEGER,
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (port_code, recorded_date)
);

CREATE INDEX IF NOT EXISTS idx_portwatch_port_daily_port_date
  ON portwatch_port_daily (port_code, recorded_date DESC);

CREATE INDEX IF NOT EXISTS idx_portwatch_port_daily_portwatch_id
  ON portwatch_port_daily (portwatch_portid, recorded_date DESC);

ALTER TABLE portwatch_port_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE portwatch_port_daily ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read" ON portwatch_port_map
  FOR SELECT TO anon USING (true);

CREATE POLICY "svc_all" ON portwatch_port_map
  FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "anon_read" ON portwatch_port_daily
  FOR SELECT TO anon USING (true);

CREATE POLICY "svc_all" ON portwatch_port_daily
  FOR ALL TO service_role USING (true) WITH CHECK (true);
