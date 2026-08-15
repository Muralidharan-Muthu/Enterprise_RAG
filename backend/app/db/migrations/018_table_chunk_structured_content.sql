-- 018_table_chunk_structured_content.sql
-- Per-window structured_content for LARGE tables (row_count > TABLE_CHUNK_MAX_ROWS,
-- default 25). A big table's whole-table structured_content_embedding on table_store
-- is a single vector diluted across every row, so semantic retrieval on it is weak.
-- These columns hold, per ≤25-row child window, a JSON slice of that window's
-- canonical rows plus its own embedding — so a query can match (and surface) the
-- structured view at row-window granularity instead of the diluted whole-table one.
--
--   structured_content            — JSON slice {"title", "headers", "rows"} for this
--                                    window's canonical rows (row_start..row_end).
--                                    NULL for tables with <= TABLE_CHUNK_MAX_ROWS rows
--                                    (their whole structured_content already lives on
--                                    table_store — a single window suffices).
--   structured_content_embedding  — BGE bge-large-en-v1.5 (1024-dim) vector of
--                                    structured_content. Retrieval prefers it via
--                                    COALESCE(structured_content_embedding, embedding).
--
-- Run in Supabase SQL Editor AFTER the prior migrations. Apply BEFORE reprocessing
-- documents, or the table_chunk_store INSERT (which now writes these columns) will fail.

SET search_path TO multi_store_rag_working, public, extensions;

ALTER TABLE multi_store_rag_working.table_chunk_store
    ADD COLUMN IF NOT EXISTS structured_content           TEXT,
    ADD COLUMN IF NOT EXISTS structured_content_embedding vector(1024);

-- HNSW index for ANN search on the structured_content embedding.
CREATE INDEX IF NOT EXISTS idx_table_chunk_store_sc_embedding
    ON multi_store_rag_working.table_chunk_store
    USING hnsw (structured_content_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
