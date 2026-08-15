"""
Graph entity extraction Celery tasks.

reprocess_graph_task(document_id, stores, full_rebuild)
    Retroactive entity extraction for ONE document from specified stores.
    When full_rebuild=True (default), clears existing graph data first and
    rebuilds from all stores. When full_rebuild=False, adds only the specified
    stores additively (useful for incremental table/image processing).

reprocess_all_task(stores, full_rebuild)
    Bulk retroactive processing — enqueues reprocess_graph_task for every
    document with status='completed' in document_registry. Protected by a
    Redis lock to prevent concurrent bulk runs.
"""
from __future__ import annotations

import logging

from app.core.background_tasks import celery_app

logger = logging.getLogger(__name__)

# Redis key used as a lock for bulk reprocess runs
_BULK_LOCK_KEY = "graphrag:bulk_reprocess:lock"
_BULK_LOCK_TTL_SEC = 3600  # 1 hour max for a bulk run


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name="app.services.graph_tasks.reprocess_graph_task",
)
def reprocess_graph_task(
    self,
    document_id: str,
    stores: list[str] | None = None,
    full_rebuild: bool = True,
) -> dict:
    """Retroactive entity extraction Celery task for a single document.

    Parameters
    ----------
    document_id : str
        UUID of the document to (re)process.
    stores : list[str] | None
        Stores to process. None means all stores (text + table + image).
        Valid values: "vector_store", "clause_store", "document_store",
        "table_store", "image_store".
    full_rebuild : bool
        If True (default), clear prior graph data before re-extracting.
        If False, run additively (MERGE-safe but may leave stale data if
        entities were renamed/removed since the last run).

    Returns
    -------
    dict
        counts dict from extract_stores_for_document(), plus "document_id".
    """
    from app.services import graph_service
    from app.services.store_entity_extractor import (
        extract_and_upsert_all_stores,
        extract_stores_for_document,
        _detect_text_store,
    )
    from app.db.connection import get_db

    logger.info("[%s] reprocess_graph_task started (full_rebuild=%s stores=%s)",
                document_id, full_rebuild, stores)

    if not graph_service.is_available():
        logger.warning("[%s] reprocess_graph_task: Neo4j unavailable — aborting", document_id)
        return {"document_id": document_id, "error": "Neo4j unavailable",
                "stores_processed": 0, "records_total": 0,
                "entities_total": 0, "relationships_total": 0, "failed": 0}

    try:
        if stores is None:
            # Full all-stores rebuild (most common retroactive case)
            if full_rebuild:
                graph_service.clear_document_graph(document_id)
            result = extract_and_upsert_all_stores(document_id)
        else:
            # Partial store reprocessing
            if full_rebuild:
                # Only clear if rebuilding fully — partial clears are risky
                graph_service.clear_document_graph(document_id)

            # Resolve doc metadata for the targeted extract call
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT original_filename, document_type "
                        "FROM multi_store_rag_working.document_registry WHERE id = %s",
                        (document_id,),
                    )
                    row = cur.fetchone()

            if not row:
                return {"document_id": document_id,
                        "error": f"document {document_id} not found",
                        "stores_processed": 0, "records_total": 0,
                        "entities_total": 0, "relationships_total": 0, "failed": 0}

            filename, doc_type = row[0] or "", row[1] or ""
            graph_service.ensure_schema()
            graph_service.upsert_document(doc_id=document_id, filename=filename, doc_type=doc_type)

            result = extract_stores_for_document(
                document_id=document_id,
                filename=filename,
                doc_type=doc_type,
                stores=stores,
            )

        # Signal community recompute after graph changes
        try:
            from app.services.graph_build_service import _signal_community_recompute
            _signal_community_recompute(document_id)
        except Exception as exc:
            logger.debug("[%s] community recompute signal failed (non-fatal): %s", document_id, exc)

        result["document_id"] = document_id
        logger.info("[%s] reprocess_graph_task complete: %s", document_id, result)
        return result

    except Exception as exc:
        logger.error("[%s] reprocess_graph_task failed: %s", document_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return {"document_id": document_id, "error": str(exc),
                    "stores_processed": 0, "records_total": 0,
                    "entities_total": 0, "relationships_total": 0, "failed": 0}


@celery_app.task(
    bind=True,
    name="app.services.graph_tasks.reprocess_all_task",
)
def reprocess_all_task(
    self,
    stores: list[str] | None = None,
    full_rebuild: bool = True,
) -> dict:
    """Bulk retroactive graph entity extraction for ALL completed documents.

    Scans document_registry for all documents with status='completed' and
    enqueues a reprocess_graph_task for each one. Protected by a Redis lock
    so that duplicate bulk runs do not overlap.

    Parameters
    ----------
    stores : list[str] | None
        Same as reprocess_graph_task.stores — None means all stores.
    full_rebuild : bool
        Passed through to each reprocess_graph_task.

    Returns
    -------
    dict
        {"enqueued": int, "skipped": int, "error": str | None}
    """
    import redis as _redis
    from app.config import settings
    from app.db.connection import get_db

    # Redis distributed lock — prevents concurrent bulk runs
    try:
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        acquired = r.set(_BULK_LOCK_KEY, "1", nx=True, ex=_BULK_LOCK_TTL_SEC)
        if not acquired:
            logger.warning("reprocess_all_task: another bulk run is already active — skipping")
            return {"enqueued": 0, "skipped": 0, "error": "bulk lock held by another run"}
    except Exception as exc:
        logger.warning("reprocess_all_task: Redis lock unavailable (%s) — proceeding anyway", exc)

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM multi_store_rag_working.document_registry WHERE status = 'completed'"
                )
                doc_ids = [str(r[0]) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("reprocess_all_task: DB query failed: %s", exc)
        return {"enqueued": 0, "skipped": 0, "error": str(exc)}

    enqueued = 0
    for doc_id in doc_ids:
        try:
            celery_app.send_task(
                "app.services.graph_tasks.reprocess_graph_task",
                kwargs={"document_id": doc_id, "stores": stores, "full_rebuild": full_rebuild},
            )
            enqueued += 1
        except Exception as exc:
            logger.warning("reprocess_all_task: failed to enqueue %s: %s", doc_id, exc)

    logger.info("reprocess_all_task: enqueued %d/%d documents", enqueued, len(doc_ids))
    return {"enqueued": enqueued, "skipped": len(doc_ids) - enqueued, "error": None}
