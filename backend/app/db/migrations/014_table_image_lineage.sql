-- ============================================================
-- Multi-Store RAG Chatbot — Migration 014: Table/Image Lineage (Slice 1)
--
-- Apply manually via the Supabase SQL Editor (no Alembic in this project;
-- see CLAUDE.md "DB Layer"). Idempotent: every ADD COLUMN / CREATE INDEX
-- uses IF NOT EXISTS, so re-running this file is always safe.
--
-- NOTE (unique index prerequisite): the UNIQUE INDEX created below on
-- table_store(document_id, table_index) will FAIL TO CREATE if any
-- (document_id, table_index) pair already has duplicate rows. Before
-- applying this migration, run the pre-check in
-- backend/scripts/backfill_014_table_lineage.py (see
-- check_duplicate_table_index()) against the live DB and resolve any
-- duplicates it reports. This file does not delete/merge rows itself.
--
-- Context: image_store is the canonical asset registry for rendered
-- visuals (figures + table crops). table_store is a derived semantic
-- view. Today the two are linked only by a fragile convention
-- (image_store.image_index = 20000 + table_store.table_index) and a
-- JSONB field (table_store.table_metadata->>'source_image_id'). This
-- migration lays additive schema for a real FK link + extraction
-- provenance. NO behavior change — see backend/scripts/backfill_014_table_lineage.py
-- for the follow-up backfill, and storage_service.py for the (default-only)
-- asset_role stamping wired up at insert time in this same slice.
-- ============================================================

SET search_path TO multi_store_rag_working, public;

-- ------------------------------------------------------------
-- image_store.asset_role
-- Coarse classification of what an image_store row visually is.
--   'figure'      — a rendered figure/chart/photo/diagram extracted from the page
--   'table_crop'  — a visual crop mirroring a table that also lives in table_store
-- ------------------------------------------------------------
ALTER TABLE image_store ADD COLUMN IF NOT EXISTS asset_role TEXT;

COMMENT ON COLUMN multi_store_rag_working.image_store.asset_role IS
    'Coarse role of this asset: figure | table_crop. Populated at insert time '
    '(storage_service._image_rows / store_table_crop_images); backfilled for '
    'pre-existing rows by backfill_014_table_lineage.py.';

-- ------------------------------------------------------------
-- table_store.source_image_id
-- Real FK replacing the fragile "image_index = 20000 + table_index" convention
-- and the JSONB table_metadata->>'source_image_id' pointer. Nullable: not every
-- table_store row (e.g. pure Docling-grid extractions with no crop) has a
-- corresponding image_store asset.
-- ------------------------------------------------------------
ALTER TABLE table_store
    ADD COLUMN IF NOT EXISTS source_image_id UUID
        REFERENCES multi_store_rag_working.image_store(id) ON DELETE SET NULL;

COMMENT ON COLUMN multi_store_rag_working.table_store.source_image_id IS
    'FK to image_store.id — the table-crop image this row was extracted from '
    '(when applicable). Replaces the image_index=20000+table_index convention '
    'and the table_metadata JSONB pointer as the source of truth going forward.';

-- ------------------------------------------------------------
-- table_store.extraction_method
-- How this table's structured content was produced.
--   'pdf_grid'       — Docling's native PDF table-grid extraction
--   'image_vlm'      — VLM transcription of a rendered table image
--   'image_ocr'      — plain OCR of a rendered table image (no VLM)
--   'vlm_gapfilled'  — pdf_grid extraction with VLM-filled gaps/corrections
-- ------------------------------------------------------------
ALTER TABLE table_store ADD COLUMN IF NOT EXISTS extraction_method TEXT;

COMMENT ON COLUMN multi_store_rag_working.table_store.extraction_method IS
    'How the table content was produced: pdf_grid | image_vlm | image_ocr | '
    'vlm_gapfilled. Populated going forward at insert time; backfilled for '
    'pre-existing rows by backfill_014_table_lineage.py from from_image_store.';

-- ------------------------------------------------------------
-- table_store.extraction_quality
-- Coarse confidence bucket for the extraction, independent of source_confidence's
-- continuous score — useful for quick filtering/alerts.
-- ------------------------------------------------------------
ALTER TABLE table_store ADD COLUMN IF NOT EXISTS extraction_quality TEXT;

COMMENT ON COLUMN multi_store_rag_working.table_store.extraction_quality IS
    'Coarse extraction confidence bucket: high | medium | low.';

-- ------------------------------------------------------------
-- table_store.source_confidence
-- Continuous confidence score (0.0-1.0) from the extraction step, when available.
-- ------------------------------------------------------------
ALTER TABLE table_store ADD COLUMN IF NOT EXISTS source_confidence REAL;

COMMENT ON COLUMN multi_store_rag_working.table_store.source_confidence IS
    'Continuous 0.0-1.0 confidence score from the extraction step, when available.';

-- ------------------------------------------------------------
-- table_store.provenance
-- Free-form structured audit trail of how this row's content was derived
-- (e.g. which passes ran, gap-fill details, model versions). Additive JSONB
-- so future slices can extend it without another migration.
-- ------------------------------------------------------------
ALTER TABLE table_store ADD COLUMN IF NOT EXISTS provenance JSONB DEFAULT '{}'::jsonb;

COMMENT ON COLUMN multi_store_rag_working.table_store.provenance IS
    'Free-form structured extraction audit trail (pass history, gap-fill '
    'details, model versions, etc). Additive JSONB — safe to extend later '
    'without a new migration.';

-- ------------------------------------------------------------
-- Indexes for the new lookup/filter columns.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_table_store_source_image_id
    ON multi_store_rag_working.table_store(source_image_id);

CREATE INDEX IF NOT EXISTS idx_table_store_fiscal_year
    ON multi_store_rag_working.table_store(fiscal_year);

CREATE INDEX IF NOT EXISTS idx_table_store_currency
    ON multi_store_rag_working.table_store(currency);

CREATE INDEX IF NOT EXISTS idx_table_store_table_category
    ON multi_store_rag_working.table_store(table_category);

-- ------------------------------------------------------------
-- Integrity constraints.
--
-- uq_table_store_doc_tableidx: enforces the invariant that every
-- (document_id, table_index) pair is unique — this is the correlation key the
-- rest of the pipeline already assumes (e.g. the 20000+table_index image_store
-- convention). Requires no pre-existing duplicates; see the NOTE at the top of
-- this file and backfill_014_table_lineage.py's pre-check.
--
-- uq_table_store_source_image: enforces a 1:1 relationship between an
-- image_store row and the table_store row it produced (a crop image should
-- back exactly one table). Partial index (WHERE source_image_id IS NOT NULL)
-- so rows without a source image (e.g. pure pdf_grid tables) are unaffected.
-- ------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_table_store_doc_tableidx
    ON multi_store_rag_working.table_store(document_id, table_index);

CREATE UNIQUE INDEX IF NOT EXISTS uq_table_store_source_image
    ON multi_store_rag_working.table_store(source_image_id)
    WHERE source_image_id IS NOT NULL;
