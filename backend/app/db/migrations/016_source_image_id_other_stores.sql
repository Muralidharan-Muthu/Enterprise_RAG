-- ============================================================
-- Multi-Store RAG Chatbot — Migration 016: source_image_id for clause_store,
-- document_store, vector_store
--
-- Apply manually via the Supabase SQL Editor (no Alembic in this project;
-- see CLAUDE.md "DB Layer"). Idempotent: every ADD COLUMN / CREATE INDEX
-- uses IF NOT EXISTS, so re-running this file is always safe.
--
-- Context: migration 014 added table_store.source_image_id as a real FK to
-- image_store.id, replacing the fragile index-offset convention. The same
-- cross-store image routing (store_router.py, STORE_REGISTRY) also writes
-- image-derived rows into clause_store, document_store, and vector_store,
-- but those three never got the equivalent FK column — lineage for those
-- rows was only reachable by parsing the *_metadata JSONB traceability dict
-- (source_image_id embedded as a string, not queryable/joinable). This
-- migration closes that gap for all four cross-store destinations.
--
-- NO behavior change from this file alone — see store_router.py's
-- ClauseStoreHandler.insert() / VectorStoreHandler.insert() /
-- DocumentStoreHandler.insert() (companion code change, same commit) for
-- where the new column is actually populated.
-- ============================================================

SET search_path TO multi_store_rag_working, public;

-- ------------------------------------------------------------
-- source_image_id columns
--
-- Each image_store row is routed to exactly ONE destination store
-- (store_router's per-image worklist produces a single INSERT per row), so a
-- 1:1 relationship holds here just as it does for table_store — see the
-- uq_table_store_source_image partial unique index in migration 014.
-- ------------------------------------------------------------

ALTER TABLE clause_store
    ADD COLUMN IF NOT EXISTS source_image_id UUID
        REFERENCES multi_store_rag_working.image_store(id) ON DELETE SET NULL;

COMMENT ON COLUMN multi_store_rag_working.clause_store.source_image_id IS
    'FK to image_store.id — the image this clause row was extracted from via '
    'cross-store routing (store_router.ClauseStoreHandler), when applicable.';

ALTER TABLE document_store
    ADD COLUMN IF NOT EXISTS source_image_id UUID
        REFERENCES multi_store_rag_working.image_store(id) ON DELETE SET NULL;

COMMENT ON COLUMN multi_store_rag_working.document_store.source_image_id IS
    'FK to image_store.id — the image this chunk was extracted from via '
    'cross-store routing (store_router.DocumentStoreHandler), when applicable.';

ALTER TABLE vector_store
    ADD COLUMN IF NOT EXISTS source_image_id UUID
        REFERENCES multi_store_rag_working.image_store(id) ON DELETE SET NULL;

COMMENT ON COLUMN multi_store_rag_working.vector_store.source_image_id IS
    'FK to image_store.id — the image this chunk was extracted from via '
    'cross-store routing (store_router.VectorStoreHandler), when applicable.';

-- ------------------------------------------------------------
-- Lookup indexes.
-- ------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_clause_store_source_image_id
    ON multi_store_rag_working.clause_store(source_image_id);

CREATE INDEX IF NOT EXISTS idx_document_store_source_image_id
    ON multi_store_rag_working.document_store(source_image_id);

CREATE INDEX IF NOT EXISTS idx_vector_store_source_image_id
    ON multi_store_rag_working.vector_store(source_image_id);

-- ------------------------------------------------------------
-- Integrity: one image_store row backs at most one row per destination
-- store. Partial indexes (WHERE source_image_id IS NOT NULL) so
-- normal-pipeline rows (source_image_id always NULL) are unaffected.
-- ------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS uq_clause_store_source_image
    ON multi_store_rag_working.clause_store(source_image_id)
    WHERE source_image_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_store_source_image
    ON multi_store_rag_working.document_store(source_image_id)
    WHERE source_image_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_vector_store_source_image
    ON multi_store_rag_working.vector_store(source_image_id)
    WHERE source_image_id IS NOT NULL;
