import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, status

from app.core.exceptions import DocumentNotFoundError
from app.db.connection import get_db
from app.models.responses import DocumentDetail, DocumentListResponse, DocumentSummary

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    doc_status: Optional[str] = Query(None, alias="status"),
    document_type: Optional[str] = Query(None),
):
    offset = (page - 1) * limit
    conditions = []
    params: list = []

    if doc_status:
        conditions.append("status = %s")
        params.append(doc_status)
    if document_type:
        conditions.append("document_type ILIKE %s")
        params.append(f"%{document_type}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM multi_store_rag_working.document_overview {where}",
                params,
            )
            total = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT id, original_filename, document_type, document_subtype,
                       status, page_count, word_count, router_confidence,
                       doc_title, doc_summary, vector_chunks, table_count,
                       clause_count, completed_at, created_at, error_message
                FROM multi_store_rag_working.document_overview
                {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

    items = []
    for r in rows:
        raw_type = r[2] or ""
        type_list = [t.strip() for t in raw_type.split(",") if t.strip()]
        items.append(
            DocumentSummary(
                id=str(r[0]),
                original_filename=r[1],
                document_type=r[2],
                document_types=type_list,
                document_subtype=r[3],
                status=r[4],
                page_count=r[5],
                word_count=r[6],
                router_confidence=r[7],
                doc_title=r[8],
                doc_summary=r[9],
                vector_chunks=r[10] or 0,
                table_count=r[11] or 0,
                clause_count=r[12] or 0,
                completed_at=r[13],
                created_at=r[14],
                error_message=r[15],
            )
        )

    return DocumentListResponse(
        items=items,
        total=total,
        page=page,
        pages=max(1, (total + limit - 1) // limit),
        limit=limit,
    )


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(document_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.original_filename, d.document_type, d.document_subtype,
                       d.status, d.page_count, d.word_count, d.router_confidence,
                       d.doc_title, d.doc_summary, d.router_reasoning,
                       d.doc_author, d.doc_date, d.has_tables, d.has_images,
                       d.language_detected, d.doc_metadata, d.storage_path,
                       d.completed_at, d.created_at, d.error_message,
                       COALESCE(vs.n, 0), COALESCE(ts.n, 0),
                       COALESCE(cs.n, 0)
                FROM multi_store_rag_working.document_registry d
                LEFT JOIN (SELECT document_id, COUNT(*) n FROM multi_store_rag_working.vector_store  GROUP BY document_id) vs ON vs.document_id = d.id
                LEFT JOIN (SELECT document_id, COUNT(*) n FROM multi_store_rag_working.table_store    GROUP BY document_id) ts ON ts.document_id = d.id
                LEFT JOIN (SELECT document_id, COUNT(*) n FROM multi_store_rag_working.clause_store   GROUP BY document_id) cs ON cs.document_id = d.id
                WHERE d.id = %s
                """,
                (document_id,),
            )
            row = cur.fetchone()

    if not row:
        raise DocumentNotFoundError(document_id)

    raw_type = row[2] or ""
    type_list = [t.strip() for t in raw_type.split(",") if t.strip()]

    return DocumentDetail(
        id=str(row[0]),
        original_filename=row[1],
        document_type=row[2],
        document_types=type_list,
        document_subtype=row[3],
        status=row[4],
        page_count=row[5],
        word_count=row[6],
        router_confidence=row[7],
        doc_title=row[8],
        doc_summary=row[9],
        router_reasoning=row[10],
        doc_author=row[11],
        doc_date=str(row[12]) if row[12] else None,
        has_tables=row[13] or False,
        has_images=row[14] or False,
        language_detected=row[15] or "en",
        doc_metadata=row[16] or {},
        storage_path=row[17],
        completed_at=row[18],
        created_at=row[19],
        error_message=row[20],
        vector_chunks=row[21] or 0,
        table_count=row[22] or 0,
        clause_count=row[23] or 0,
    )


@router.get("/{document_id}/page-stats")
def get_page_stats(document_id: str):
    """Aggregate per-page stats from all chunk stores for a document."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT page_count, word_count, has_tables, has_images, language_detected
                   FROM multi_store_rag_working.document_registry WHERE id = %s""",
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                raise DocumentNotFoundError(document_id)

            page_count, word_count, has_tables, has_images, language = row

            if not page_count:
                return {"total_pages": 0, "word_count": word_count or 0,
                        "has_tables": False, "has_images": False, "language": language or "en", "pages": []}

            cur.execute(
                """SELECT page_number, COUNT(*) FROM multi_store_rag_working.vector_store
                   WHERE document_id = %s AND page_number IS NOT NULL GROUP BY page_number""",
                (document_id,),
            )
            text_by_page = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(
                """SELECT page_number, COUNT(*) FROM multi_store_rag_working.table_store
                   WHERE document_id = %s AND page_number IS NOT NULL GROUP BY page_number""",
                (document_id,),
            )
            tables_by_page = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(
                """SELECT page_number, COUNT(*) FROM multi_store_rag_working.clause_store
                   WHERE document_id = %s AND page_number IS NOT NULL GROUP BY page_number""",
                (document_id,),
            )
            clauses_by_page = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(
                """SELECT page_number, COUNT(*) FROM multi_store_rag_working.image_store
                   WHERE document_id = %s AND page_number IS NOT NULL GROUP BY page_number""",
                (document_id,),
            )
            images_by_page = {r[0]: r[1] for r in cur.fetchall()}

    avg_words = (word_count or 0) // page_count if page_count else 0
    pages = [
        {
            "page": p,
            "chunks": text_by_page.get(p, 0),
            "tables": tables_by_page.get(p, 0),
            "clauses": clauses_by_page.get(p, 0),
            "images": images_by_page.get(p, 0),
            "est_words": avg_words,
        }
        for p in range(1, page_count + 1)
    ]

    return {
        "total_pages": page_count,
        "word_count": word_count or 0,
        "has_tables": has_tables or False,
        "has_images": has_images or False,
        "language": language or "en",
        "pages": pages,
    }


def _image_metrics(items: list) -> dict:
    """Aggregate pre-VLM filter tracking data across an image list.

    Pure function (no DB/network) so it can be unit-tested directly.
    """
    metrics = {
        "total": len(items),
        "vlm_processed": 0,
        "ocr_only": 0,
        "skipped": 0,
        "by_stage": {},   # filter_stage -> count (skip None)
        "by_type": {},    # image_type -> count (skip None)
    }
    for it in items:
        st = it["processing_status"]
        if st == "VLM_PROCESSED":
            metrics["vlm_processed"] += 1
        elif st == "OCR_ONLY":
            metrics["ocr_only"] += 1
        elif st == "SKIPPED":
            metrics["skipped"] += 1
        stg = it.get("filter_stage")
        if stg:
            metrics["by_stage"][stg] = metrics["by_stage"].get(stg, 0) + 1
        ityp = it.get("image_type")
        if ityp:
            metrics["by_type"][ityp] = metrics["by_type"].get(ityp, 0) + 1
    avoided = metrics["skipped"] + metrics["ocr_only"]
    metrics["vlm_avoided_pct"] = round(avoided / metrics["total"] * 100, 1) if metrics["total"] else 0.0
    return metrics


@router.get("/{document_id}/images")
def get_document_images(document_id: str):
    """All extracted images for a document with signed URLs + structured_content/OCR.
    Powers the Images stage detail panel (thumbnails the user can inspect).
    structured_content is the primary knowledge column (replaces the old caption column);
    caption key is kept in the response for frontend backward-compatibility.
    Also returns pre-VLM filter tracking fields (processing_status, skip_reason,
    filter_stage, image_type) plus an aggregate `metrics` object."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT image_index, page_number, bbox, storage_path, storage_bucket,
                          width, height, structured_content, ocr_text, vlm_ocr_text,
                          processing_status, skip_reason, filter_stage, image_type
                   FROM multi_store_rag_working.image_store
                   WHERE document_id = %s ORDER BY image_index""",
                (document_id,),
            )
            rows = cur.fetchall()

    from app.services.supabase_storage import create_signed_url
    items = []
    for r in rows:
        image_url = None
        if r[3]:
            try:
                image_url = create_signed_url(r[4] or "rag-documents", r[3], expires_in=3600)
            except Exception as exc:
                logger.warning("signed url failed for image %s: %s", r[3], exc)
        items.append({
            "image_index": r[0], "page_number": r[1], "bbox": r[2],
            "width": r[5], "height": r[6], "caption": r[7] or "",
            "structured_content": r[7] or "",
            "ocr_text": r[8] or "", "vlm_ocr_text": r[9] or "",
            "image_url": image_url,
            "processing_status": r[10] or "VLM_PROCESSED",
            "skip_reason": r[11],
            "filter_stage": r[12],
            "image_type": r[13],
        })
    metrics = _image_metrics(items)
    return {"items": items, "total": len(items), "metrics": metrics}


@router.delete("/{document_id}", status_code=status.HTTP_200_OK)
def delete_document(document_id: str):
    """
    Cascade deletes all store records (FK ON DELETE CASCADE handles child rows).
    Also removes ALL files for this document from Supabase Storage:
    the original PDF and every image/table-crop asset stored in image_store.
    Also clears Neo4j graph data.
    """
    from app.services.supabase_storage import delete_all_document_storage, delete_files
    from app.services import graph_service

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, storage_path, storage_bucket FROM multi_store_rag_working.document_registry WHERE id = %s",
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                raise DocumentNotFoundError(document_id)

            # Collect any explicit image/table asset paths before DB deletion
            cur.execute(
                "SELECT storage_path, storage_bucket FROM multi_store_rag_working.image_store WHERE document_id = %s",
                (document_id,),
            )
            image_rows = cur.fetchall()

    storage_path = row[1]
    bucket = row[2] or "rag-documents"

    # 1. Invalidate retrieval and query caches immediately
    try:
        from app.services import retrieval_cache
        retrieval_cache.clear_all()
    except Exception as exc:
        logger.debug("Could not clear retrieval cache: %s", exc)

    # 2. Delete PostgreSQL record immediately — FK ON DELETE CASCADE cleans all child tables:
    #    (vector_store, table_store, table_row_store, clause_store, image_store, parse_staging, ingestion_jobs)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM multi_store_rag_working.document_registry WHERE id = %s",
                (document_id,),
            )

    # 3. Offload remote cloud storage purging (Supabase S3) and Neo4j graph cleanup to background thread
    #    to prevent HTTP 502 / proxy timeouts on slow external networks.
    import threading

    def _bg_cleanup(doc_id: str, st_path: str, bkt: str, img_rows: list):
        try:
            # Delete explicit image assets
            if img_rows:
                by_bkt: dict[str, list[str]] = {}
                for p, b in img_rows:
                    if p:
                        by_bkt.setdefault(b or bkt, []).append(p)
                for b_name, paths in by_bkt.items():
                    try:
                        delete_files(b_name, paths)
                    except Exception as e:
                        logger.warning("Could not delete asset files from %s for %s: %s", b_name, doc_id, e)

            # Delete all folders (images, tables, staging, original doc)
            delete_all_document_storage(doc_id, storage_path=st_path, bucket=bkt)
            logger.info("Deleted all Supabase Storage folders and assets for document %s", doc_id)

            # Clear Neo4j graph data
            try:
                graph_service.clear_document_graph(doc_id)
                from app.services.graph_build_service import _signal_community_recompute
                _signal_community_recompute(doc_id)
            except Exception as g_exc:
                logger.warning("Graph cleanup warning for %s: %s", doc_id, g_exc)
        except Exception as bg_exc:
            logger.error("Background storage/graph cleanup failed for %s: %s", doc_id, bg_exc)

    threading.Thread(
        target=_bg_cleanup,
        args=(document_id, storage_path, bucket, image_rows),
        daemon=True,
        name=f"cleanup-{document_id}",
    ).start()

    logger.info("Successfully cascade-deleted document %s and scheduled cloud storage/graph purge", document_id)
    return {"deleted": True, "document_id": document_id}


def _try_delete_from_storage(bucket: str, path: str) -> None:
    try:
        from app.services.supabase_storage import delete_files
        delete_files(bucket, [path])
    except Exception as exc:
        logger.warning("Could not delete file from storage (%s): %s", path, exc)


@router.get("/{document_id}/chunks")
def get_document_chunks(
    document_id: str,
    store: str = Query("vector", pattern="^(vector|table|clause)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Return paginated chunks from a specific store for a document.
    store: vector | table | clause
    """
    offset = (page - 1) * limit

    store_map = {
        "vector": ("multi_store_rag_working.vector_store",
                   "id, chunk_index, chunk_text, page_number, section_title, semantic_type, keywords, chunk_metadata"),
        "table": ("multi_store_rag_working.table_store",
                   "id, table_index, table_title, page_number, raw_text, markdown_text, json_data, table_category, row_count, col_count, structured_content"),
        "clause": ("multi_store_rag_working.clause_store",
                   "id, clause_index, clause_number, clause_title, clause_text, clause_type, risk_level, page_number, section_path, parties_mentioned"),
    }

    table_name, columns = store_map[store]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE document_id = %s",
                (document_id,),
            )
            total = cur.fetchone()[0]

            cur.execute(
                f"SELECT {columns} FROM {table_name} WHERE document_id = %s ORDER BY 2 LIMIT %s OFFSET %s",
                (document_id, limit, offset),
            )
            rows = cur.fetchall()
            col_names = [desc[0] for desc in cur.description]

    items = [dict(zip(col_names, row)) for row in rows]
    # Convert UUID and non-serialisable types to strings
    for item in items:
        for k, v in item.items():
            if hasattr(v, '__str__') and not isinstance(v, (str, int, float, bool, list, dict, type(None))):
                item[k] = str(v)

    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
        "store": store,
    }
