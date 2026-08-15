-- ============================================================
-- Multi-Store RAG Chatbot — Initial Schema
-- Run this in Supabase SQL Editor BEFORE starting the application.
-- Schema: multi_store_rag_working
-- ============================================================

-- Enable required extensions (run as superuser — Supabase does this automatically)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Create the working schema
CREATE SCHEMA IF NOT EXISTS multi_store_rag_working;

SET search_path TO multi_store_rag_working, public, extensions;

-- ============================================================
-- TABLE 1: document_registry
-- Central registry for every uploaded document.
-- Every other table references this via FK.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.document_registry (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename            TEXT NOT NULL,
    original_filename   TEXT NOT NULL,
    file_size_bytes     BIGINT NOT NULL,
    mime_type           TEXT NOT NULL DEFAULT 'application/pdf',
    storage_path        TEXT,
    storage_bucket      TEXT DEFAULT 'rag-documents',

    -- Gemma 4 Router output
    document_type       TEXT,
    -- 'policy' | 'financial' | 'legal' | 'entity' | 'research'
    document_subtype    TEXT,
    router_confidence   FLOAT,
    router_reasoning    TEXT,

    -- Pipeline state machine
    -- uploaded → parsing → routing → chunking → embedding → storing → completed | failed
    status              TEXT NOT NULL DEFAULT 'uploaded',

    -- Per-stage timestamps
    uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    parsed_at           TIMESTAMPTZ,
    chunked_at          TIMESTAMPTZ,
    embedded_at         TIMESTAMPTZ,
    stored_at           TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,

    -- Error tracking
    error_stage         TEXT,
    error_message       TEXT,
    retry_count         INT NOT NULL DEFAULT 0,

    -- Docling parse metadata
    page_count          INT,
    word_count          INT,
    has_tables          BOOLEAN DEFAULT FALSE,
    has_images          BOOLEAN DEFAULT FALSE,
    language_detected   TEXT DEFAULT 'en',

    -- Metadata extracted by Gemma during routing
    doc_title           TEXT,
    doc_author          TEXT,
    doc_date            DATE,
    doc_summary         TEXT,
    doc_metadata        JSONB DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_doc_registry_status   ON multi_store_rag_working.document_registry(status);
CREATE INDEX IF NOT EXISTS idx_doc_registry_doc_type ON multi_store_rag_working.document_registry(document_type);
CREATE INDEX IF NOT EXISTS idx_doc_registry_created  ON multi_store_rag_working.document_registry(created_at DESC);

-- ============================================================
-- TABLE 2: vector_store
-- Semantic chunks for Policy/Operational documents.
-- Router classification: 'policy'
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.vector_store (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id      UUID NOT NULL
                     REFERENCES multi_store_rag_working.document_registry(id)
                     ON DELETE CASCADE,

    chunk_index      INT NOT NULL,
    chunk_text       TEXT NOT NULL,
    chunk_word_count INT,
    chunk_char_count INT,

    -- Position from Docling
    page_number      INT,
    page_numbers     INT[],
    bbox             JSONB,          -- {"x1":..,"y1":..,"x2":..,"y2":..}
    section_title    TEXT,
    section_level    INT,            -- H1=1, H2=2, etc.

    semantic_type    TEXT,           -- 'paragraph' | 'list' | 'header' | 'caption'
    keywords         TEXT[],

    -- BGE bge-large-en-v1.5 produces 1024-dim vectors
    embedding        vector(1024),

    chunk_metadata   JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index — no training step, fast build, best for <1M rows
CREATE INDEX IF NOT EXISTS idx_vector_store_embedding
    ON multi_store_rag_working.vector_store
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_vector_store_doc_id
    ON multi_store_rag_working.vector_store(document_id);

CREATE INDEX IF NOT EXISTS idx_vector_store_keywords
    ON multi_store_rag_working.vector_store USING GIN(keywords);

-- ============================================================
-- TABLE 3: table_store
-- Structured tables for Financial/Quantitative documents.
-- Router classification: 'financial'
-- Tables are NOT chunked — each Docling table = one row.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.table_store (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id      UUID NOT NULL
                     REFERENCES multi_store_rag_working.document_registry(id)
                     ON DELETE CASCADE,

    table_index      INT NOT NULL,
    table_title      TEXT,
    page_number      INT,
    bbox             JSONB,

    -- Table content in multiple representations
    raw_text         TEXT,
    markdown_text    TEXT,
    json_data        JSONB,          -- {"headers": [...], "rows": [[...]]}
    csv_data         TEXT,

    -- Dimensions
    row_count        INT,
    col_count        INT,

    -- Financial flags
    has_numeric_data BOOLEAN DEFAULT FALSE,
    has_currency     BOOLEAN DEFAULT FALSE,
    has_percentages  BOOLEAN DEFAULT FALSE,
    detected_units   TEXT[],         -- ['USD', 'INR', '%', 'tonnes', ...]

    -- Surrounding context from the document
    context_before   TEXT,
    context_after    TEXT,

    -- Gemma-generated summary of this table (used for embedding)
    table_summary    TEXT,
    embedding        vector(1024),

    -- Financial-specific metadata
    fiscal_year      TEXT,
    reporting_period TEXT,
    currency         TEXT,
    table_category   TEXT,
    -- 'balance_sheet' | 'income_statement' | 'cash_flow' | 'kpi' | 'comparison' | 'other'

    table_metadata   JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_table_store_embedding
    ON multi_store_rag_working.table_store
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_table_store_doc_id
    ON multi_store_rag_working.table_store(document_id);

CREATE INDEX IF NOT EXISTS idx_table_store_category
    ON multi_store_rag_working.table_store(table_category);

CREATE INDEX IF NOT EXISTS idx_table_store_json
    ON multi_store_rag_working.table_store USING GIN(json_data);

-- ============================================================
-- TABLE 4: clause_store
-- Legal clauses for Legal/Compliance documents.
-- Router classification: 'legal'
-- Chunking strategy: split by clause boundaries, not semantic similarity.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.clause_store (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id         UUID NOT NULL
                        REFERENCES multi_store_rag_working.document_registry(id)
                        ON DELETE CASCADE,

    clause_index        INT NOT NULL,
    clause_number       TEXT,         -- e.g. "12.3.1" or "Article III"
    clause_title        TEXT,
    clause_text         TEXT NOT NULL,
    clause_word_count   INT,

    clause_type         TEXT NOT NULL DEFAULT 'general',
    -- 'obligation' | 'prohibition' | 'right' | 'definition' | 'liability' |
    -- 'indemnification' | 'termination' | 'confidentiality' | 'dispute_resolution' |
    -- 'force_majeure' | 'warranty' | 'penalty' | 'governing_law' | 'general'

    clause_subtype      TEXT,
    risk_level          TEXT,         -- 'high' | 'medium' | 'low'
    risk_rationale      TEXT,

    -- Parties (extracted by Gemma)
    obligor             TEXT,
    obligee             TEXT,
    parties_mentioned   TEXT[],

    -- Key terms
    key_dates           JSONB,        -- {"effective_date": "...", "expiry": "..."}
    monetary_values     JSONB,        -- {"amount": 500000, "currency": "USD"}
    conditions          TEXT[],

    -- Position in document
    page_number         INT,
    page_numbers        INT[],
    section_path        TEXT[],       -- ['Part I', 'Section 3', 'Clause 3.1']

    embedding           vector(1024),

    -- Cross-references between clauses
    references_clauses  TEXT[],
    referenced_by       TEXT[],

    clause_metadata     JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clause_store_embedding
    ON multi_store_rag_working.clause_store
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_clause_store_doc_id
    ON multi_store_rag_working.clause_store(document_id);

CREATE INDEX IF NOT EXISTS idx_clause_store_type
    ON multi_store_rag_working.clause_store(clause_type);

CREATE INDEX IF NOT EXISTS idx_clause_store_risk
    ON multi_store_rag_working.clause_store(risk_level);

CREATE INDEX IF NOT EXISTS idx_clause_store_parties
    ON multi_store_rag_working.clause_store USING GIN(parties_mentioned);

-- ============================================================
-- TABLE 5: document_store
-- Chunks with citations for Research/Scientific documents.
-- Router classification: 'research'
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.document_store (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id         UUID NOT NULL
                        REFERENCES multi_store_rag_working.document_registry(id)
                        ON DELETE CASCADE,

    chunk_index         INT NOT NULL,
    chunk_text          TEXT NOT NULL,
    chunk_type          TEXT DEFAULT 'body',
    -- 'abstract' | 'introduction' | 'methodology' | 'results' |
    -- 'discussion' | 'conclusion' | 'body' | 'figure_caption' | 'reference'

    -- Citation tracking
    citation_key        TEXT,
    source_title        TEXT,
    source_authors      TEXT[],
    source_year         INT,
    source_doi          TEXT,
    source_url          TEXT,
    source_journal      TEXT,
    source_confidence   FLOAT,        -- Confidence in citation extraction accuracy

    -- Position
    page_number         INT,
    section_title       TEXT,
    section_type        TEXT,

    -- Research flags
    contains_hypothesis BOOLEAN DEFAULT FALSE,
    contains_finding    BOOLEAN DEFAULT FALSE,
    contains_method     BOOLEAN DEFAULT FALSE,
    entities_mentioned  TEXT[],       -- Named entities (chemicals, genes, companies, etc.)

    embedding           vector(1024),

    chunk_metadata      JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_doc_store_embedding
    ON multi_store_rag_working.document_store
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_doc_store_doc_id
    ON multi_store_rag_working.document_store(document_id);

CREATE INDEX IF NOT EXISTS idx_doc_store_chunk_type
    ON multi_store_rag_working.document_store(chunk_type);

CREATE INDEX IF NOT EXISTS idx_doc_store_doi
    ON multi_store_rag_working.document_store(source_doi);

-- ============================================================
-- TABLE 6: ingestion_jobs
-- Tracks Celery background task state for each pipeline run.
-- The frontend polls GET /api/v1/ingest/status/{job_id} which reads this.
-- ============================================================
CREATE TABLE IF NOT EXISTS multi_store_rag_working.ingestion_jobs (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id      UUID NOT NULL
                     REFERENCES multi_store_rag_working.document_registry(id)
                     ON DELETE CASCADE,

    celery_task_id   TEXT,

    -- State machine: queued → parsing → routing → chunking → embedding → storing → done | error
    current_stage    TEXT NOT NULL DEFAULT 'queued',
    stage_progress   INT DEFAULT 0,      -- 0-100 within current stage

    total_chunks     INT,
    processed_chunks INT DEFAULT 0,

    -- Timing
    queued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    duration_seconds FLOAT,

    -- Per-stage durations (seconds): {"parsing": 12.3, "routing": 2.1, ...}
    stage_timings    JSONB DEFAULT '{}',

    -- Error info
    error_message    TEXT,
    error_traceback  TEXT,
    is_retryable     BOOLEAN DEFAULT TRUE,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_document_id
    ON multi_store_rag_working.ingestion_jobs(document_id);

CREATE INDEX IF NOT EXISTS idx_jobs_celery
    ON multi_store_rag_working.ingestion_jobs(celery_task_id);

CREATE INDEX IF NOT EXISTS idx_jobs_stage
    ON multi_store_rag_working.ingestion_jobs(current_stage);

-- ============================================================
-- Auto-update updated_at trigger
-- ============================================================
CREATE OR REPLACE FUNCTION multi_store_rag_working.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_doc_registry_updated_at ON multi_store_rag_working.document_registry;
CREATE TRIGGER trg_doc_registry_updated_at
    BEFORE UPDATE ON multi_store_rag_working.document_registry
    FOR EACH ROW EXECUTE FUNCTION multi_store_rag_working.set_updated_at();

DROP TRIGGER IF EXISTS trg_ingestion_jobs_updated_at ON multi_store_rag_working.ingestion_jobs;
CREATE TRIGGER trg_ingestion_jobs_updated_at
    BEFORE UPDATE ON multi_store_rag_working.ingestion_jobs
    FOR EACH ROW EXECUTE FUNCTION multi_store_rag_working.set_updated_at();

-- ============================================================
-- Helper view: document_overview
-- Used by GET /api/v1/documents endpoint.
-- ============================================================
CREATE OR REPLACE VIEW multi_store_rag_working.document_overview AS
SELECT
    d.id,
    d.original_filename,
    d.document_type,
    d.document_subtype,
    d.status,
    d.page_count,
    d.word_count,
    d.router_confidence,
    d.doc_title,
    d.doc_summary,
    COALESCE(vs.n, 0)  AS vector_chunks,
    COALESCE(ts.n, 0)  AS table_count,
    COALESCE(cs.n, 0)  AS clause_count,
    COALESCE(ds.n, 0)  AS research_chunks,
    d.completed_at,
    d.created_at,
    d.error_message
FROM multi_store_rag_working.document_registry d
LEFT JOIN (
    SELECT document_id, COUNT(*) AS n
    FROM multi_store_rag_working.vector_store
    GROUP BY document_id
) vs ON vs.document_id = d.id
LEFT JOIN (
    SELECT document_id, COUNT(*) AS n
    FROM multi_store_rag_working.table_store
    GROUP BY document_id
) ts ON ts.document_id = d.id
LEFT JOIN (
    SELECT document_id, COUNT(*) AS n
    FROM multi_store_rag_working.clause_store
    GROUP BY document_id
) cs ON cs.document_id = d.id
LEFT JOIN (
    SELECT document_id, COUNT(*) AS n
    FROM multi_store_rag_working.document_store
    GROUP BY document_id
) ds ON ds.document_id = d.id;

-- ============================================================
-- Verification queries — run after schema is applied
-- ============================================================
-- SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'multi_store_rag_working'
--   ORDER BY table_name;
-- Expected: clause_store, document_registry, document_store,
--           ingestion_jobs, table_store, vector_store
