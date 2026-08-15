-- ============================================================
-- Multi-Store RAG Chatbot — Migration 019: Table Cell/Row Store
-- Enterprise table retrieval: indexed EAV-style predicate pushdown for
-- exhaustive filter/aggregation/ranking/GROUP BY queries over table_store
-- data, without loading full JSONB blobs into Python and without needing
-- ANN/reranking (which by design surfaces "most relevant few", not "all
-- rows matching a predicate").
--
-- table_store.json_data ({"headers": [...], "rows": [[...]]}) stores each
-- table as one positional-array JSONB blob — no per-column SQL index is
-- possible against it, and column sets vary per table so a fixed relational
-- schema can't represent arbitrary tabular data. table_row_store/
-- table_cell_store instead use the standard EAV (entity-attribute-value)
-- pattern: one row per (table, row, column) cell, with typed shadow columns
-- (value_text/value_numeric) that CAN be indexed regardless of the source
-- table's actual column names.
--
-- Run this in Supabase SQL Editor AFTER 001_initial_schema.sql and
-- 012_table_chunk_store.sql.
-- Schema: multi_store_rag_working
-- ============================================================

SET search_path TO multi_store_rag_working, public, extensions;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- TABLE: table_row_store
-- One row per (table_id, row_index) — the full row as a keyed JSONB object
-- ({"Company Name": "SRF Ltd", "Sector": "Chemicals", ...}), used to
-- hydrate matched rows after table_cell_store predicate evaluation
-- identifies which row_index values satisfy a query.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.table_row_store (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id     UUID NOT NULL
                    REFERENCES multi_store_rag_working.document_registry(id)
                    ON DELETE CASCADE,
    table_id        UUID NOT NULL
                    REFERENCES multi_store_rag_working.table_store(id)
                    ON DELETE CASCADE,

    row_index       INT NOT NULL,        -- 0-based position within the parent table's rows array
    row_data        JSONB NOT NULL,      -- {header: cell_value, ...} keyed object for this row

    created_at      TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (table_id, row_index)
);

CREATE INDEX IF NOT EXISTS idx_table_row_store_table
    ON multi_store_rag_working.table_row_store(table_id);

CREATE INDEX IF NOT EXISTS idx_table_row_store_document
    ON multi_store_rag_working.table_row_store(document_id);

-- ============================================================
-- TABLE: table_cell_store
-- One row per (table_id, row_index, column_name) cell. value_text/
-- value_numeric are typed, normalized shadow copies of the cell used for
-- indexed predicate pushdown (WHERE/AND/OR/BETWEEN/IN/LIKE/GROUP BY),
-- computed at ingestion by table_schema_service using the same numeric/
-- text normalization already used by table_query_engine
-- (_normalize_numeric_token / _normalize_label) so query-time and
-- ingestion-time normalization stay identical.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.table_cell_store (
    id              BIGSERIAL PRIMARY KEY,
    document_id     UUID NOT NULL
                    REFERENCES multi_store_rag_working.document_registry(id)
                    ON DELETE CASCADE,
    table_id        UUID NOT NULL
                    REFERENCES multi_store_rag_working.table_store(id)
                    ON DELETE CASCADE,

    row_index       INT NOT NULL,
    column_name     TEXT NOT NULL,       -- original header text, verbatim
    column_index    INT NOT NULL,        -- 0-based position within headers array

    value_text      TEXT,                -- normalized lowercase form (_normalize_label) — equality/IN/LIKE
    value_numeric   NUMERIC,             -- parsed numeric form (_normalize_numeric_token); NULL if non-numeric
    value_raw       TEXT,                -- original cell text, verbatim (display/debug)

    created_at      TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (table_id, row_index, column_name)
);

-- Numeric predicate pushdown (>, >=, <, <=, BETWEEN) + GROUP BY joins
CREATE INDEX IF NOT EXISTS idx_cell_store_numeric
    ON multi_store_rag_working.table_cell_store(table_id, column_name, value_numeric);

-- Exact-match / IN pushdown
CREATE INDEX IF NOT EXISTS idx_cell_store_text
    ON multi_store_rag_working.table_cell_store(table_id, column_name, value_text);

-- LIKE/contains pushdown
CREATE INDEX IF NOT EXISTS idx_cell_store_text_trgm
    ON multi_store_rag_working.table_cell_store
    USING gin (value_text gin_trgm_ops);

-- FK lookups
CREATE INDEX IF NOT EXISTS idx_cell_store_table
    ON multi_store_rag_working.table_cell_store(table_id);

CREATE INDEX IF NOT EXISTS idx_cell_store_document
    ON multi_store_rag_working.table_cell_store(document_id);
