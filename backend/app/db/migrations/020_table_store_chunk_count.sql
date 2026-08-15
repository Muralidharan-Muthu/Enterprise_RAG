-- ============================================================
-- Multi-Store RAG Chatbot — Migration 020: table_store.chunk_count
--
-- Adds a denormalized count of table_chunk_store children per parent
-- table_store row, so callers can tell how many row-windows a table was
-- split into without a JOIN/COUNT against table_chunk_store.
--
-- Run this in Supabase SQL Editor AFTER 012_table_chunk_store.sql.
-- Schema: multi_store_rag_working
-- ============================================================

SET search_path TO multi_store_rag_working, public, extensions;

ALTER TABLE multi_store_rag_working.table_store
    ADD COLUMN IF NOT EXISTS chunk_count INT NOT NULL DEFAULT 0;

-- Backfill existing rows from current table_chunk_store contents.
UPDATE multi_store_rag_working.table_store ts
SET chunk_count = tc.n
FROM (
    SELECT table_id, COUNT(*) AS n
    FROM multi_store_rag_working.table_chunk_store
    GROUP BY table_id
) tc
WHERE tc.table_id = ts.id;
