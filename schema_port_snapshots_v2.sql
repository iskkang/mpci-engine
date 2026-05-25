-- ============================================================
-- MPCI Engine — port_snapshots 컬럼 추가 (v2)
-- 지도 좌표 + EconDB 신규 통계 컬럼
-- Supabase SQL Editor에서 한 번 실행 (멱등)
-- ============================================================

ALTER TABLE port_snapshots
  ADD COLUMN IF NOT EXISTS lat                double precision,
  ADD COLUMN IF NOT EXISTS lon                double precision,
  ADD COLUMN IF NOT EXISTS econdb_rank        integer,
  ADD COLUMN IF NOT EXISTS econdb_region      text,
  ADD COLUMN IF NOT EXISTS port_congestion_raw double precision,
  ADD COLUMN IF NOT EXISTS last_import_teu    double precision,
  ADD COLUMN IF NOT EXISTS last_export_teu    double precision,
  ADD COLUMN IF NOT EXISTS vessels_berthed    integer,
  ADD COLUMN IF NOT EXISTS schedule_count     integer;

-- 인덱스: 좌표가 있는 항만만 빠르게 조회
CREATE INDEX IF NOT EXISTS idx_port_snapshots_lat_lon
  ON port_snapshots (lat, lon)
  WHERE lat IS NOT NULL AND lon IS NOT NULL;
