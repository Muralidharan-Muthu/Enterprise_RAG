-- 010_image_processing_tracking.sql
-- Records the pre-VLM filtering decision for every extracted image so the
-- pipeline is auditable (which images were skipped, why, and at which stage).
--
-- Apply manually via the Supabase SQL Editor (no Alembic; see CLAUDE.md).
-- Idempotent: ADD COLUMN IF NOT EXISTS.
--
--   processing_status : 'SKIPPED' | 'OCR_ONLY' | 'VLM_PROCESSED'
--   skip_reason       : human-readable reason (NULL when VLM-processed)
--   filter_stage      : 'technical_filter' | 'rule_engine' | 'decision_engine' | NULL
--   image_type        : lightweight classification (logo|icon|blank|separator|
--                       duplicate|corrupted|watermark|text|table|chart|diagram|
--                       screenshot|photo|unknown)

SET search_path TO multi_store_rag_working;

ALTER TABLE image_store ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'VLM_PROCESSED';
ALTER TABLE image_store ADD COLUMN IF NOT EXISTS skip_reason       TEXT;
ALTER TABLE image_store ADD COLUMN IF NOT EXISTS filter_stage      TEXT;
ALTER TABLE image_store ADD COLUMN IF NOT EXISTS image_type        TEXT;

CREATE INDEX IF NOT EXISTS idx_image_store_processing_status
    ON image_store (processing_status);

COMMENT ON COLUMN multi_store_rag_working.image_store.processing_status IS
    'Pre-VLM filter outcome: SKIPPED | OCR_ONLY | VLM_PROCESSED.';
