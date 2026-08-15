"""Backfill table_row_store / table_cell_store for existing tables.

Migration 019 added table_row_store/table_cell_store — the indexed EAV
pushdown tables the enterprise structured query engine
(table_sql_compiler.py) reads for exhaustive filter/aggregation/ranking/
GROUP BY queries. New ingestions populate them automatically (see
ingestion_orchestrator.py's Stage 5a-cell hook), but tables ingested BEFORE
this change have json_data in table_store with no corresponding rows/cells
here yet — those tables fall back to the slower tier-2 Python/JSONB
structured-query engine until backfilled.

This rebuilds table_row_store/table_cell_store directly from each table's
already-stored table_store.json_data ({"headers": [...], "rows": [[...]]}) —
no re-parsing, no re-embedding, no LLM calls, just typed cell extraction
(table_schema_service, the same normalization used at query time).

Run from backend/ with the venv activated, AFTER migration 019 has been
applied to the target database:

    python -m scripts.backfill_table_cell_store            # all missing tables
    python -m scripts.backfill_table_cell_store <doc_id>   # one document only

Safe to re-run: a table that already has cells in table_cell_store is
skipped, so a second pass is a no-op for already-backfilled tables.
"""
import json
import logging
import sys

from app.db.connection import get_db
from app.db.repositories.table_cell_store import insert_table_cells, insert_table_rows
from app.services import table_schema_service

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_table_cell_store")


def _find_tables_without_cells(document_id: str | None) -> list[tuple]:
    """Return (table_uuid, document_id, table_index, json_data) for every
    table_store row that has 0 rows in table_cell_store."""
    sql = """
        SELECT ts.id::text, ts.document_id::text, ts.table_index, ts.json_data
        FROM multi_store_rag_working.table_store ts
        JOIN multi_store_rag_working.document_registry dr ON dr.id = ts.document_id
        WHERE dr.status = 'completed'
          AND ts.json_data IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM multi_store_rag_working.table_cell_store tcs
              WHERE tcs.table_id = ts.id
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


def _backfill_one(table_uuid: str, document_id: str, table_index: int, json_data) -> tuple[int, int]:
    """Populate table_row_store/table_cell_store for one table. Returns
    (rows_inserted, cells_inserted)."""
    data = json_data if isinstance(json_data, dict) else json.loads(json_data)
    headers = data.get("headers") or []
    rows = data.get("rows") or []
    if not headers or not rows:
        logger.info("  table_index=%s has no headers/rows in json_data — skipping", table_index)
        return 0, 0

    row_tuples = table_schema_service.build_row_store_rows(document_id, table_uuid, headers, rows)
    cell_tuples = table_schema_service.build_cell_store_rows(document_id, table_uuid, headers, rows)

    n_rows = insert_table_rows(row_tuples)
    n_cells = insert_table_cells(cell_tuples)
    return n_rows, n_cells


def main(document_id: str | None = None) -> None:
    targets = _find_tables_without_cells(document_id)
    if not targets:
        logger.info(
            "No tables missing cell-store data%s — nothing to backfill.",
            f" for document {document_id}" if document_id else "",
        )
        return

    logger.info("Found %d table(s) missing cell-store data.", len(targets))
    total_rows = 0
    total_cells = 0
    for table_uuid, doc_id, table_index, json_data in targets:
        n_rows, n_cells = _backfill_one(table_uuid, doc_id, table_index, json_data)
        total_rows += n_rows
        total_cells += n_cells
        logger.info(
            "  doc=%s table_index=%s -> inserted %d row(s) / %d cell(s)",
            doc_id, table_index, n_rows, n_cells,
        )
    logger.info(
        "Done. Inserted %d row(s) / %d cell(s) across %d table(s).",
        total_rows, total_cells, len(targets),
    )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
