-- ============================================================
-- Migration 022: Remove document_store (research) and table_chunk_store
-- Schema: multi_store_rag_working
-- ============================================================

-- Drop table_chunk_store (superseded by table_store, table_row_store, and table_cell_store)
DROP TABLE IF EXISTS multi_store_rag_working.table_chunk_store CASCADE;

-- Drop document_store (academic/research paper store, superseded by vector_store)
DROP TABLE IF EXISTS multi_store_rag_working.document_store CASCADE;
