-- ================================================================
-- MPCI Engine - Schema v3: EconDB current + historical MPCI
-- Supabase SQL Editor에서 실행
-- ================================================================

ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS econdb_current_index FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS historic_percentile_index FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS trend_change_index FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS final_mpci FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_confidence VARCHAR(30);
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_history_days INTEGER DEFAULT 0;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_period_start DATE;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_period_end DATE;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_previous_start DATE;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_previous_end DATE;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_delta_prev FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS mpci_delta_pct_prev FLOAT;
ALTER TABLE port_snapshots ADD COLUMN IF NOT EXISTS econdb_schedule FLOAT;

CREATE TABLE IF NOT EXISTS port_metrics_history (
  id                         BIGSERIAL PRIMARY KEY,
  port_code                  VARCHAR(10) NOT NULL,
  observed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source                     VARCHAR(20) DEFAULT 'econdb',
  econdb_congestion          FLOAT,
  econdb_delay_pct           FLOAT,
  econdb_turnaround          FLOAT,
  econdb_schedule            FLOAT,
  econdb_current_index       FLOAT,
  historic_percentile_index  FLOAT,
  trend_change_index         FLOAT,
  final_mpci                 FLOAT,
  mpci_confidence            VARCHAR(30),
  mpci_history_days          INTEGER DEFAULT 0,
  mpci_delta_prev            FLOAT,
  mpci_delta_pct_prev        FLOAT
);

CREATE INDEX IF NOT EXISTS idx_port_metrics_history_port_time
  ON port_metrics_history (port_code, observed_at DESC);

ALTER TABLE port_metrics_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "svc_all" ON port_metrics_history
  FOR ALL TO service_role USING (true) WITH CHECK (true);
