"""table_row_store repository — bulk insert for unified enterprise table rows.

Stores full-row JSONB (row_data), typed numeric metrics (row_numeric), formatted
text (row_text), and dense semantic vectors (embedding) for SQL pushdown and row-level vector search.
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

_INSERT_ROWS_WITH_EMB_SQL = """
    INSERT INTO multi_store_rag_working.table_row_store
        (document_id, table_id, row_index, row_data, row_numeric, row_text, embedding)
    VALUES %s
    ON CONFLICT (table_id, row_index) DO UPDATE SET
        row_data = EXCLUDED.row_data,
        row_numeric = EXCLUDED.row_numeric,
        row_text = EXCLUDED.row_text,
        embedding = EXCLUDED.embedding
"""
_ROWS_WITH_EMB_TEMPLATE = "(%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::vector)"


def insert_table_rows(rows: list[tuple]) -> int:
    """Bulk-insert into unified table_row_store.

    rows: list of tuples with either 6 items (no embedding) or 7 items (with embedding):
      (document_id, table_id, row_index, row_data_json, row_numeric_json, row_text[, embedding_str]).
    """
    if not rows:
        return 0
    has_emb = len(rows[0]) >= 7 and rows[0][6] is not None
    sql = _INSERT_ROWS_WITH_EMB_SQL if has_emb else _INSERT_ROWS_SQL
    template = _ROWS_WITH_EMB_TEMPLATE if has_emb else _ROWS_TEMPLATE
    with get_db() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, sql, rows, template=template, page_size=500,
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

