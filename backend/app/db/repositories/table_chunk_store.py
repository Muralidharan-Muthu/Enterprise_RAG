"""table_chunk_store repository — bulk insert of table row-window children.

Mirrors image_store.py style: one public function that takes pre-built row tuples
and bulk-inserts via psycopg2.extras.execute_values with page_size=200.

Column order matches the INSERT statement:
    document_id, table_id, table_index, chunk_index,
    row_start, row_end, serialized_text, page_number,
    embedding (::vector), chunk_metadata (::jsonb),
    structured_content, structured_content_embedding (::vector)

structured_content / structured_content_embedding (migration 018) are populated
only for LARGE tables (row_count > TABLE_CHUNK_MAX_ROWS) — a per-window JSON slice
of the window's canonical rows and its embedding. Both are NULL for smaller tables.
"""
import psycopg2.extras

from app.db.connection import get_db

_INSERT_SQL = """
    INSERT INTO multi_store_rag_working.table_chunk_store
        (document_id, table_id, table_index, chunk_index,
         row_start, row_end, serialized_text, page_number,
         embedding, chunk_metadata,
         structured_content, structured_content_embedding)
    VALUES %s
"""

_TEMPLATE = (
    "(%s, %s, %s, %s,"
    " %s, %s, %s, %s,"
    " %s::vector, %s::jsonb,"
    " %s, %s::vector)"
)


def insert_table_chunks(rows: list[tuple]) -> int:
    """Bulk-insert table row-window children into table_chunk_store.

    Parameters
    ----------
    rows : list of tuples, each with columns in the order defined by _INSERT_SQL /
           _TEMPLATE above:
           (document_id, table_id, table_index, chunk_index,
            row_start, row_end, serialized_text, page_number,
            embedding_list_or_None, chunk_metadata_json_str,
            structured_content_or_None, structured_content_embedding_list_or_None)

    Returns the number of rows inserted.

    Robustness: the last two columns (structured_content,
    structured_content_embedding — migration 018) are optional. A caller (e.g.
    an older ingestion build loaded in a not-yet-restarted worker) that supplies
    only the first 10 values is tolerated — the row is padded to the full 12
    with NULLs rather than crashing execute_values with "tuple index out of
    range". Rows longer than the template are rejected loudly.
    """
    if not rows:
        return 0
    n_cols = _TEMPLATE.count("%s")
    normalized: list[tuple] = []
    for r in rows:
        r = tuple(r)
        if len(r) > n_cols:
            raise ValueError(
                f"insert_table_chunks: row has {len(r)} values but the template "
                f"expects at most {n_cols}"
            )
        if len(r) < n_cols:
            r = r + (None,) * (n_cols - len(r))
        normalized.append(r)
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(),
            _INSERT_SQL,
            normalized,
            template=_TEMPLATE,
            page_size=200,
        )
    return len(normalized)


def update_table_chunk_counts(counts: dict[str, int]) -> None:
    """Bulk-update table_store.chunk_count for the given {table_id: n} map.

    Sets the exact count passed in (not additive) — callers pass the number
    of children just inserted for that parent, matching the migration 020
    backfill semantics of one COUNT(*) per table_id.
    """
    if not counts:
        return
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(),
            """
            UPDATE multi_store_rag_working.table_store AS t
            SET chunk_count = data.n
            FROM (VALUES %s) AS data(id, n)
            WHERE t.id = data.id::uuid
            """,
            list(counts.items()),
            template="(%s::uuid, %s::int)",
            page_size=200,
        )
