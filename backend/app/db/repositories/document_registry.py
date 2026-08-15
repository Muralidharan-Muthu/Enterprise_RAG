import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def update_status(document_id: str, status: str, **kwargs) -> None:
    """Update document_registry status and any optional stage timestamp."""
    allowed_ts_fields = {"parsed_at", "chunked_at", "embedded_at", "stored_at", "completed_at"}
    sets = ["status = %s", "updated_at = NOW()"]
    params: list = [status]

    for field in allowed_ts_fields:
        if field in kwargs and kwargs[field] is True:
            sets.append(f"{field} = NOW()")

    for field in ("error_stage", "error_message", "page_count", "word_count",
                  "has_tables", "has_images", "document_type", "document_subtype",
                  "router_confidence", "router_reasoning", "doc_title", "doc_author",
                  "doc_date", "doc_summary", "doc_metadata", "storage_path"):
        if field in kwargs:
            sets.append(f"{field} = %s")
            params.append(kwargs[field])

    params.append(document_id)
    sql = f"UPDATE multi_store_rag_working.document_registry SET {', '.join(sets)} WHERE id = %s"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
