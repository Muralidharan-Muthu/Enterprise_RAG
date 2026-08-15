"""Backfill script for migration 014 (table/image lineage).

Populates the additive columns introduced by
app/db/migrations/014_table_image_lineage.sql for rows that existed before
the migration was applied:

  - image_store.asset_role          ('table_crop' | 'figure')
  - table_store.extraction_method   ('image_vlm' | 'pdf_grid')
  - table_store.source_image_id     (backfilled from JSONB, then from the
                                      image_index = 20000 + table_index
                                      convention for Docling-extracted tables)

Run with (from backend/, with the venv activated), AFTER migration 014 has
been applied to the target database:

    python -m scripts.backfill_014_table_lineage

Safe to re-run: every UPDATE is guarded with a `... IS NULL` (or equivalent)
predicate, so rows already backfilled are left untouched and running this
script twice is a no-op on the second pass.
"""
import logging

from app.db.connection import get_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_014")

_TABLE_CROP_IMAGE_STORE_OFFSET = 20_000


def check_duplicate_table_index() -> list[tuple]:
    """Pre-check (read-only): report — do NOT fix — duplicate (document_id,
    table_index) rows in table_store.

    Migration 014 creates a UNIQUE INDEX on (document_id, table_index). If any
    duplicates exist, that CREATE UNIQUE INDEX will fail. Run this BEFORE
    applying migration 014 so the operator can resolve duplicates first.
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, table_index, COUNT(*) AS cnt
                FROM multi_store_rag_working.table_store
                GROUP BY document_id, table_index
                HAVING COUNT(*) > 1
                ORDER BY cnt DESC
                """
            )
            dupes = cur.fetchall()

    if dupes:
        logger.warning(
            "Found %d duplicate (document_id, table_index) pair(s) in table_store "
            "— migration 014's UNIQUE INDEX will FAIL until these are resolved:",
            len(dupes),
        )
        for document_id, table_index, cnt in dupes:
            logger.warning(
                "  document_id=%s table_index=%s count=%d", document_id, table_index, cnt
            )
    else:
        logger.info("No duplicate (document_id, table_index) pairs found in table_store.")

    return dupes


def backfill_image_store_asset_role() -> None:
    """image_store.asset_role: 'table_crop' for crop-offset rows, 'figure' otherwise."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE multi_store_rag_working.image_store
                    SET asset_role = 'table_crop'
                    WHERE image_index >= %s AND asset_role IS NULL
                    """,
                    (_TABLE_CROP_IMAGE_STORE_OFFSET,),
                )
                table_crop_count = cur.rowcount
                cur.execute(
                    """
                    UPDATE multi_store_rag_working.image_store
                    SET asset_role = 'figure'
                    WHERE image_index < %s AND asset_role IS NULL
                    """,
                    (_TABLE_CROP_IMAGE_STORE_OFFSET,),
                )
                figure_count = cur.rowcount
        logger.info(
            "image_store.asset_role backfilled: %d table_crop, %d figure",
            table_crop_count, figure_count,
        )
    except Exception:
        logger.exception("Step failed: backfill_image_store_asset_role")


def backfill_table_store_extraction_method() -> None:
    """table_store.extraction_method: 'image_vlm' when from_image_store, else 'pdf_grid'."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE multi_store_rag_working.table_store
                    SET extraction_method = 'image_vlm'
                    WHERE from_image_store = TRUE AND extraction_method IS NULL
                    """
                )
                image_vlm_count = cur.rowcount
                cur.execute(
                    """
                    UPDATE multi_store_rag_working.table_store
                    SET extraction_method = 'pdf_grid'
                    WHERE from_image_store = FALSE AND extraction_method IS NULL
                    """
                )
                pdf_grid_count = cur.rowcount
        logger.info(
            "table_store.extraction_method backfilled: %d image_vlm, %d pdf_grid",
            image_vlm_count, pdf_grid_count,
        )
    except Exception:
        logger.exception("Step failed: backfill_table_store_extraction_method")


def backfill_source_image_id_from_jsonb() -> None:
    """table_store.source_image_id (a): pull from the existing JSONB pointer."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE multi_store_rag_working.table_store
                    SET source_image_id = (table_metadata->>'source_image_id')::uuid
                    WHERE source_image_id IS NULL
                      AND table_metadata->>'source_image_id' IS NOT NULL
                    """
                )
                count = cur.rowcount
        logger.info("table_store.source_image_id backfilled from JSONB: %d row(s)", count)
    except Exception:
        logger.exception("Step failed: backfill_source_image_id_from_jsonb")


def backfill_source_image_id_from_convention() -> None:
    """table_store.source_image_id (b): for Docling-extracted tables
    (from_image_store = FALSE) still missing source_image_id, match by the
    legacy convention — image_store.image_index = 20000 + table_store.table_index
    within the same document."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE multi_store_rag_working.table_store AS ts
                    SET source_image_id = img.id
                    FROM multi_store_rag_working.image_store AS img
                    WHERE ts.source_image_id IS NULL
                      AND ts.from_image_store = FALSE
                      AND img.document_id = ts.document_id
                      AND img.image_index = %s + ts.table_index
                    """,
                    (_TABLE_CROP_IMAGE_STORE_OFFSET,),
                )
                count = cur.rowcount
        logger.info(
            "table_store.source_image_id backfilled from image_index convention: %d row(s)",
            count,
        )
    except Exception:
        logger.exception("Step failed: backfill_source_image_id_from_convention")


def main() -> None:
    logger.info("=== migration 014 backfill: pre-check ===")
    check_duplicate_table_index()

    logger.info("=== migration 014 backfill: running steps ===")
    backfill_image_store_asset_role()
    backfill_table_store_extraction_method()
    backfill_source_image_id_from_jsonb()
    backfill_source_image_id_from_convention()

    logger.info("=== migration 014 backfill: done ===")


if __name__ == "__main__":
    main()
