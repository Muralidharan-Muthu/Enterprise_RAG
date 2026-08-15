"""parse_staging repository — CRUD for the parse_staging pointer table.

Each row points to the serialised ParsedDocument blob in Supabase Storage,
carrying parse counts and a status field used by the two-task ingestion chain.

Style mirrors ingestion_jobs.py: raw SQL, get_db context manager, explicit
kwargs — no ORM.
"""
import logging

from app.db.connection import get_db

logger = logging.getLogger(__name__)

_TABLE = "multi_store_rag_working.parse_staging"


def upsert_staging(
    document_id: str,
    storage_bucket: str,
    blob_path: str,
    status: str = "staged",
    page_count: int | None = None,
    block_count: int | None = None,
    table_count: int | None = None,
    image_count: int | None = None,
    bytes_size: int | None = None,
) -> None:
    """Insert or update the parse_staging row for *document_id*.

    Uses ON CONFLICT … DO UPDATE so re-ingestion of the same document (which
    yields deterministic blob paths) is idempotent.
    """
    sql = f"""
        INSERT INTO {_TABLE}
            (document_id, storage_bucket, blob_path, status,
             page_count, block_count, table_count, image_count, bytes_size,
             updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (document_id) DO UPDATE
            SET storage_bucket = EXCLUDED.storage_bucket,
                blob_path      = EXCLUDED.blob_path,
                status         = EXCLUDED.status,
                page_count     = EXCLUDED.page_count,
                block_count    = EXCLUDED.block_count,
                table_count    = EXCLUDED.table_count,
                image_count    = EXCLUDED.image_count,
                bytes_size     = EXCLUDED.bytes_size,
                updated_at     = NOW()
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    document_id,
                    storage_bucket,
                    blob_path,
                    status,
                    page_count,
                    block_count,
                    table_count,
                    image_count,
                    bytes_size,
                ),
            )
    logger.debug("[%s] parse_staging upserted (status=%s)", document_id, status)


def get_staging(document_id: str) -> dict | None:
    """Return the parse_staging row as a plain dict, or None if not found."""
    sql = f"""
        SELECT id, document_id, storage_bucket, blob_path, status,
               page_count, block_count, table_count, image_count, bytes_size,
               created_at, updated_at
        FROM {_TABLE}
        WHERE document_id = %s
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (document_id,))
            row = cur.fetchone()

    if row is None:
        return None

    keys = [
        "id", "document_id", "storage_bucket", "blob_path", "status",
        "page_count", "block_count", "table_count", "image_count", "bytes_size",
        "created_at", "updated_at",
    ]
    return dict(zip(keys, row))


def set_status(document_id: str, status: str) -> None:
    """Update only the status column for a parse_staging row."""
    sql = f"UPDATE {_TABLE} SET status = %s, updated_at = NOW() WHERE document_id = %s"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, document_id))
    logger.debug("[%s] parse_staging status → %s", document_id, status)


def delete_staging(document_id: str) -> None:
    """Delete the parse_staging row for *document_id* (blob cleanup is done separately)."""
    sql = f"DELETE FROM {_TABLE} WHERE document_id = %s"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (document_id,))
    logger.debug("[%s] parse_staging row deleted", document_id)
