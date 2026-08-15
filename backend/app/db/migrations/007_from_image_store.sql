-- 007_from_image_store.sql
-- Adds a uniform `from_image_store` boolean to every store so any row can be
-- traced to whether it originated from an image extraction (connectivity/audit).
--
-- Apply manually via the Supabase SQL Editor (no Alembic in this project; see
-- CLAUDE.md "DB Layer"). Idempotent: ADD COLUMN IF NOT EXISTS + DEFAULT means
-- re-running is safe and existing rows backfill to the default.
--
-- Semantics:
--   vector_store / table_store / clause_store  -> DEFAULT FALSE
--       Most rows come from normal text/table/clause extraction. The image
--       pipeline flips this to TRUE only on rows it cross-stores
--       (_store_image_as_table / _text_chunk / _clause, and the table-crop path).
--   image_store -> DEFAULT TRUE
--       Every image_store row is, by definition, an image.

SET search_path TO multi_store_rag_working;

ALTER TABLE vector_store   ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE table_store    ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE clause_store   ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE document_store ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE image_store    ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT TRUE;

-- Optional helper indexes for audit queries that filter by origin.
CREATE INDEX IF NOT EXISTS idx_vector_store_from_image   ON vector_store   (from_image_store);
CREATE INDEX IF NOT EXISTS idx_table_store_from_image    ON table_store    (from_image_store);
CREATE INDEX IF NOT EXISTS idx_clause_store_from_image   ON clause_store   (from_image_store);
CREATE INDEX IF NOT EXISTS idx_document_store_from_image ON document_store (from_image_store);
