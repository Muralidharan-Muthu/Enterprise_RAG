-- 006_image_vlm_ocr_text.sql
-- Add a SECOND OCR column to image_store.
--
--   ocr_text      — RAW OCR-engine output (unaltered). Added in 005. Audit/debug.
--   vlm_ocr_text  — the VLM's OWN verbatim transcription of the image text,
--                   produced after the VLM reads the image (using the raw OCR
--                   only to disambiguate hard-to-read characters). This is the
--                   corrected/complete text reading, distinct from both the raw
--                   OCR (ocr_text) and the rich structured extraction
--                   (structured_content).
--
-- Non-breaking: nullable, no default beyond NULL; existing rows keep working.

ALTER TABLE multi_store_rag_working.image_store
    ADD COLUMN IF NOT EXISTS vlm_ocr_text TEXT;
