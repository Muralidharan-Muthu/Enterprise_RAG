-- 005_image_structured_content.sql
-- Image pipeline rework: OCR engine → VLM(image + raw OCR) → structured extraction.
--
-- image_store changes:
--   * DROP caption            — replaced by the VLM-generated structured_content.
--   * ADD  ocr_text           — RAW OCR-engine output (unaltered). Already present
--                               from migration 003; kept here as IF NOT EXISTS for
--                               environments that predate it. Now holds raw OCR, not
--                               VLM-extracted text.
--   * ADD  structured_content — primary knowledge column: the complete VLM
--                               retrieval-ready extraction. This is what gets
--                               embedded and is the main source for image retrieval.
--   * ADD  detected_store     — destination store the VLM selected, based on the
--                               extracted content (vector_store | table_store |
--                               clause_store | image_store).
--
-- content_type and stored_in are retained for backward compatibility:
--   content_type — legacy classification, derived from detected_store on write.
--   stored_in    — physical store the derived chunk was actually written to.

ALTER TABLE multi_store_rag_working.image_store
    DROP COLUMN IF EXISTS caption,
    ADD COLUMN IF NOT EXISTS ocr_text           TEXT,
    ADD COLUMN IF NOT EXISTS structured_content TEXT,
    ADD COLUMN IF NOT EXISTS detected_store     TEXT NOT NULL DEFAULT 'image_store';
