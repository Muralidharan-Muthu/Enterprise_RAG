-- 017_table_structured_content.sql
-- Universal VLM table pipeline: persist the VLM structured_content (the clean,
-- retrieval-ready extraction — the same quality column image_store already has)
-- on table_store, plus its own embedding for vector search.
--
--   structured_content            — VLM structured extraction for the table,
--                                    falling back to markdown_text/raw_text when
--                                    the VLM produced nothing. Primary content
--                                    surfaced in the UI.
--   structured_content_embedding  — BGE bge-large-en-v1.5 (1024-dim) vector of
--                                    structured_content. Additive to the existing
--                                    table_summary-based `embedding` column;
--                                    retrieval prefers it via COALESCE.
--
-- Run in Supabase SQL Editor AFTER the prior migrations. Apply BEFORE reprocessing
-- documents, or the table_store INSERT (which now writes these columns) will fail.

SET search_path TO multi_store_rag_working, public, extensions;

ALTER TABLE multi_store_rag_working.table_store
    ADD COLUMN IF NOT EXISTS structured_content           TEXT,
    ADD COLUMN IF NOT EXISTS structured_content_embedding vector(1024);

-- HNSW index for ANN search on the structured_content embedding.
CREATE INDEX IF NOT EXISTS idx_table_store_sc_embedding
    ON multi_store_rag_working.table_store
    USING hnsw (structured_content_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
