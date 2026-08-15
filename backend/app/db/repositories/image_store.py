"""image_store repository — bulk insert of extracted images + metadata.

image_store is a PURE extraction repository: no embedding column (semantic
search happens only in the destination stores). See migration 008.
"""
import psycopg2.extras

from app.db.connection import get_db

_INSERT_SQL = """
    INSERT INTO multi_store_rag_working.image_store
        (document_id, image_index, page_number, bbox, storage_path, storage_bucket,
         mime_type, width, height, ocr_text, vlm_ocr_text, structured_content,
         image_metadata, content_type, detected_store, stored_in,
         processing_status, skip_reason, filter_stage, image_type, asset_role)
    VALUES %s
"""
_TEMPLATE = ("(%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,"
             " %s, %s, %s, %s, %s)")


def insert_images(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(), _INSERT_SQL, rows, template=_TEMPLATE, page_size=200,
        )
    return len(rows)
