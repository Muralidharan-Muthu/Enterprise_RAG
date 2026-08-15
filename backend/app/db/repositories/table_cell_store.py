"""table_cell_store / table_row_store repository (migration 019) — bulk
insert for the enterprise structured-query pushdown engine.

Mirrors table_chunk_store.py's style: pre-built row tuples, bulk-inserted
via psycopg2.extras.execute_values. Two tables, populated together for one
parent table_store row: table_row_store (full-row JSONB, for hydration) and
table_cell_store (per-cell typed values, for indexed predicate pushdown).
"""
import psycopg2.extras

from app.db.connection import get_db

_INSERT_ROWS_SQL = """
    INSERT INTO multi_store_rag_working.table_row_store
        (document_id, table_id, row_index, row_data)
    VALUES %s
    ON CONFLICT (table_id, row_index) DO UPDATE SET row_data = EXCLUDED.row_data
"""
_ROWS_TEMPLATE = "(%s, %s, %s, %s::jsonb)"

_INSERT_CELLS_SQL = """
    INSERT INTO multi_store_rag_working.table_cell_store
        (document_id, table_id, row_index, column_name, column_index,
         value_text, value_numeric, value_raw)
    VALUES %s
    ON CONFLICT (table_id, row_index, column_name) DO UPDATE SET
        value_text = EXCLUDED.value_text,
        value_numeric = EXCLUDED.value_numeric,
        value_raw = EXCLUDED.value_raw
"""
_CELLS_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, %s)"


def insert_table_rows(rows: list[tuple]) -> int:
    """Bulk-insert into table_row_store.

    rows: list of (document_id, table_id, row_index, row_data_json_str).
    ON CONFLICT UPDATE makes this safe to re-run (backfill re-runs, or a
    document re-ingested after edits) without needing a DELETE-first pass.
    """
    if not rows:
        return 0
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(), _INSERT_ROWS_SQL, rows, template=_ROWS_TEMPLATE, page_size=500,
        )
    return len(rows)


def insert_table_cells(cells: list[tuple]) -> int:
    """Bulk-insert into table_cell_store.

    cells: list of (document_id, table_id, row_index, column_name,
    column_index, value_text, value_numeric, value_raw).
    """
    if not cells:
        return 0
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(), _INSERT_CELLS_SQL, cells, template=_CELLS_TEMPLATE, page_size=500,
        )
    return len(cells)


def delete_table_cell_data(table_id: str) -> None:
    """Remove existing row/cell data for a table_id before re-populating —
    used when a table is re-ingested and its column set may have changed
    (ON CONFLICT UPDATE alone can't remove now-stale columns/rows)."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM multi_store_rag_working.table_cell_store WHERE table_id = %s",
                [table_id],
            )
            cur.execute(
                "DELETE FROM multi_store_rag_working.table_row_store WHERE table_id = %s",
                [table_id],
            )
