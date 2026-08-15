-- Migration 009: parse_staging table for Feature 1.6
-- Stores a pointer to the serialised ParsedDocument blob in Supabase Storage
-- and tracks parse/embed status so the embed task can resume without re-parsing.
-- Apply manually via Supabase SQL Editor.

SET search_path TO multi_store_rag_working, public, extensions;

CREATE TABLE IF NOT EXISTS multi_store_rag_working.parse_staging (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- One row per document; ON DELETE CASCADE keeps the staging table tidy.
    document_id   UUID        NOT NULL UNIQUE
                              REFERENCES multi_store_rag_working.document_registry(id)
                              ON DELETE CASCADE,
    storage_bucket TEXT       NOT NULL,
    blob_path      TEXT       NOT NULL,
    -- 'staged' → written; 'embed_queued' → handed to chunk_embed_store_task;
    -- 'consumed' → embed task finished (blob can be deleted on retention=0)
    status         TEXT       NOT NULL DEFAULT 'staged',
    -- Counts written at save time — handy for monitoring without fetching the blob.
    page_count     INT,
    block_count    INT,
    table_count    INT,
    image_count    INT,
    bytes_size     BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS parse_staging_document_id_idx
    ON multi_store_rag_working.parse_staging (document_id);

-- Trigger to auto-update updated_at on every write (optional but handy for
-- debugging; drop safely if the DB user lacks CREATE TRIGGER permission).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'parse_staging_updated_at'
          AND tgrelid = 'multi_store_rag_working.parse_staging'::regclass
    ) THEN
        EXECUTE $trig$
            CREATE OR REPLACE FUNCTION multi_store_rag_working.set_updated_at()
            RETURNS TRIGGER LANGUAGE plpgsql AS '
            BEGIN NEW.updated_at = NOW(); RETURN NEW; END;';
        $trig$;

        CREATE TRIGGER parse_staging_updated_at
            BEFORE UPDATE ON multi_store_rag_working.parse_staging
            FOR EACH ROW EXECUTE PROCEDURE multi_store_rag_working.set_updated_at();
    END IF;
END$$;
