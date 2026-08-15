-- backend/app/db/migrations/004_image_content_type.sql
-- Add content classification columns to image_store so each image row
-- records what kind of content it contains and which content store also
-- holds the extracted data.
--
-- content_type: 'figure' | 'table' | 'text'
--   figure = chart, diagram, photo, or anything that is not primarily
--            structured data or readable text
--   table  = image is mainly a data table/grid with rows and columns;
--            OCR data will also be written to table_store
--   text   = image is mainly text paragraphs/lists/headings;
--            OCR data will also be written to vector_store
--
-- stored_in: 'image_store' | 'table_store' | 'vector_store'
--   image_store  = content lives only in image_store (figures)
--   table_store  = OCR/table content also stored in table_store
--   vector_store = OCR text also stored as a chunk in vector_store

SET search_path TO multi_store_rag_working, public;

ALTER TABLE multi_store_rag_working.image_store
    ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'figure',
    ADD COLUMN IF NOT EXISTS stored_in    TEXT NOT NULL DEFAULT 'image_store';

COMMENT ON COLUMN multi_store_rag_working.image_store.content_type IS
    'Gemma-classified image content: figure | table | text';

COMMENT ON COLUMN multi_store_rag_working.image_store.stored_in IS
    'Store that also holds this image''s extracted content: image_store | table_store | vector_store';
