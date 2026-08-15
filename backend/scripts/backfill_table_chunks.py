"""Backfill table_chunk_store children for existing large tables.

For every table_store row with row_count > TABLE_CHUNK_MAX_ROWS (default 25)
that currently has NO children in table_chunk_store, this rebuilds the
row-window children exactly as the ingestion pipeline would:

  - split the table's canonical rows (from table_store.json_data) into
    fixed-size windows of TABLE_CHUNK_MAX_ROWS rows each
    (e.g. 200 rows -> 8 windows of 25),
  - embed each window's serialized text  -> table_chunk_store.embedding,
  - build a per-window structured_content JSON slice + embed it
    -> table_chunk_store.structured_content / structured_content_embedding.

Use this to populate table_chunk_store for documents that were ingested
before the child-window feature was live (or before the worker was restarted
on the current code), without having to re-upload them.

Run from backend/ with the venv activated, AFTER migration 018 has been
applied to the target database:

    python -m scripts.backfill_table_chunks            # all missing big tables
    python -m scripts.backfill_table_chunks <doc_id>   # one document only

Safe to re-run: a table that already has children in table_chunk_store is
skipped, so a second pass is a no-op for already-backfilled tables.
"""
import json
import logging
import sys

from app.config import settings
from app.db.connection import get_db
from app.db.repositories.table_chunk_store import insert_table_chunks, update_table_chunk_counts
from app.models.document import ExtractedTable
from app.services.embedding_service import embed_passages
from app.services.table_chunker import build_window_structured_content, chunk_tables

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_table_chunks")


def _find_big_tables_without_children(document_id: str | None) -> list[tuple]:
    """Return (table_uuid, document_id, table_index, table_title, page_number,
    json_data) for every big table (row_count > cap) that has 0 children."""
    sql = """
        SELECT ts.id::text, ts.document_id::text, ts.table_index,
               ts.table_title, ts.page_number, ts.json_data
        FROM multi_store_rag_working.table_store ts
        WHERE ts.row_count > %s
          AND NOT EXISTS (
              SELECT 1 FROM multi_store_rag_working.table_chunk_store tcs
              WHERE tcs.table_id = ts.id
          )
    """
    params: list = [settings.TABLE_CHUNK_MAX_ROWS]
    if document_id:
        sql += " AND ts.document_id = %s"
        params.append(document_id)
    sql += " ORDER BY ts.document_id, ts.table_index"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _rebuild_children(table_uuid, document_id, table_index, title, page, json_data) -> int:
    """Rebuild + insert row-window children for one table. Returns rows inserted."""
    data = json_data if isinstance(json_data, dict) else json.loads(json_data)
    headers = data.get("headers") or []
    rows = data.get("rows") or []
    if not rows:
        logger.info("  table_index=%s has no rows in json_data — skipping", table_index)
        return 0

    table = ExtractedTable(
        table_index=table_index,
        page_number=page or 1,
        headers=headers,
        rows=rows,
        caption=title,
    )

    children, _ = chunk_tables(
        [table],
        max_tokens=settings.TABLE_CHUNK_MAX_TOKENS,
        max_rows=settings.TABLE_CHUNK_MAX_ROWS,
        overlap_rows=settings.TABLE_CHUNK_OVERLAP_ROWS,
        max_windows_per_table=settings.TABLE_MAX_WINDOWS_PER_TABLE,
    )
    if not children:
        return 0

    base_embs = embed_passages([c.serialized_text for c in children])
    # Big table (this script only selects row_count > cap) → every window gets a
    # structured_content JSON slice + its own embedding.
    sc_texts = [
        build_window_structured_content(table, c.row_start, c.row_end)
        for c in children
    ]
    sc_embs = embed_passages(sc_texts)

    child_rows = [
        (
            document_id, table_uuid, c.table_index, c.chunk_index,
            c.row_start, c.row_end, c.serialized_text, c.page_number,
            base_embs[i].tolist(), json.dumps(c.chunk_metadata),
            sc_texts[i], sc_embs[i].tolist(),
        )
        for i, c in enumerate(children)
    ]
    n = insert_table_chunks(child_rows)
    if n:
        update_table_chunk_counts({table_uuid: n})
    return n


def main(document_id: str | None = None) -> None:
    targets = _find_big_tables_without_children(document_id)
    if not targets:
        logger.info(
            "No big tables missing children%s — nothing to backfill.",
            f" for document {document_id}" if document_id else "",
        )
        return

    logger.info("Found %d big table(s) missing children.", len(targets))
    total = 0
    for table_uuid, doc_id, table_index, title, page, json_data in targets:
        n = _rebuild_children(table_uuid, doc_id, table_index, title, page, json_data)
        total += n
        logger.info(
            "  doc=%s table_index=%s -> inserted %d windows", doc_id, table_index, n
        )
    logger.info("Done. Inserted %d child window(s) across %d table(s).", total, len(targets))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
