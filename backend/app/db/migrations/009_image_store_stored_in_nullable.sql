-- 009_image_store_stored_in_nullable.sql
-- Makes image_store.stored_in a true status column: NULL until the content has
-- been successfully written to a destination semantic store, then set to that
-- store's name. Previously it defaulted to 'image_store' (NOT NULL), which
-- conflated "extracted but not routed" with an actual storage location.
--
--   Image extracted            -> stored_in = NULL
--   destination row created OK  -> stored_in = '<detected_store>'
--   figure (no destination)     -> stored_in stays NULL
--
-- Apply manually via the Supabase SQL Editor (no Alembic; see CLAUDE.md).
-- Idempotent enough to re-run (the UPDATE is a no-op once values are NULL).

SET search_path TO multi_store_rag_working;

ALTER TABLE image_store ALTER COLUMN stored_in DROP DEFAULT;
ALTER TABLE image_store ALTER COLUMN stored_in DROP NOT NULL;

-- Backfill: under the old default, 'image_store' meant "no semantic destination"
-- (figures, or rows whose cross-store write was skipped/failed). That is now
-- represented as NULL. Rows actually written to a destination keep their value
-- ('table_store' / 'vector_store' / 'clause_store' / 'document_store').
UPDATE image_store SET stored_in = NULL WHERE stored_in = 'image_store';

COMMENT ON COLUMN multi_store_rag_working.image_store.stored_in IS
    'Destination semantic store the content was successfully written to '
    '(table_store|vector_store|clause_store|document_store). NULL = extracted '
    'but not stored in any semantic store (e.g. a plain figure).';
