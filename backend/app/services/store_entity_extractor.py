"""
Store Entity Extractor — extends GraphRAG to table_store and image_store.

Problem: the existing graph_build_service.build_document_graph() only processes
text chunks from whichever single store a document's doc_type maps to
(vector_store / clause_store / document_store). table_store rows and
image_store rows are silently skipped, leaving rich entity signal on the floor.

This service adds store-aware record readers and a unified extraction driver
that feeds those records through the same Gemma NER + upsert_entity_graph()
path already used for text chunks.

Public API
----------
read_table_store_records(document_id)
    Read table_store rows for a document. Prefers markdown_text; falls back to
    raw_text. Skips rows with no usable text.

read_image_store_records(document_id)
    Read image_store rows for a document. Prefers structured_content (VLM
    output); falls back to ocr_text. Skips SKIPPED images and rows whose
    combined text is shorter than GRAPHRAG_IMAGE_MIN_TEXT_LEN chars.

read_text_store_records(document_id, store)
    Read vector_store / clause_store / document_store rows. Mirrors
    graph_build_service._load_chunk_records_from_postgres() for consistency
    but is callable from outside that module.

extract_stores_for_document(document_id, filename, doc_type, stores)
    Run Gemma NER + relationship extraction on records from the given stores and
    upsert the results into Neo4j. Parallel via ThreadPoolExecutor (respects
    GRAPHRAG_EXTRACT_CONCURRENCY). Returns a counts dict.

extract_and_upsert_all_stores(document_id)
    Full entry point: reads ALL four stores (auto-detects which text store the
    document uses), extracts, and upserts. Used by the retroactive
    reprocess_graph_task Celery task and the /graph/reprocess API endpoint.

Design notes
------------
* All upserts use graph_service.upsert_entity_graph() (MERGE-based), so
  running this on a document that already has graph data is idempotent — it
  adds/updates entities but never creates duplicates.
* table_store and image_store rows are treated as additive: this service does
  NOT call clear_document_graph() because the caller (run_graph_stage) already
  cleared it before processing text chunks.
* When called from the retroactive path (reprocess API / Celery task) the
  caller MUST call graph_service.clear_document_graph() first if a full rebuild
  is desired.
* Never raises — all exceptions are caught, logged, and returned in the
  counts dict under the "error" key.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config import settings

logger = logging.getLogger(__name__)

# ── Store → text-column mapping (mirrors graph_build_service._STORE_TEXT_COLUMN)
_TEXT_STORE_COLUMNS = {
    "vector_store":   "chunk_text",
    "clause_store":   "clause_text",
    "document_store": "chunk_text",
}


# ─────────────────────────────────────────────────────────────────────────────
# Record readers
# ─────────────────────────────────────────────────────────────────────────────

def read_table_store_records(document_id: str) -> list[dict]:
    """Return entity-extraction records from table_store.

    Each record: {pg_id, store, chunk_index, page_number, text}

    Text priority: markdown_text -> raw_text.
    Rows with no usable text are silently skipped.
    """
    from app.db.connection import get_db

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, table_index, page_number, markdown_text, raw_text
                    FROM multi_store_rag_working.table_store
                    WHERE document_id = %s
                    ORDER BY table_index
                    """,
                    (document_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("[%s] read_table_store_records DB query failed: %s", document_id, exc)
        return []

    records: list[dict] = []
    for r in rows:
        pg_id, table_index, page_number, markdown_text, raw_text = r
        text = ""
        if settings.GRAPHRAG_TABLE_PREFER_MARKDOWN and markdown_text:
            text = markdown_text.strip()
        if not text and raw_text:
            text = raw_text.strip()
        if not text:
            continue
        records.append({
            "pg_id": str(pg_id),
            "store": "table_store",
            "chunk_index": table_index,
            "page_number": page_number,
            "text": text,
        })

    logger.debug("[%s] table_store: %d/%d rows had usable text", document_id, len(records), len(rows))
    return records


def read_image_store_records(document_id: str) -> list[dict]:
    """Return entity-extraction records from image_store.

    Each record: {pg_id, store, chunk_index, page_number, text}

    Text priority: structured_content (VLM output) -> ocr_text.
    Rows with processing_status='SKIPPED' or combined text shorter than
    GRAPHRAG_IMAGE_MIN_TEXT_LEN are silently skipped.
    """
    from app.db.connection import get_db

    min_len = settings.GRAPHRAG_IMAGE_MIN_TEXT_LEN

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, image_index, page_number,
                           structured_content, ocr_text, processing_status
                    FROM multi_store_rag_working.image_store
                    WHERE document_id = %s
                    ORDER BY image_index
                    """,
                    (document_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("[%s] read_image_store_records DB query failed: %s", document_id, exc)
        return []

    records: list[dict] = []
    for r in rows:
        pg_id, image_index, page_number, structured_content, ocr_text, proc_status = r
        # Skip images the pre-filter decided were not worth processing
        if (proc_status or "").upper() == "SKIPPED":
            continue
        # Text priority: VLM structured content first, then raw OCR
        text = (structured_content or "").strip() or (ocr_text or "").strip()
        if len(text) < min_len:
            continue
        records.append({
            "pg_id": str(pg_id),
            "store": "image_store",
            "chunk_index": image_index,
            "page_number": page_number,
            "text": text,
        })

    logger.debug(
        "[%s] image_store: %d/%d rows had usable text (min_len=%d)",
        document_id, len(records), len(rows), min_len,
    )
    return records


def read_text_store_records(document_id: str, store: str) -> list[dict]:
    """Return entity-extraction records from vector_store, clause_store, or document_store.

    Mirrors graph_build_service._load_chunk_records_from_postgres() but is
    importable from outside that module.
    """
    from app.db.connection import get_db

    if store not in _TEXT_STORE_COLUMNS:
        logger.warning("read_text_store_records: unknown store '%s'", store)
        return []

    text_col = _TEXT_STORE_COLUMNS[store]
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, chunk_index, {text_col}, page_number
                    FROM multi_store_rag_working.{store}
                    WHERE document_id = %s
                    ORDER BY chunk_index
                    """,
                    (document_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("[%s] read_text_store_records('%s') failed: %s", document_id, store, exc)
        return []

    return [
        {
            "pg_id": str(r[0]),
            "store": store,
            "chunk_index": r[1],
            "page_number": r[3],
            "text": (r[2] or "").strip(),
        }
        for r in rows
        if (r[2] or "").strip()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Extraction + upsert
# ─────────────────────────────────────────────────────────────────────────────

def _extract_one(record: dict, max_entities: int) -> tuple[dict, dict]:
    """Worker: extract graph elements from a single record. Returns (record, elements)."""
    from app.services.graph_extraction_service import extract_graph_elements
    elements = extract_graph_elements(record["text"], max_entities=max_entities)
    return record, elements


def extract_stores_for_document(
    document_id: str,
    filename: str,
    doc_type: str,
    stores: list[str],
) -> dict:
    """Extract entities + relationships from the given stores and upsert to Neo4j.

    stores: list of store names to process, e.g. ["table_store", "image_store"].
    Each valid store name is read, run through Gemma NER in parallel, and
    upserted via graph_service.upsert_entity_graph().

    Returns:
        {
            "stores_processed": int,
            "records_total": int,
            "entities_total": int,
            "relationships_total": int,
            "failed": int,
        }
    """
    from app.services import graph_service
    from app.services.entity_service import canonicalize

    counts = {
        "stores_processed": 0,
        "records_total": 0,
        "entities_total": 0,
        "relationships_total": 0,
        "failed": 0,
    }

    if not graph_service.is_available():
        logger.info("[%s] extract_stores_for_document: Neo4j unavailable — skipping", document_id)
        return counts

    # Build the combined record list from all requested stores
    all_records: list[dict] = []
    for store in stores:
        if store == "table_store":
            if not settings.GRAPHRAG_TABLE_STORE_ENABLED:
                continue
            recs = read_table_store_records(document_id)
        elif store == "image_store":
            if not settings.GRAPHRAG_IMAGE_STORE_ENABLED:
                continue
            recs = read_image_store_records(document_id)
        elif store in _TEXT_STORE_COLUMNS:
            recs = read_text_store_records(document_id, store)
        else:
            logger.warning(
                "[%s] extract_stores_for_document: unknown store '%s' — skipped",
                document_id, store,
            )
            continue

        if recs:
            counts["stores_processed"] += 1
        all_records.extend(recs)

    if not all_records:
        logger.info(
            "[%s] extract_stores_for_document: no records to process from %s",
            document_id, stores,
        )
        return counts

    counts["records_total"] = len(all_records)
    max_entities = settings.GRAPHRAG_ENTITIES_PER_CHUNK
    concurrency = settings.GRAPHRAG_EXTRACT_CONCURRENCY

    logger.info(
        "[%s] extract_stores_for_document: %d records from %s (concurrency=%d)",
        document_id, len(all_records), stores, concurrency,
    )

    # Parallel Gemma extraction
    results: list[tuple[dict, dict]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_extract_one, rec, max_entities): rec
            for rec in all_records
        }
        for fut in as_completed(futures):
            try:
                rec, elements = fut.result()
                results.append((rec, elements))
            except Exception as exc:
                logger.warning("[%s] extraction future failed: %s", document_id, exc)
                counts["failed"] += 1

    # Upsert each extracted record to Neo4j
    for rec, elements in results:
        entities_raw = elements.get("entities", [])
        relationships_raw = elements.get("relationships", [])

        # Canonicalize
        canon_entities = []
        for e in entities_raw:
            name = (e.get("name") or "").strip()
            if not name:
                continue
            canon_entities.append({
                "name": name,
                "key": canonicalize(name),
                "type": e.get("type", "misc") or "misc",
                "description": e.get("description", "") or "",
                "confidence": e.get("confidence"),
            })

        canon_rels = []
        for r in relationships_raw:
            src_key = canonicalize(r.get("source", ""))
            tgt_key = canonicalize(r.get("target", ""))
            if src_key and tgt_key:
                canon_rels.append({
                    "source_key": src_key,
                    "target_key": tgt_key,
                    "type": r.get("type", "RELATES_TO"),
                    "description": r.get("description", "") or "",
                    "confidence": r.get("confidence"),
                })

        wrote_ok = graph_service.upsert_entity_graph(
            document_id=document_id,
            filename=filename,
            doc_type=doc_type,
            chunk_meta={
                "pg_id": rec["pg_id"],
                "store": rec["store"],
                "chunk_index": rec["chunk_index"],
                "page_number": rec.get("page_number"),
            },
            entities=canon_entities,
            relationships=canon_rels,
        )

        if wrote_ok:
            counts["entities_total"] += len(canon_entities)
            counts["relationships_total"] += len(canon_rels)
        else:
            counts["failed"] += 1

    logger.info(
        "[%s] extract_stores_for_document done: records=%d entities=%d rels=%d failed=%d",
        document_id,
        counts["records_total"],
        counts["entities_total"],
        counts["relationships_total"],
        counts["failed"],
    )
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Full retroactive entry point
# ─────────────────────────────────────────────────────────────────────────────

def _detect_text_store(doc_type: str) -> str:
    """Mirror graph_build_service._store_for_doc_type()."""
    if doc_type == "legal":
        return "clause_store"
    if doc_type == "research":
        return "document_store"
    return "vector_store"


def extract_and_upsert_all_stores(document_id: str) -> dict:
    """Retroactive full-store entity extraction for an already-ingested document.

    Reads from ALL four stores (auto-detects the text store from doc_type),
    extracts entities and relationships, and upserts to Neo4j.

    NOTE: This function does NOT call clear_document_graph(). Callers that want
    a clean rebuild MUST call graph_service.clear_document_graph(document_id)
    before calling this function.

    Returns the counts dict from extract_stores_for_document(), plus
    "error" key on fatal failure.
    """
    from app.db.connection import get_db
    from app.services import graph_service

    # Fetch document metadata
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT original_filename, document_type "
                    "FROM multi_store_rag_working.document_registry WHERE id = %s",
                    (document_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.error("[%s] extract_and_upsert_all_stores: DB lookup failed: %s", document_id, exc)
        return {"error": str(exc), "stores_processed": 0, "records_total": 0,
                "entities_total": 0, "relationships_total": 0, "failed": 0}

    if not row:
        return {"error": f"document {document_id} not found in document_registry",
                "stores_processed": 0, "records_total": 0,
                "entities_total": 0, "relationships_total": 0, "failed": 0}

    filename, doc_type = row[0] or "", row[1] or ""
    text_store = _detect_text_store(doc_type)

    # Ensure schema before first write
    graph_service.ensure_schema()
    graph_service.upsert_document(doc_id=document_id, filename=filename, doc_type=doc_type)

    stores_to_process = [text_store, "table_store", "image_store"]
    return extract_stores_for_document(
        document_id=document_id,
        filename=filename,
        doc_type=doc_type,
        stores=stores_to_process,
    )
