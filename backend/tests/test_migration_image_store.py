"""Schema migration checks for image_store.

caption was DROPPED; structured_content + detected_store were ADDED; ocr_text is
retained (raw OCR). image_store is now a PURE repository (migrations 008/009):
embedding + from_image_store were DROPPED (semantic data lives in the destination
stores), and stored_in is nullable (NULL until a destination row is created).
"""
import pytest
from app.db.connection import get_db

# Required columns in the pure-repository schema (no embedding) + prefilter
# tracking columns (migration 010).
REQUIRED = {
    "id", "document_id", "image_index", "page_number", "bbox",
    "storage_path", "storage_bucket", "mime_type", "width", "height",
    "ocr_text", "vlm_ocr_text", "structured_content", "detected_store",
    "image_metadata", "content_type", "stored_in", "created_at",
    "processing_status", "skip_reason", "filter_stage", "image_type",
}

# caption (schema rework), embedding + from_image_store (migration 008) removed.
REMOVED = {"caption", "embedding", "from_image_store"}


@pytest.mark.slow
def test_image_store_columns_exist():
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'multi_store_rag_working' AND table_name = 'image_store'"""
        )
        cols = {r[0] for r in cur.fetchall()}
    assert REQUIRED <= cols, f"missing: {REQUIRED - cols}"


@pytest.mark.slow
def test_image_store_caption_column_dropped():
    """caption column must no longer exist in the image_store table."""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'multi_store_rag_working' AND table_name = 'image_store'"""
        )
        cols = {r[0] for r in cur.fetchall()}
    for col in REMOVED:
        assert col not in cols, f"column '{col}' should have been dropped but still exists"


@pytest.mark.slow
def test_image_store_has_structured_content_and_detected_store():
    """structured_content and detected_store must be present."""
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'multi_store_rag_working' AND table_name = 'image_store'"""
        )
        cols = {r[0] for r in cur.fetchall()}
    assert "structured_content" in cols
    assert "detected_store" in cols
    assert "ocr_text" in cols


@pytest.mark.slow
def test_table_store_has_image_storage_path():
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'multi_store_rag_working'
                 AND table_name = 'table_store' AND column_name = 'image_storage_path'"""
        )
        assert cur.fetchone() is not None
