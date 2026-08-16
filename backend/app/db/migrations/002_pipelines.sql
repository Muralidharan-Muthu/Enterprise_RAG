-- ============================================================
-- Multi-Store RAG Chatbot — Migration 002: Pipeline Runs
-- Adds the concept of a "pipeline run": one upload batch carrying
-- name/description + taxonomy (domain/sub-domain/category/sub-category)
-- and grouping the documents loaded together.
-- Run this AFTER 001_initial_schema.sql.
-- Schema: multi_store_rag_working
-- ============================================================

SET search_path TO multi_store_rag_working, public, extensions;

-- ============================================================
-- TABLE: pipeline_runs
-- One row per upload batch. Documents reference it via FK.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.pipeline_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    name            TEXT NOT NULL,
    description     TEXT,

    -- Where the files came from: 'local' | 'gdrive' | 'sharepoint'
    source          TEXT NOT NULL DEFAULT 'local',

    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_created
    ON multi_store_rag_working.pipeline_runs(created_at DESC);

-- ============================================================
-- Link documents to their pipeline run.
-- ON DELETE SET NULL: deleting a run keeps the documents.
-- ============================================================
ALTER TABLE multi_store_rag_working.document_registry
    ADD COLUMN IF NOT EXISTS pipeline_run_id UUID
    REFERENCES multi_store_rag_working.pipeline_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_doc_registry_pipeline
    ON multi_store_rag_working.document_registry(pipeline_run_id);

-- ============================================================
-- updated_at trigger (reuses set_updated_at from migration 001)
-- ============================================================
DROP TRIGGER IF EXISTS trg_pipeline_runs_updated_at ON multi_store_rag_working.pipeline_runs;
CREATE TRIGGER trg_pipeline_runs_updated_at
    BEFORE UPDATE ON multi_store_rag_working.pipeline_runs
    FOR EACH ROW EXECUTE FUNCTION multi_store_rag_working.set_updated_at();

-- ============================================================
-- VIEW: pipeline_run_overview
-- Computes per-run file counts + a derived run status.
-- Used by GET /api/v1/ingest/pipelines (the run history table).
--   found      = total documents in the run
--   processed  = documents that reached 'completed'
--   failed     = documents that ended in 'failed'
--   status     = 'empty' | 'running' | 'completed' | 'failed'
-- ============================================================
CREATE OR REPLACE VIEW multi_store_rag_working.pipeline_run_overview AS
SELECT
    p.id,
    p.name,
    p.description,
    p.source,
    p.started_at,
    p.created_at,
    COUNT(d.id)                                                        AS files_found,
    COUNT(*) FILTER (WHERE d.status = 'completed')                     AS files_processed,
    COUNT(*) FILTER (WHERE d.status = 'failed')                        AS files_failed,
    CASE
        WHEN COUNT(d.id) = 0 THEN 'empty'
        WHEN COUNT(*) FILTER (WHERE d.status NOT IN ('completed', 'failed')) > 0 THEN 'running'
        WHEN COUNT(*) FILTER (WHERE d.status = 'failed') = COUNT(d.id) THEN 'failed'
        ELSE 'completed'
    END                                                               AS status
FROM multi_store_rag_working.pipeline_runs p
LEFT JOIN multi_store_rag_working.document_registry d ON d.pipeline_run_id = p.id
GROUP BY p.id;
