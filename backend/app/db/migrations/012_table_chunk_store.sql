-- ============================================================
-- Multi-Store RAG Chatbot — Migration 008: Table Chunk Store
-- Feature 1.5: Scalable table vectorization (parent/child row-windows)
--
-- Run this in Supabase SQL Editor AFTER 001_initial_schema.sql.
-- Schema: multi_store_rag_working
-- ============================================================

SET search_path TO multi_store_rag_working, public, extensions;

-- ============================================================
-- TABLE: table_chunk_store
-- Child row-windows of parent table_store rows.
-- Each row = one token-bounded window of table rows from a parent table.
-- Parent table_store row (unchanged) holds the summary embedding.
-- Children hold per-window row embeddings for fine-grained semantic search.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.table_chunk_store (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id     UUID NOT NULL
                    REFERENCES multi_store_rag_working.document_registry(id)
                    ON DELETE CASCADE,
    table_id        UUID NOT NULL
                    REFERENCES multi_store_rag_working.table_store(id)
                    ON DELETE CASCADE,

    -- Position in the parent table
    table_index     INT,                 -- matches table_store.table_index for correlation
    chunk_index     INT,                 -- 0-based window index within this table
    row_start       INT,                 -- first data row index (0-based) in this window
    row_end         INT,                 -- last data row index (inclusive, 0-based)

    -- Serialized content: "Col1: val1; Col2: val2\nCol1: val2; ..." with header repeated
    serialized_text TEXT NOT NULL,

    -- Source location
    page_number     INT,

    -- BGE bge-large-en-v1.5 produces 1024-dim vectors
    embedding       vector(1024),

    -- Extra info: oversized flag from parent, coarsening note if windows were capped
    chunk_metadata  JSONB DEFAULT '{}',

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- HNSW index for ANN search on child embeddings
CREATE INDEX IF NOT EXISTS idx_table_chunk_store_embedding
    ON multi_store_rag_working.table_chunk_store
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- Btree index for FK lookups / JOIN to parent
CREATE INDEX IF NOT EXISTS idx_table_chunk_store_document_id
    ON multi_store_rag_working.table_chunk_store(document_id);

CREATE INDEX IF NOT EXISTS idx_table_chunk_store_table_id
    ON multi_store_rag_working.table_chunk_store(table_id);
