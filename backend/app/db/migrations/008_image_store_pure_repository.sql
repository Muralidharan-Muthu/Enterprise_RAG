-- 008_image_store_pure_repository.sql
-- Makes image_store a PURE extraction repository: it stores extracted images and
-- their metadata (OCR, VLM structured_content, detected_store, stored_in) only.
-- Semantic embeddings now live exclusively in the destination stores
-- (vector_store / table_store / clause_store / document_store), which are the
-- only stores that perform vector search.
--
-- Apply manually via the Supabase SQL Editor (no Alembic; see CLAUDE.md).
-- Idempotent: IF EXISTS guards make re-running safe.
--
-- Removed from image_store:
--   embedding         — image_store is not a searchable semantic store; the
--                       embedding belongs in the destination store, generated
--                       from that store's content. Retrieval no longer queries
--                       image_store (see retriever_service).
--   from_image_store  — always TRUE on image_store (every row is an image), so it
--                       carried no information. Origin is tracked on the
--                       DESTINATION rows (table/vector/clause/document) via their
--                       from_image_store flag + *_metadata.source_image_id.

SET search_path TO multi_store_rag_working;

-- Drop the HNSW index on the embedding before dropping the column.
DROP INDEX IF EXISTS multi_store_rag_working.idx_image_store_embedding;

ALTER TABLE image_store DROP COLUMN IF EXISTS embedding;
ALTER TABLE image_store DROP COLUMN IF EXISTS from_image_store;

-- NOTE: from_image_store is intentionally KEPT on vector_store / table_store /
-- clause_store / document_store (added in 007) — there it is meaningful
-- (FALSE for normal extraction, TRUE for image-derived rows).
