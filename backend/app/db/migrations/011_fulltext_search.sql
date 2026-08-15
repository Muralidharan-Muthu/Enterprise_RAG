-- ============================================================
-- Multi-Store RAG Chatbot — Migration 007: Full-Text Search (GIN indexes)
-- Adds GENERATED ALWAYS AS tsvector columns + GIN indexes for
-- hybrid keyword + semantic search (Feature 1.1 Agentic RAG).
-- Apply manually via Supabase SQL Editor.
-- Schema: multi_store_rag_working
-- ============================================================

SET search_path TO multi_store_rag_working, public, extensions;

-- ── vector_store ─────────────────────────────────────────────────────────────
-- tsvector column generated from chunk_text (never NULL per schema)
ALTER TABLE multi_store_rag_working.vector_store
    ADD COLUMN IF NOT EXISTS chunk_text_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_vector_store_chunk_text_tsv
    ON multi_store_rag_working.vector_store
    USING GIN(chunk_text_tsv);

-- ── document_store ────────────────────────────────────────────────────────────
-- tsvector column generated from chunk_text
ALTER TABLE multi_store_rag_working.document_store
    ADD COLUMN IF NOT EXISTS chunk_text_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_doc_store_chunk_text_tsv
    ON multi_store_rag_working.document_store
    USING GIN(chunk_text_tsv);

-- ── clause_store ──────────────────────────────────────────────────────────────
-- tsvector column generated from clause_text (NOT NULL per schema)
ALTER TABLE multi_store_rag_working.clause_store
    ADD COLUMN IF NOT EXISTS clause_text_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(clause_text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_clause_store_clause_text_tsv
    ON multi_store_rag_working.clause_store
    USING GIN(clause_text_tsv);

-- ── table_store ───────────────────────────────────────────────────────────────
-- tsvector column generated from table_summary || ' ' || raw_text || ' ' || markdown_text
-- All three columns are nullable so we coalesce each to '' before concatenating.
ALTER TABLE multi_store_rag_working.table_store
    ADD COLUMN IF NOT EXISTS table_text_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(table_summary, '') || ' ' ||
                coalesce(raw_text, '')      || ' ' ||
                coalesce(markdown_text, '')
            )
        ) STORED;

CREATE INDEX IF NOT EXISTS idx_table_store_table_text_tsv
    ON multi_store_rag_working.table_store
    USING GIN(table_text_tsv);

-- image_store intentionally omitted — stays semantic-only for v1.

-- ============================================================
-- Verification queries
-- ============================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_schema = 'multi_store_rag_working'
--   AND column_name LIKE '%_tsv'
-- ORDER BY table_name, column_name;
-- Expected: chunk_text_tsv (vector_store, document_store),
--           clause_text_tsv (clause_store), table_text_tsv (table_store)
