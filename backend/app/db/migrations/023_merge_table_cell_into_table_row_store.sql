-- ============================================================
-- Multi-Store RAG Chatbot — Migration 023: Merge table_cell_store into table_row_store
-- Schema: multi_store_rag_working
-- ============================================================

SET search_path TO multi_store_rag_working, public, extensions;

-- 1. Alter table_row_store to hold structured numeric metrics, raw text, and embeddings
ALTER TABLE multi_store_rag_working.table_row_store
    ADD COLUMN IF NOT EXISTS row_numeric JSONB DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS row_text TEXT,
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- 2. Indexes on table_row_store for fast JSONB pushdown and text matching
CREATE INDEX IF NOT EXISTS idx_table_row_store_numeric_gin
    ON multi_store_rag_working.table_row_store USING gin (row_numeric jsonb_path_ops);

CREATE INDEX IF NOT EXISTS idx_table_row_store_data_gin
    ON multi_store_rag_working.table_row_store USING gin (row_data);

-- 3. Drop un-normalized cell store (superseded by enriched table_row_store)
DROP TABLE IF EXISTS multi_store_rag_working.table_cell_store CASCADE;
