"""table_row_store repository — bulk insert for unified enterprise table rows.

Stores full-row JSONB (row_data), typed numeric metrics (row_numeric), and formatted
text (row_text) for fast single-pass SQL pushdown.
"""
import logging
import psycopg2.extras

from app.db.connection import get_db

logger = logging.getLogger(__name__)

_INSERT_ROWS_SQL = """
    INSERT INTO multi_store_rag_working.table_row_store
        (document_id, table_id, row_index, row_data, row_numeric, row_text)
    VALUES %s
    ON CONFLICT (table_id, row_index) DO UPDATE SET
        row_data = EXCLUDED.row_data,
        row_numeric = EXCLUDED.row_numeric,
        row_text = EXCLUDED.row_text
"""
_ROWS_TEMPLATE = "(%s, %s, %s, %s::jsonb, %s::jsonb, %s)"


def insert_table_rows(rows: list[tuple]) -> int:
    """Bulk-insert into unified table_row_store.

    rows: list of (document_id, table_id, row_index, row_data_json, row_numeric_json, row_text).
    """
    if not rows:
        return 0
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(), _INSERT_ROWS_SQL, rows, template=_ROWS_TEMPLATE, page_size=500,
        )
    return len(rows)


def insert_table_cells(cells: list[tuple]) -> int:
    """Deprecated / No-op stub following migration 023."""
    return len(cells)


def delete_table_cell_data(table_id: str) -> None:
    """Remove existing row data for a table_id before re-populating."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM multi_store_rag_working.table_row_store WHERE table_id = %s",
                [table_id],
            )
