"""Backfill unified table_row_store for existing tables (migration 023).

Rebuilds table_row_store directly from each table's already-stored
table_store.json_data ({"headers": [...], "rows": [[...]]}).
"""
import json
import logging
import sys

from app.db.connection import get_db
from app.db.repositories.table_cell_store import insert_table_rows
from app.services import table_schema_service

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_table_row_store")


def _find_tables_without_rows(document_id: str | None) -> list[tuple]:
    """Return (table_uuid, document_id, table_index, json_data) for every
    table_store row that has 0 rows in table_row_store."""
    sql = """
        SELECT ts.id::text, ts.document_id::text, ts.table_index, ts.json_data
        FROM multi_store_rag_working.table_store ts
        JOIN multi_store_rag_working.document_registry dr ON dr.id = ts.document_id
        WHERE dr.status = 'completed'
          AND ts.json_data IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM multi_store_rag_working.table_row_store trs
              WHERE trs.table_id = ts.id
          )
    """
    params: list = []
    if document_id:
        sql += " AND ts.document_id = %s"
        params.append(document_id)
    sql += " ORDER BY ts.document_id, ts.table_index"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _backfill_one(table_uuid: str, document_id: str, table_index: int, json_data) -> int:
    """Populate table_row_store for one table. Returns rows_inserted."""
    data = json_data if isinstance(json_data, dict) else json.loads(json_data)
    headers = data.get("headers") or []
    rows = data.get("rows") or []
    if not headers or not rows:
        logger.info("  table_index=%s has no headers/rows in json_data — skipping", table_index)
        return 0

    row_tuples = table_schema_service.build_row_store_rows(document_id, table_uuid, headers, rows)
    return insert_table_rows(row_tuples)


def main(document_id: str | None = None) -> None:
    targets = _find_tables_without_rows(document_id)
    if not targets:
        logger.info(
            "No tables missing row-store data%s — nothing to backfill.",
            f" for document {document_id}" if document_id else "",
        )
        return

    logger.info("Found %d table(s) missing row-store data.", len(targets))
    total_rows = 0
    for table_uuid, doc_id, table_index, json_data in targets:
        n_rows = _backfill_one(table_uuid, doc_id, table_index, json_data)
        total_rows += n_rows
        logger.info(
            "  doc=%s table_index=%s -> inserted %d row(s)",
            doc_id, table_index, n_rows,
        )

    logger.info("Backfill complete. Total rows inserted: %d", total_rows)


if __name__ == "__main__":
    doc_arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(doc_arg)
