"""
Graph build service — Feature 1.3 GraphRAG.

build_document_graph():
    Per-chunk entity + relationship extraction → Neo4j upsert.
    Bounded by GRAPHRAG_MAX_CHUNKS_PER_DOC; beyond cap falls back to doc-level extraction.
    Uses a worker-side ThreadPoolExecutor for concurrent Groq calls.

assemble_chunk_records():
    Bridges Postgres stored_ids (UUIDs per store, insertion-ordered) to chunk
    text/metadata from ParsedDocument + chunk/clause lists. This is the critical
    pg_id → Chunk-node bridge.

run_graph_stage():
    UNIFIED stage-6 helper called from BOTH ingestion_orchestrator (monolith) and
    ingestion_tasks.chunk_embed_store_task. Placed here to avoid circular imports
    (graph_build_service has no dependency on ingestion_orchestrator).

rebuild_graph_for_document():
    Recovery path — rebuilds the graph for an ALREADY-INGESTED document straight
    from its stored Postgres chunks, with no re-parsing/re-embedding. Use when
    the graph stage silently failed to persist (e.g. a dropped Neo4j connection
    mid-ingestion) but the document's chunks already exist in vector_store/
    clause_store/document_store. Verifies against Neo4j afterward so the
    returned counts are never aspirational.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── assemble_chunk_records ────────────────────────────────────────────────────

def assemble_chunk_records(
    parsed_doc,
    chunks: list,
    legal_clauses,
    router_result,
    stored_ids: dict,
) -> list[dict]:
    """Build the list of chunk records that bridge Postgres UUIDs to chunk text.

    Each record:
        {store, chunk_index, pg_id, page_number, section_title, text}

    stored_ids is dict[str, list[str]] returned by store_chunks():
        "vector_store"   → UUIDs in insertion order (mirrors chunks list)
        "clause_store"   → UUIDs (mirrors legal_clauses list)
        "table_store"    → UUIDs (mirrors parsed_doc.tables)
    """
    records: list[dict] = []

    if legal_clauses:
        pg_ids = stored_ids.get("clause_store", [])
        for i, clause in enumerate(legal_clauses):
            if i >= len(pg_ids):
                break
            records.append({
                "store": "clause_store",
                "chunk_index": i,
                "pg_id": pg_ids[i],
                "page_number": getattr(clause, "page_number", None),
                "section_title": getattr(clause, "clause_type", None) or getattr(clause, "clause_title", None),
                "text": (getattr(clause, "clause_text", "") or "").strip(),
            })

    if chunks:
        pg_ids = stored_ids.get("vector_store", [])
        for i, chunk in enumerate(chunks):
            if i >= len(pg_ids):
                break
            records.append({
                "store": "vector_store",
                "chunk_index": i + len(records),
                "pg_id": pg_ids[i],
                "page_number": getattr(chunk, "page_number", None),
                "section_title": getattr(chunk, "section_title", None),
                "text": (getattr(chunk, "chunk_text", "") or "").strip(),
            })

    return records


# ── _extract_for_chunk (worker function for ThreadPoolExecutor) ───────────────

def _extract_for_chunk(record: dict, max_entities: int) -> tuple[dict, dict]:
    """Extract graph elements for a single chunk. Returns (record, graph_elements)."""
    from app.services.graph_extraction_service import extract_graph_elements
    elements = extract_graph_elements(record["text"], max_entities=max_entities)
    return record, elements


# ── build_document_graph ──────────────────────────────────────────────────────

def build_document_graph(
    document_id: str,
    doc_meta: dict,
    chunk_records: list[dict],
) -> dict:
    """Extract graph elements and upsert Chunk nodes for each chunk record.

    chunk_records: list of {store, chunk_index, pg_id, page_number, section_title, text}
    doc_meta: {filename, doc_type}

    Returns counts {entities_total, relationships_total, chunks_processed,
    chunks_failed_to_write} — entities/relationships/chunks_processed reflect
    CONFIRMED Neo4j writes only, not extraction attempts.
    Non-fatal: any exception logs and returns zeroed counts.
    """
    if not chunk_records:
        return {"entities_total": 0, "relationships_total": 0, "chunks_processed": 0, "chunks_failed_to_write": 0}

    try:
        from app.services import graph_service
        from app.services.entity_service import canonicalize

        max_chunks = settings.GRAPHRAG_MAX_CHUNKS_PER_DOC
        extract_per_chunk = settings.GRAPHRAG_EXTRACT_PER_CHUNK
        max_entities = settings.GRAPHRAG_ENTITIES_PER_CHUNK
        concurrency = settings.GRAPHRAG_EXTRACT_CONCURRENCY

        # Determine if we exceed the per-doc cap
        if len(chunk_records) > max_chunks:
            logger.info(
                "[%s] %d chunks > GRAPHRAG_MAX_CHUNKS_PER_DOC=%d — falling back to doc-level extraction",
                document_id, len(chunk_records), max_chunks,
            )
            extract_per_chunk = False

        entities_total = 0
        relationships_total = 0
        chunks_processed = 0
        chunks_failed_to_write = 0
        t0 = time.monotonic()

        if extract_per_chunk:
            # ── Per-chunk extraction (parallel) ───────────────────────────────
            results: list[tuple[dict, dict]] = []

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(_extract_for_chunk, rec, max_entities): rec
                    for rec in chunk_records
                }
                for fut in as_completed(futures):
                    try:
                        rec, elements = fut.result()
                        results.append((rec, elements))
                    except Exception as exc:
                        logger.warning("[%s] chunk extraction future failed: %s", document_id, exc)

            for rec, elements in results:
                entities = elements.get("entities", [])
                relationships = elements.get("relationships", [])

                # Canonicalize entity names
                canon_entities = []
                for e in entities:
                    name = e.get("name", "").strip()
                    if not name:
                        continue
                    canon_entities.append({
                        "name": name,
                        "key": canonicalize(name),
                        "type": e.get("type", "misc"),
                        "description": e.get("description", ""),
                        "confidence": e.get("confidence"),
                    })

                canon_rels = []
                for r in relationships:
                    src_key = canonicalize(r.get("source", ""))
                    tgt_key = canonicalize(r.get("target", ""))
                    if src_key and tgt_key:
                        canon_rels.append({
                            "source_key": src_key,
                            "target_key": tgt_key,
                            "type": r.get("type", "RELATES_TO"),
                            "description": r.get("description", ""),
                            "confidence": r.get("confidence"),
                        })

                wrote_ok = graph_service.upsert_entity_graph(
                    document_id=document_id,
                    filename=doc_meta.get("filename", ""),
                    doc_type=doc_meta.get("doc_type", ""),
                    chunk_meta={
                        "pg_id": rec["pg_id"],
                        "store": rec["store"],
                        "chunk_index": rec["chunk_index"],
                        "page_number": rec.get("page_number"),
                    },
                    entities=canon_entities,
                    relationships=canon_rels,
                )

                # Only count what was ACTUALLY persisted — previously these
                # counters came from extraction results regardless of whether
                # the Neo4j write succeeded, so a dropped connection mid-run
                # could report "105 entities written" while zero landed.
                if wrote_ok:
                    entities_total += len(canon_entities)
                    relationships_total += len(canon_rels)
                    chunks_processed += 1
                else:
                    chunks_failed_to_write += 1

        else:
            # ── Doc-level fallback: extract once from full text ────────────────
            from app.services.graph_extraction_service import extract_graph_elements

            full_text = " ".join(
                rec["text"] for rec in chunk_records if rec.get("text")
            )[:8000]

            elements = extract_graph_elements(full_text, max_entities=max_entities * 2)
            entities = elements.get("entities", [])
            relationships = elements.get("relationships", [])

            canon_entities = []
            for e in entities:
                name = e.get("name", "").strip()
                if not name:
                    continue
                canon_entities.append({
                    "name": name,
                    "key": canonicalize(name),
                    "type": e.get("type", "misc"),
                    "description": e.get("description", ""),
                    "confidence": e.get("confidence"),
                })

            canon_rels = []
            for r in relationships:
                src_key = canonicalize(r.get("source", ""))
                tgt_key = canonicalize(r.get("target", ""))
                if src_key and tgt_key:
                    canon_rels.append({
                        "source_key": src_key,
                        "target_key": tgt_key,
                        "type": r.get("type", "RELATES_TO"),
                        "description": r.get("description", ""),
                        "confidence": r.get("confidence"),
                    })

            # Upsert each chunk record (no per-chunk extraction — use doc-level
            # entities, but still record per-chunk pg_id traceability on MENTIONED_IN)
            any_write_succeeded = False
            for rec in chunk_records:
                wrote_ok = graph_service.upsert_entity_graph(
                    document_id=document_id,
                    filename=doc_meta.get("filename", ""),
                    doc_type=doc_meta.get("doc_type", ""),
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
                    chunks_processed += 1
                    any_write_succeeded = True
                else:
                    chunks_failed_to_write += 1

            # Doc-level entities were extracted once and reused for every chunk's
            # traceability edge — only report them as "written" if at least one
            # chunk's write actually landed.
            entities_total = len(canon_entities) if any_write_succeeded else 0
            relationships_total = len(canon_rels) if any_write_succeeded else 0

        elapsed = time.monotonic() - t0
        if chunks_failed_to_write:
            logger.error(
                "[%s] Graph build INCOMPLETE: %d/%d chunks failed to persist to Neo4j "
                "(entities/relationships below reflect confirmed writes only, not extraction attempts)",
                document_id, chunks_failed_to_write, chunks_failed_to_write + chunks_processed,
            )
        logger.info(
            "[%s] Graph build complete: %d chunks, %d entities, %d relationships in %.1fs",
            document_id, chunks_processed, entities_total, relationships_total, elapsed,
        )
        return {
            "entities_total": entities_total,
            "relationships_total": relationships_total,
            "chunks_processed": chunks_processed,
            "chunks_failed_to_write": chunks_failed_to_write,
        }

    except Exception as exc:
        logger.warning("[%s] build_document_graph failed (non-fatal): %s", document_id, exc)
        return {"entities_total": 0, "relationships_total": 0, "chunks_processed": 0, "chunks_failed_to_write": 0}


# ── run_graph_stage (UNIFIED stage-6 helper) ─────────────────────────────────

def run_graph_stage(
    document_id: str,
    parsed_doc,
    chunks: list,
    legal_clauses,
    router_result,
    stored_ids: dict,
    storage_path: str = "",
) -> None:
    """UNIFIED stage-6 graph helper.

    Called from BOTH:
    - ingestion_orchestrator.ingest_document (monolith, INGESTION_STAGED_ENABLED=False)
    - ingestion_tasks.chunk_embed_store_task (staged path, INGESTION_STAGED_ENABLED=True)

    When GRAPHRAG_ENABLED=True AND graph is available:
      1. ensure_schema() (idempotent)
      2. clear_document_graph() (re-ingest safety)
      3. upsert_document() (Document traceability anchor)
      4. assemble_chunk_records() (pg_id bridge)
      5. build_document_graph() → entities + typed relationships + MENTIONED_IN
         traceability edges from text chunks (NO Chunk nodes)
      6d. extract_stores_for_document(["table_store"]) — additive entity
          extraction from table_store rows (markdown/raw text).
          Respects GRAPHRAG_TABLE_STORE_ENABLED; no-op when disabled.
      6e. extract_stores_for_document(["image_store"]) — additive entity
          extraction from image_store rows (structured_content/ocr_text).
          Respects GRAPHRAG_IMAGE_STORE_ENABLED; skips SKIPPED images and
          rows whose text is shorter than GRAPHRAG_IMAGE_MIN_TEXT_LEN.
      7. set Redis dirty flag + enqueue recompute_communities_task

    When GRAPHRAG_ENABLED=False:
      Fall back to the lightweight _build_graph_inputs + upsert_entities path
      (entities + doc-level MENTIONED_IN, no relationship extraction).

    Non-fatal: any exception logs and continues.
    """
    from app.services import graph_service

    if not graph_service.is_available():
        logger.info("[%s] Graph stage skipped (neo4j unavailable)", document_id)
        return

    graph_filename = (
        getattr(router_result, "doc_title", None)
        or parsed_doc.metadata.get("title")
        or (storage_path.split("/")[-1] if storage_path else "")
        or ""
    )
    doc_type = getattr(router_result, "document_type", "") or ""

    if not settings.GRAPHRAG_ENABLED:
        # ── Legacy lightweight path (unchanged behaviour) ──────────────────────
        try:
            from app.services.ingestion_orchestrator import _build_graph_inputs
            doc_meta, entities = _build_graph_inputs(
                parsed_doc, document_id, graph_filename, doc_type,
            )
            graph_service.upsert_document(**doc_meta)
            graph_service.upsert_entities(document_id, entities, filename=graph_filename)
            logger.info("[%s] Graph (legacy): linked %d entities", document_id, len(entities))
        except Exception as exc:
            logger.warning("[%s] Legacy graph stage failed (non-fatal): %s", document_id, exc)
        return

    # ── Full GraphRAG path ─────────────────────────────────────────────────────
    try:
        # 1. Ensure schema (idempotent, cached after first call)
        graph_service.ensure_schema()

        # 2. Clear previous graph for this document (re-ingest safety)
        graph_service.clear_document_graph(document_id)

        # 3. Keep legacy Document node + doc-level MENTIONS for graph_expanded_chunks
        graph_service.upsert_document(
            doc_id=document_id,
            filename=graph_filename,
            doc_type=doc_type,
        )

        # 4. Assemble chunk records (pg_id bridge)
        chunk_records = assemble_chunk_records(
            parsed_doc, chunks, legal_clauses, router_result, stored_ids,
        )
        logger.info("[%s] Graph: assembled %d chunk records", document_id, len(chunk_records))

        # 5. Build the knowledge graph: entities + typed relationships, with
        #    per-chunk pg_id traceability on MENTIONED_IN. No Chunk nodes.
        #    (Entities + MENTIONED_IN are written here, so no separate legacy
        #    doc-level MENTIONS pass is needed — related_documents() reads
        #    MENTIONED_IN directly.)
        doc_meta_graph = {"filename": graph_filename, "doc_type": doc_type}
        counts = build_document_graph(document_id, doc_meta_graph, chunk_records)

        # 6d. Additive entity extraction from table_store (any document with
        #     tables gets table-level entities merged into the same graph).
        # 6e. Additive entity extraction from image_store (VLM structured
        #     content / OCR text for images that survived the pre-filter).
        # Both use MERGE-based upserts — safe to run even when entities from
        # step 5 already exist; no clear needed.
        try:
            from app.services.store_entity_extractor import extract_stores_for_document
            store_counts = extract_stores_for_document(
                document_id=document_id,
                filename=graph_filename,
                doc_type=doc_type,
                stores=["table_store", "image_store"],
            )
            logger.info(
                "[%s] Table/Image graph stage: stores=%d records=%d entities=%d rels=%d failed=%d",
                document_id,
                store_counts["stores_processed"],
                store_counts["records_total"],
                store_counts["entities_total"],
                store_counts["relationships_total"],
                store_counts["failed"],
            )
        except Exception as exc:
            logger.warning("[%s] Table/Image graph stage failed (non-fatal): %s", document_id, exc)

        # 7. Signal community recompute (debounced, async)
        _signal_community_recompute(document_id)

        logger.info(
            "[%s] Graph stage complete: chunks=%d entities=%d rels=%d failed=%d",
            document_id,
            counts["chunks_processed"],
            counts["entities_total"],
            counts["relationships_total"],
            counts.get("chunks_failed_to_write", 0),
        )

    except Exception as exc:
        logger.warning("[%s] Graph stage failed (non-fatal): %s", document_id, exc)


def _signal_community_recompute(document_id: str) -> None:
    """Set Redis dirty flag and enqueue throttled recompute_communities_task."""
    try:
        import redis as _redis
        from app.config import settings as _cfg
        r = _redis.from_url(_cfg.REDIS_URL, decode_responses=True)
        dirty_key = "graphrag:community:dirty_docs"
        ts_key = "graphrag:community:last_build_ts"
        r.incr(dirty_key)
        r.expire(dirty_key, 86400)  # expire after 1 day as safety

        dirty_count = int(r.get(dirty_key) or 0)
        last_build_ts = float(r.get(ts_key) or 0.0)
        import time as _time
        now = _time.time()
        elapsed = now - last_build_ts

        min_interval = _cfg.GRAPHRAG_COMMUNITY_MIN_INTERVAL_SEC
        dirty_threshold = _cfg.GRAPHRAG_COMMUNITY_DIRTY_DOCS

        should_enqueue = (
            elapsed >= min_interval
            or dirty_count >= dirty_threshold
        )

        if should_enqueue:
            from app.core.background_tasks import celery_app
            celery_app.send_task(
                "app.services.community_service.recompute_communities_task",
                kwargs={"document_id": document_id},
            )
            logger.info(
                "[%s] Enqueued recompute_communities_task (dirty=%d elapsed=%.0fs)",
                document_id, dirty_count, elapsed,
            )
        else:
            logger.debug(
                "[%s] Community recompute deferred (dirty=%d elapsed=%.0fs < interval=%ds)",
                document_id, dirty_count, elapsed, min_interval,
            )
    except Exception as exc:
        logger.debug("Community recompute signal failed (non-fatal): %s", exc)


# ── rebuild_graph_for_document (recovery path) ───────────────────────────────

# Mirrors assemble_chunk_records()'s store-selection rule (legal→clause_store,
# research→document_store, else→vector_store) but reads straight from Postgres
# instead of in-memory ingestion objects, since those don't exist outside a live
# ingestion run.
_STORE_TEXT_COLUMN = {
    "clause_store": "clause_text",
    "vector_store": "chunk_text",
}


def _store_for_doc_type(doc_type: str) -> str:
    if "legal" in (doc_type or ""):
        return "clause_store"
    return "vector_store"


def _load_chunk_records_from_postgres(document_id: str, store: str) -> list[dict]:
    """Read chunk_id/text/page_number for `document_id` directly from `store`.
    Returns [] if the document has no rows there (wrong store guess or truly
    no chunks)."""
    from app.db.connection import get_db

    text_col = _STORE_TEXT_COLUMN[store]
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

    return [
        {
            "store": store,
            "chunk_index": r[1],
            "pg_id": str(r[0]),
            "page_number": r[3],
            "section_title": None,
            "text": (r[2] or "").strip(),
        }
        for r in rows
    ]


def rebuild_graph_for_document(document_id: str) -> dict:
    """Rebuild the knowledge graph for an already-ingested document straight
    from its stored Postgres chunks — no re-parsing/re-embedding needed.

    Use this when the graph stage silently failed to persist during the
    original ingestion (e.g. a dropped Neo4j connection mid-run left the
    ingestion log reporting success while zero entities actually landed).

    Returns build_document_graph's counts dict, PLUS "verified_entities" read
    back directly from Neo4j after the rebuild, and "error" if it couldn't run
    at all (Neo4j down, document not found, or no stored chunks).
    Also includes "table_image_entities" / "table_image_records" counts from
    the additive table_store + image_store extraction pass.
    """
    from app.services import graph_service
    from app.db.connection import get_db

    if not graph_service.is_available():
        return {"error": "Neo4j is unavailable", "verified_entities": 0}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT original_filename, document_type FROM multi_store_rag_working.document_registry "
                "WHERE id = %s",
                (document_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"error": f"document {document_id} not found in document_registry", "verified_entities": 0}

    filename, doc_type = row[0] or "", row[1] or ""

    # Guess the store from doc_type first; if that store has no rows for this
    # document (e.g. router disagreed with where chunks actually landed), try
    # the other two stores rather than silently rebuilding nothing.
    guessed_store = _store_for_doc_type(doc_type)
    stores_to_try = [guessed_store] + [s for s in _STORE_TEXT_COLUMN if s != guessed_store]

    chunk_records: list[dict] = []
    for store in stores_to_try:
        chunk_records = _load_chunk_records_from_postgres(document_id, store)
        if chunk_records:
            break

    if not chunk_records:
        return {"error": "no stored chunks found for this document in any store", "verified_entities": 0}

    graph_service.ensure_schema()
    graph_service.clear_document_graph(document_id)
    graph_service.upsert_document(doc_id=document_id, filename=filename, doc_type=doc_type)

    counts = build_document_graph(document_id, {"filename": filename, "doc_type": doc_type}, chunk_records)

    # Also extract entities from table_store + image_store (additive, same
    # MERGE-based upserts as used during live ingestion).
    try:
        from app.services.store_entity_extractor import extract_stores_for_document
        store_counts = extract_stores_for_document(
            document_id=document_id,
            filename=filename,
            doc_type=doc_type,
            stores=["table_store", "image_store"],
        )
        counts["table_image_entities"] = store_counts["entities_total"]
        counts["table_image_relationships"] = store_counts["relationships_total"]
        counts["table_image_records"] = store_counts["records_total"]
        logger.info(
            "[%s] rebuild: table/image store extraction added %d entities from %d records",
            document_id, store_counts["entities_total"], store_counts["records_total"],
        )
    except Exception as exc:
        logger.warning("[%s] rebuild: table/image store extraction failed (non-fatal): %s", document_id, exc)

    # Verify against Neo4j directly rather than trust the (now-honest, but
    # still worth double-checking) counters.
    verified_entities = 0
    try:
        drv = graph_service._get_driver()
        with graph_service._session(drv) as session:
            rec = session.run(
                "MATCH (e:Entity)-[:MENTIONED_IN]->(:Document {id: $id}) RETURN count(e) AS c",
                id=document_id,
            ).single()
            verified_entities = rec["c"] if rec else 0
    except Exception as exc:
        logger.warning("[%s] rebuild verification query failed: %s", document_id, exc)

    counts["verified_entities"] = verified_entities
    logger.info(
        "[%s] rebuild_graph_for_document: %d chunk records, %d written, %d verified in Neo4j",
        document_id, len(chunk_records), counts.get("chunks_processed", 0), verified_entities,
    )
    return counts
