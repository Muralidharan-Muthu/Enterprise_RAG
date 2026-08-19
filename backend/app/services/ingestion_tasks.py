"""ingestion_tasks — Feature 1.6 two-task chained ingestion pipeline.

When INGESTION_STAGED_ENABLED=True the monolithic ingest_document task is
replaced by a Celery chain:

    parse_document_task (queue="parse") → chunk_embed_store_task (queue="embed")

parse_document_task
    Downloads the source file, runs Stage 1 (parse), writes ParsedDocument to
    staging via parse_staging_service, and marks document_registry status='parsed'.
    This task can run highly concurrently — it carries no GPU model dependency.

chunk_embed_store_task
    Loads the staged ParsedDocument, then runs Stages 1b (images OCR/VLM/embed),
    2 (routing), 3 (chunking), 4 (embedding), 5 (storing), 6 (graph) — verbatim
    copies of the stage code from ingestion_orchestrator.py.  The embed queue is
    rate-limited and runs concurrently=1 so the 1.3GB BGE model is not overloaded.

Stage ownership
    parse_document_task  → Stage 1 (parse) only
    chunk_embed_store_task → Stages 1b, 2, 3, 4, 5 (all sub-stages), 6

Stage logic is NOT duplicated: the helper functions (_prescan_pages,
_build_page_breakdown, _build_image_records, _is_permanent_error,
_get_current_stage) are imported from ingestion_orchestrator where they remain
as importable module-level functions. Stage 6 (graph) is a single shared helper
run_graph_stage() imported from graph_build_service, called identically from
both this task and the monolith ingest_document.
All job_repo / doc_repo update calls are preserved verbatim.

Windows note
------------
On Windows (solo pool) run TWO separate Celery workers:
    celery -A app.core.background_tasks worker -Q parse -c 4  --loglevel=info
    celery -A app.core.background_tasks worker -Q embed -c 1  --loglevel=info
Linux/Docker prefork can serve both queues in a single worker.
"""
from __future__ import annotations

import logging
import tempfile
import time
import traceback
from pathlib import Path

from celery import chain

from app.core.background_tasks import celery_app
from app.db.repositories import document_registry as doc_repo
from app.db.repositories import ingestion_jobs as job_repo

logger = logging.getLogger(__name__)

# ── Stage helper imports (defined in ingestion_orchestrator, shared) ──────────
# Import lazily inside task bodies to avoid triggering embedding_service.warmup()
# in the parse worker process.


# ── Task 1 of 2: PARSE ────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.services.ingestion_tasks.parse_document_task",
    queue="parse",
    max_retries=2,
    default_retry_delay=30,
)
def parse_document_task(self, document_id: str, storage_path: str, job_id: str) -> dict:
    """Download the source file and run the PARSE stage only.

    On success: ParsedDocument is persisted to Supabase Storage staging and
    document_registry.status is set to 'parsed'.  The return value is passed as
    *prev* to chunk_embed_store_task when used inside a chain.

    On failure: marks the doc failed at 'parsing' exactly as the monolith does,
    and retries on transient errors.
    """
    temp_file: Path | None = None

    try:
        # ── Pre-ingest cleanup ────────────────────────────────
        from app.services.storage_service import delete_document_images
        try:
            delete_document_images(document_id)
        except Exception as _cleanup_exc:
            logger.warning(
                "[%s] Pre-ingest image cleanup failed (non-fatal): %s",
                document_id, _cleanup_exc,
            )

        # ── Download from Supabase Storage ────────────────────
        from app.config import settings
        from app.services.supabase_storage import download_file

        file_bytes = download_file(settings.SUPABASE_STORAGE_BUCKET, storage_path)

        # Docling routes by file extension — temp file must carry real extension.
        import os as _os
        _real_ext = _os.path.splitext(storage_path)[1].lower() or ".pdf"
        tmp = tempfile.NamedTemporaryFile(suffix=_real_ext, delete=False)
        tmp.write(file_bytes)
        tmp.close()
        temp_file = Path(tmp.name)
        local_path = str(temp_file)

        # ── Stage 1: PARSING ──────────────────────────────────
        job_repo.update_job(job_id, "parsing", progress=0)
        doc_repo.update_status(document_id, "parsing")
        t0 = time.monotonic()

        # Import helpers from the monolith (they live there; no logic duplication).
        from app.services.ingestion_orchestrator import (
            _prescan_pages,
            _build_page_breakdown,
        )

        _prescan = None
        try:
            _prescan = _prescan_pages(local_path)
            job_repo.update_job(
                job_id, "parsing", progress=5,
                stage_detail={"parsing": {"pages": _prescan, "total_pages": len(_prescan),
                                          "pages_done": 0, "phase": "parsing"}},
            )
        except Exception as _ps_exc:
            logger.warning(
                "[%s] page pre-scan skipped (live parsing UI will be blank): %s",
                document_id, _ps_exc,
            )

        def _on_parse_progress(pages_done: int, total_pages: int, pages: list) -> None:
            pct = 5 + int((pages_done / total_pages) * 90) if total_pages else 50
            job_repo.update_job(
                job_id, "parsing", progress=min(95, pct),
                stage_detail={"parsing": {"pages": pages, "total_pages": total_pages,
                                          "pages_done": pages_done, "phase": "parsing"}},
            )

        from app.services.document_parser import parse_document
        parsed_doc = parse_document(
            local_path, document_id, on_progress=_on_parse_progress, prescan=_prescan,
        )

        parse_time = time.monotonic() - t0

        # Final per-page breakdown
        try:
            _breakdown = _build_page_breakdown(parsed_doc)
            for _p in _breakdown:
                _p["done"] = True
        except Exception as _pb_exc:
            logger.debug("[%s] page breakdown skipped: %s", document_id, _pb_exc)
            _breakdown = None

        job_repo.update_job(
            job_id, "parsing", progress=100,
            stage_timing=("parsing", parse_time),
            **({"stage_detail": {"parsing": {"pages": _breakdown,
                                             "total_pages": parsed_doc.page_count,
                                             "pages_done": parsed_doc.page_count,
                                             "phase": "done"}}} if _breakdown is not None else {}),
        )
        doc_repo.update_status(
            document_id, "parsed",
            parsed_at=True,
            page_count=parsed_doc.page_count,
            word_count=parsed_doc.word_count,
            has_tables=parsed_doc.has_tables,
            has_images=parsed_doc.has_images,
            doc_title=parsed_doc.metadata.get("title"),
            doc_author=parsed_doc.metadata.get("author"),
        )
        logger.info(
            "[%s] Parsed: %d pages, %d blocks, %d tables",
            document_id, parsed_doc.page_count,
            len(parsed_doc.text_blocks), len(parsed_doc.tables),
        )

        # ── Persist ParsedDocument to staging ─────────────────
        from app.services.parse_staging_service import save_parsed
        blob_path = save_parsed(document_id, parsed_doc)
        logger.info("[%s] Staged ParsedDocument → %s", document_id, blob_path)

        # Clean up temp file (original stays in Supabase Storage)
        if temp_file and temp_file.exists():
            temp_file.unlink()

        return {"document_id": document_id, "job_id": job_id, "blob_path": blob_path}

    except Exception as exc:
        err_tb = traceback.format_exc()
        logger.error("[%s] parse_document_task failed: %s\n%s", document_id, exc, err_tb)

        from app.services.ingestion_orchestrator import _get_current_stage, _is_permanent_error
        current_stage = _get_current_stage(job_id)
        job_repo.update_job(
            job_id, "error",
            error_message=str(exc),
            error_traceback=err_tb[:2000],
        )
        doc_repo.update_status(
            document_id, "failed",
            error_stage=current_stage,
            error_message=str(exc)[:500],
        )

        if temp_file and temp_file.exists():
            temp_file.unlink()

        if not _is_permanent_error(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        raise


# ── Task 2 of 2: CHUNK → EMBED → STORE → GRAPH ───────────────────────────────

def _embed_rate_limit() -> str:
    """Read EMBED_QUEUE_RATE_LIMIT from settings at decoration time.
    Falls back to '10/m' if settings are unavailable (e.g. during tests)."""
    try:
        from app.config import settings as _s
        return _s.EMBED_QUEUE_RATE_LIMIT or "10/m"
    except Exception:
        return "10/m"


@celery_app.task(
    bind=True,
    name="app.services.ingestion_tasks.chunk_embed_store_task",
    queue="embed",
    max_retries=1,
    default_retry_delay=60,
    rate_limit=_embed_rate_limit(),
)
def chunk_embed_store_task(self, prev: dict, document_id: str, job_id: str) -> dict:
    """Load staged ParsedDocument and run Stages 1b → 2 → 3 → 4 → 5 → 6.

    *prev* is the return value of parse_document_task (passed automatically
    by Celery chain). Its presence confirms the parse task succeeded.

    The stage logic below is a verbatim relocation of the corresponding blocks
    from ingestion_orchestrator.ingest_document — no logic changes, same
    job_repo / doc_repo update calls, same error handling shape.

    store_chunks() clears existing chunks first, so this task is re-runnable
    without re-parsing.
    """
    stage_times: dict[str, float] = {}

    try:
        # ── Load staged ParsedDocument ────────────────────────
        from app.services.parse_staging_service import load_parsed, delete_staging
        from app.config import settings
        from app.services.supabase_storage import upload_file

        parsed_doc = load_parsed(document_id)
        logger.info(
            "[%s] Loaded staged ParsedDocument (%d pages)", document_id, parsed_doc.page_count,
        )

        # storage_path is needed for the graph stage filename; recover from prev dict
        # or from document_registry if prev is missing (re-dispatch scenario).
        storage_path: str = (prev or {}).get("storage_path", "")
        if not storage_path:
            try:
                from app.db.connection import get_db
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT storage_path FROM multi_store_rag_working.document_registry WHERE id = %s",
                            (document_id,),
                        )
                        _row = cur.fetchone()
                        storage_path = _row[0] if _row else ""
            except Exception:
                pass

        # Import shared helpers (defined in orchestrator, no duplication)
        from app.services.ingestion_orchestrator import (
            _build_image_records_parallel,
            _is_permanent_error,
            _get_current_stage,
        )

        # ── Stage 1b: IMAGES ──────────────────────────────────
        # Mirrors the monolith's Stage 1b (ingestion_orchestrator.ingest_document):
        # bounded-parallel VLM calls, each record appended to image_store the
        # moment it finishes (no embedding here — image_store is a pure
        # repository; the destination-store embedding happens later in
        # store_image_derived_chunks).
        if parsed_doc.images:
            job_repo.update_job(job_id, "images", progress=0)
            t0 = time.monotonic()
            try:
                from app.services.storage_service import clear_document_images, append_images

                bucket = settings.SUPABASE_STORAGE_BUCKET
                n_images = len(parsed_doc.images)
                clear_document_images(document_id)   # once — appends below survive

                _stored = {"n": 0}

                def _on_record(_i, _res):
                    append_images(document_id, [_res[0]])
                    _stored["n"] += 1
                    job_repo.update_job(
                        job_id, "images", progress=round(_stored["n"] / n_images * 100),
                    )

                records, _embed_texts = _build_image_records_parallel(
                    parsed_doc, document_id, bucket,
                    on_record=_on_record,
                    max_workers=settings.VLM_MAX_CONCURRENCY,
                )
                if _stored["n"]:
                    logger.info("[%s] Stored %d/%d images (incremental)", document_id, _stored["n"], n_images)
                else:
                    logger.warning(
                        "[%s] %d image(s) detected but 0 records built — all uploads/captions failed",
                        document_id, len(parsed_doc.images),
                    )
            except Exception:
                logger.error(
                    "[%s] Image stage failed (non-fatal):\n%s",
                    document_id, traceback.format_exc(),
                )
            job_repo.update_job(
                job_id, "images", progress=100,
                stage_timing=("images", time.monotonic() - t0),
            )

        # ── Stage 2: ROUTING ──────────────────────────────────
        job_repo.update_job(job_id, "routing", progress=0)
        t0 = time.monotonic()

        from app.services.router_service import classify_document
        router_result = classify_document(parsed_doc)

        stage_times["routing"] = time.monotonic() - t0
        job_repo.update_job(
            job_id, "routing", progress=100,
            stage_timing=("routing", stage_times["routing"]),
        )
        doc_repo.update_status(
            document_id, "routed",
            document_type=router_result.document_type,
            document_subtype=router_result.document_subtype,
            router_confidence=router_result.confidence,
            router_reasoning=router_result.reasoning,
            doc_summary=router_result.doc_summary,
            doc_title=router_result.doc_title or parsed_doc.metadata.get("title"),
            doc_author=router_result.doc_author,
        )
        logger.info(
            "[%s] Routed as '%s' (confidence=%.2f, fallback=%s)",
            document_id, router_result.document_type,
            router_result.confidence, router_result.used_fallback,
        )

        # ── Stage 3: CHUNKING ─────────────────────────────────
        job_repo.update_job(job_id, "chunking", progress=0)
        t0 = time.monotonic()

        from app.services.chunker import chunk_document
        chunks = chunk_document(parsed_doc, router_result.document_type)
        legal_clauses = None
        if router_result.document_type == "legal":
            from app.services.gemma_clause_extractor import extract_clauses_gemma
            job_repo.update_job(job_id, "chunking", progress=30)
            legal_clauses, _extraction_meta = extract_clauses_gemma(parsed_doc)
            logger.info(
                "[%s] Clause extraction: %d clauses, source=%s%s",
                document_id, len(legal_clauses), _extraction_meta.source,
                f" (fallback: {_extraction_meta.fallback_reason})"
                if _extraction_meta.fallback_reason else "",
            )
            if _extraction_meta.source == "regex" and legal_clauses:
                try:
                    from app.services.clause_enrichment_service import enrich_clauses_batch
                    job_repo.update_job(job_id, "chunking", progress=70)
                    legal_clauses = enrich_clauses_batch(legal_clauses)
                    logger.info(
                        "[%s] Enriched %d regex clauses", document_id, len(legal_clauses),
                    )
                except Exception as _enr_exc:
                    logger.warning(
                        "[%s] Clause enrichment failed (non-fatal): %s", document_id, _enr_exc,
                    )

        stage_times["chunking"] = time.monotonic() - t0
        total_units = len(legal_clauses or chunks)
        job_repo.update_job(
            job_id, "chunking", progress=100,
            total_chunks=total_units,
            stage_timing=("chunking", stage_times["chunking"]),
        )
        doc_repo.update_status(document_id, "chunked", chunked_at=True)
        logger.info(
            "[%s] Chunked: %d units (type=%s)",
            document_id, total_units, router_result.document_type,
        )

        # ── Stage 4: EMBEDDING ────────────────────────────────
        job_repo.update_job(job_id, "embedding", progress=0)
        t0 = time.monotonic()

        from app.services.embedding_service import embed_passages
        import numpy as np

        if router_result.document_type == "legal" and legal_clauses:
            texts_to_embed = [c.clause_text for c in legal_clauses]
        else:
            texts_to_embed = [c.chunk_text for c in chunks]

        embeddings = (
            embed_passages(texts_to_embed)
            if texts_to_embed
            else np.empty((0, 1024), dtype="float32")
        )

        # Feature 1.5: parent summary + child row-windows
        table_embeddings = np.empty((0, 1024), dtype="float32")
        table_child_chunks: list = []
        table_child_embeddings = np.empty((0, 1024), dtype="float32")
        if parsed_doc.tables:
            from app.services.table_chunker import chunk_tables
            from app.config import settings as _cfg

            table_child_chunks, parent_summary_texts = chunk_tables(
                parsed_doc.tables,
                max_tokens=_cfg.TABLE_CHUNK_MAX_TOKENS,
                max_rows=_cfg.TABLE_CHUNK_MAX_ROWS,
                overlap_rows=_cfg.TABLE_CHUNK_OVERLAP_ROWS,
                max_windows_per_table=_cfg.TABLE_MAX_WINDOWS_PER_TABLE,
            )
            # Only LARGE tables (row_count > TABLE_CHUNK_MAX_ROWS) get children in
            # table_chunk_store. A small table fits in one window = the whole table,
            # already fully in table_store (structured_content + embedding), so drop
            # its windows here — before embedding — no redundant rows, no wasted embed.
            _big_idx = {
                t.table_index for t in parsed_doc.tables
                if len(t.rows) > _cfg.TABLE_CHUNK_MAX_ROWS
            }
            table_child_chunks = [
                c for c in table_child_chunks if c.table_index in _big_idx
            ]
            table_embeddings = embed_passages(parent_summary_texts)
            if table_child_chunks:
                child_texts = [c.serialized_text for c in table_child_chunks]
                table_child_embeddings = embed_passages(child_texts)
                logger.info(
                    "[%s] Table children: %d windows across %d tables",
                    document_id, len(table_child_chunks), len(parsed_doc.tables),
                )

        stage_times["embedding"] = time.monotonic() - t0
        job_repo.update_job(
            job_id, "embedding", progress=100,
            stage_timing=("embedding", stage_times["embedding"]),
        )
        doc_repo.update_status(document_id, "embedded", embedded_at=True)
        logger.info("[%s] Embedded: %d vectors (dim=1024)", document_id, len(embeddings))

        # ── Stage 5: STORING ──────────────────────────────────
        job_repo.update_job(job_id, "storing", progress=0)
        t0 = time.monotonic()

        from app.services.storage_service import (
            store_chunks,
            store_image_derived_chunks,
            store_table_crop_images,
            _table_image_path,
        )

        # Upload cropped table images to the bucket (best-effort)
        table_image_paths: dict = {}
        table_image_records: list[dict] = []
        for t in parsed_doc.tables:
            if getattr(t, "image_png_bytes", None):
                p = _table_image_path(document_id, t.table_index)
                try:
                    upload_file(settings.SUPABASE_STORAGE_BUCKET, p, t.image_png_bytes, "image/png")
                    table_image_paths[t.table_index] = p
                    bbox = None
                    if t.bbox:
                        bbox = {"x1": t.bbox.x1, "y1": t.bbox.y1,
                                "x2": t.bbox.x2, "y2": t.bbox.y2}
                    table_image_records.append({
                        "table_index": t.table_index,
                        "page_number": t.page_number,
                        "bbox": bbox,
                        "storage_path": p,
                        "storage_bucket": settings.SUPABASE_STORAGE_BUCKET,
                        "caption": t.caption or f"Table {t.table_index}",
                        "ocr_text": t.raw_text or "",
                    })
                except Exception as exc:
                    logger.warning(
                        "[%s] table crop upload failed (%d): %s",
                        document_id, t.table_index, exc,
                    )

        # ── Universal VLM table pipeline (staged-path parity with the monolithic
        # orchestrator). Run the VLM on every table crop, reconcile against Docling
        # (numeric source of truth), and persist the VLM structured_content + its
        # embedding. Without this the staged path wrote NULL structured_content /
        # structured_content_embedding for pdf-grid tables. Fail-open: any error
        # keeps the Docling extraction (no regression).
        table_vlm_analyses: dict = {}
        if any(getattr(t, "image_png_bytes", None) for t in parsed_doc.tables):
            try:
                from app.services.table_reconstruction import reconstruct_tables_with_vlm
                table_vlm_analyses = reconstruct_tables_with_vlm(
                    parsed_doc, max_workers=settings.VLM_MAX_CONCURRENCY,
                )
            except Exception as _tv_exc:
                logger.warning("[%s] table-crop VLM reconstruction failed (non-fatal): %s",
                               document_id, _tv_exc)

        table_extraction: dict = {}
        for t in parsed_doc.tables:
            analysis = table_vlm_analyses.get(t.table_index) or {}
            if analysis:
                table_extraction[t.table_index] = {
                    "method": analysis.get("method") or "pdf_grid",
                    "confidence": analysis.get("confidence"),
                    "quality": analysis.get("extraction_quality"),
                    "provenance": analysis.get("provenance")
                    or {"reconciled": False, "source": "docling"},
                }
            else:
                table_extraction[t.table_index] = {
                    "method": "pdf_grid", "confidence": None, "quality": None,
                    "provenance": {"reconciled": False, "source": "docling"},
                }

        from app.services.store_router import _try_json
        from app.services.table_enrichment import enrich_table
        table_enrichment: dict = {}
        table_structured_content: dict = {}
        for t in parsed_doc.tables:
            analysis = table_vlm_analyses.get(t.table_index) or {}
            sc = (analysis.get("structured_content") or "").strip()
            vlm_meta = None
            if sc:
                try:
                    vlm_meta = _try_json(sc)
                except Exception:
                    vlm_meta = None
            table_enrichment[t.table_index] = enrich_table(
                t.headers, t.rows, t.caption, vlm_meta=vlm_meta,
            )
            table_structured_content[t.table_index] = sc or (t.markdown_text or t.raw_text or "")
        _sc_texts = [table_structured_content[t.table_index] for t in parsed_doc.tables]
        table_sc_embeddings = (
            embed_passages(_sc_texts) if _sc_texts else np.empty((0, 1024), dtype="float32")
        )

        stored_ids = store_chunks(
            document_id=document_id,
            document_type=router_result.document_type,
            chunks=chunks,
            embeddings=embeddings,
            parsed_doc=parsed_doc,
            legal_clauses=legal_clauses,
            clause_embeddings=embeddings if legal_clauses else None,
            table_image_paths=table_image_paths,
            table_embeddings=table_embeddings if len(table_embeddings) > 0 else None,
            table_extraction=table_extraction,
            table_enrichment=table_enrichment,
            table_structured_content=table_structured_content,
            table_sc_embeddings=table_sc_embeddings,
        )

        # Stage 5a-cell: populate table_row_store/table_cell_store (migration
        # 019) — the indexed EAV pushdown tables the enterprise structured
        # query engine (table_sql_compiler) reads for exhaustive filter/
        # aggregation/ranking/GROUP BY queries. Independent of
        # TABLE_CHILD_SEARCH_ENABLED (that flag only gates ANN over
        # table_chunk_store) — gated on its own flag, and non-fatal: a
        # failure here just means this table falls back to the tier-2
        # Python/JSONB structured-query engine until backfilled/retried.
        from app.config import settings as _cfg
        if _cfg.TABLE_CELL_STORE_ENABLED:
            try:
                from app.services import table_schema_service
                from app.db.repositories.table_cell_store import (
                    insert_table_rows, insert_table_cells,
                )

                parent_ids = stored_ids.get("table_store", [])
                table_index_to_uuid: dict[int, str] = {
                    t.table_index: parent_ids[i]
                    for i, t in enumerate(parsed_doc.tables)
                    if i < len(parent_ids)
                }

                total_rows = 0
                for t in parsed_doc.tables:
                    table_uuid = table_index_to_uuid.get(t.table_index)
                    if table_uuid is None or not t.headers or not t.rows:
                        continue
                    row_tuples = table_schema_service.build_row_store_rows(
                        document_id, table_uuid, t.headers, t.rows,
                    )
                    total_rows += insert_table_rows(row_tuples)

                if total_rows:
                    logger.info(
                        "[%s] Stored %d unified row(s) in table_row_store",
                        document_id, total_rows,
                    )
            except Exception as _cell_exc:
                logger.warning(
                    "[%s] table_row_store population failed (non-fatal — "
                    "falls back to tier-2 structured query engine): %s",
                    document_id, _cell_exc,
                )

        # Stage 5a-ext: insert table child row-windows into table_chunk_store (Feature 1.5)
        if _cfg.TABLE_CHILD_SEARCH_ENABLED and table_child_chunks and len(table_child_embeddings) > 0:
            try:
                from app.db.repositories.table_chunk_store import insert_table_chunks
                from app.services.table_chunker import build_window_structured_content
                import json as _json

                parent_ids = stored_ids.get("table_store", [])
                table_index_to_uuid: dict[int, str] = {
                    t.table_index: parent_ids[i]
                    for i, t in enumerate(parsed_doc.tables)
                    if i < len(parent_ids)
                }
                table_by_index: dict[int, object] = {
                    t.table_index: t for t in parsed_doc.tables
                }

                # Pass 1: every remaining child belongs to a big table (small ones
                # were filtered before embedding). Build each window's structured_content
                # JSON slice; collect for a single batch embed.
                child_scs: list[str | None] = []
                eligible: list[tuple] = []          # (child, emb, table_uuid)
                sc_texts: list[str] = []
                sc_positions: list[int] = []
                for child, emb in zip(table_child_chunks, table_child_embeddings):
                    table_uuid = table_index_to_uuid.get(child.table_index)
                    if table_uuid is None:
                        continue
                    table = table_by_index.get(child.table_index)
                    sc: str | None = None
                    if table is not None and len(table.rows) > _cfg.TABLE_CHUNK_MAX_ROWS:
                        sc = build_window_structured_content(
                            table, child.row_start, child.row_end,
                        )
                        sc_positions.append(len(eligible))
                        sc_texts.append(sc)
                    child_scs.append(sc)
                    eligible.append((child, emb, table_uuid))

                # Batch-embed the per-window structured_content slices. Degrade to
                # no structured_content on failure (base serialized_text chunk still
                # inserts; query falls back via COALESCE).
                sc_embs: list[list | None] = [None] * len(eligible)
                if sc_texts:
                    try:
                        _sc_child_embs = embed_passages(sc_texts)
                        for pos, vec in zip(sc_positions, _sc_child_embs):
                            sc_embs[pos] = vec.tolist()
                    except Exception as _sce_exc:
                        logger.warning(
                            "[%s] table child structured_content embed failed "
                            "(storing serialized_text only): %s",
                            document_id, _sce_exc,
                        )
                        child_scs = [None] * len(child_scs)

                child_rows: list[tuple] = []
                for (child, emb, table_uuid), sc, sc_emb in zip(eligible, child_scs, sc_embs):
                    child_rows.append((
                        document_id,
                        table_uuid,
                        child.table_index,
                        child.chunk_index,
                        child.row_start,
                        child.row_end,
                        child.serialized_text,
                        child.page_number,
                        emb.tolist(),
                        _json.dumps(child.chunk_metadata),
                        sc,
                        sc_emb if sc is not None else None,
                    ))

                if child_rows:
                    insert_table_chunks(child_rows)
                    from app.db.repositories.table_chunk_store import update_table_chunk_counts
                    from collections import Counter as _Counter
                    update_table_chunk_counts(dict(_Counter(r[1] for r in child_rows)))
                    logger.info(
                        "[%s] Stored %d table child chunks in table_chunk_store",
                        document_id, len(child_rows),
                    )
            except Exception as _tc_exc:
                logger.error(
                    "[%s] table_chunk_store insert FAILED — table children NOT "
                    "stored (non-fatal, parent tables still searchable): %s",
                    document_id, _tc_exc, exc_info=True,
                )

        # Stage 5b: image OCR content into the correct store
        if parsed_doc.images:
            try:
                store_image_derived_chunks(document_id)
            except Exception as _img_store_exc:
                logger.warning(
                    "[%s] store_image_derived_chunks failed (non-fatal): %s",
                    document_id, _img_store_exc,
                )

        # Stage 5c: register table crop images in image_store
        if table_image_records:
            try:
                _ti_texts = [
                    (r["caption"] or "") + ("\n" + r["ocr_text"][:200] if r["ocr_text"] else "")
                    for r in table_image_records
                ]
                _ti_embs = embed_passages(_ti_texts)
                store_table_crop_images(document_id, table_image_records, _ti_embs)
            except Exception as _ti_exc:
                logger.warning(
                    "[%s] table crop image_store registration failed (non-fatal): %s",
                    document_id, _ti_exc,
                )

        stage_times["storing"] = time.monotonic() - t0
        job_repo.update_job(
            job_id, "storing", progress=100,
            processed_chunks=total_units,
            stage_timing=("storing", stage_times["storing"]),
        )
        doc_repo.update_status(document_id, "storing", stored_at=True)

        # ── Stage 6: GRAPH (multi-PDF connections + full GraphRAG, best-effort) ──
        # Unified stage-6 helper — same as in ingestion_orchestrator.ingest_document.
        # GRAPHRAG_ENABLED=False → lightweight legacy path; True → full GraphRAG.
        job_repo.update_job(job_id, "graph", progress=0)
        t0 = time.monotonic()
        try:
            from app.services.graph_build_service import run_graph_stage
            run_graph_stage(
                document_id=document_id,
                parsed_doc=parsed_doc,
                chunks=chunks,
                legal_clauses=legal_clauses,
                router_result=router_result,
                stored_ids=stored_ids,
                storage_path=storage_path,
            )
        except Exception as graph_exc:
            logger.warning("[%s] Graph stage failed (non-fatal): %s", document_id, graph_exc)
        job_repo.update_job(
            job_id, "graph", progress=100,
            stage_timing=("graph", time.monotonic() - t0),
        )

        # ── DONE ──────────────────────────────────────────────
        job_repo.update_job(job_id, "done", progress=100)
        doc_repo.update_status(document_id, "completed", completed_at=True)

        # ── Optional staging cleanup ──────────────────────────
        try:
            if settings.PARSE_STAGING_RETENTION_DAYS == 0:
                delete_staging(document_id)
        except Exception as _del_exc:
            logger.warning("[%s] staging cleanup failed (non-fatal): %s", document_id, _del_exc)

        logger.info(
            "[%s] chunk_embed_store_task complete. Times: %s",
            document_id,
            {k: f"{v:.1f}s" for k, v in stage_times.items()},
        )

        return {
            "document_id": document_id,
            "document_type": router_result.document_type,
            "chunks_stored": total_units,
            "stage_times": stage_times,
        }

    except Exception as exc:
        err_tb = traceback.format_exc()
        logger.error("[%s] chunk_embed_store_task failed: %s\n%s", document_id, exc, err_tb)

        from app.services.ingestion_orchestrator import _get_current_stage, _is_permanent_error
        current_stage = _get_current_stage(job_id)
        job_repo.update_job(
            job_id, "error",
            error_message=str(exc),
            error_traceback=err_tb[:2000],
        )
        doc_repo.update_status(
            document_id, "failed",
            error_stage=current_stage,
            error_message=str(exc)[:500],
        )

        if not _is_permanent_error(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        raise


# ── Chain builder ─────────────────────────────────────────────────────────────

def dispatch_ingestion(document_id: str, storage_path: str, job_id: str) -> None:
    """Build and dispatch the two-task chain with task_id == job_id.

    parse_document_task returns a dict that is passed as *prev* to
    chunk_embed_store_task by Celery chain automatically.  We enrich the
    parse task's return value with storage_path so the embed task can
    recover it without an extra DB query.
    """
    # Patch the parse task's signature to include storage_path in its kwargs
    # so the embed task can read it from *prev*.  We do this by using a simple
    # lambda-style chain: parse returns {"storage_path": ..., ...} and the embed
    # task reads prev.get("storage_path").
    #
    # Implementation: parse_document_task already returns
    # {"document_id", "job_id", "blob_path"}.  We add storage_path by having
    # dispatch_ingestion pass it directly as part of the parse task args — the
    # embed task also gets storage_path from *prev* since we embed it there.
    #
    # Simplest correct approach: pass storage_path in the parse return by adding
    # it to the task — but we do NOT want to modify task signatures.  Instead,
    # we store storage_path in the parse task args and have parse_document_task
    # include it in its return dict (it currently returns storage_path indirectly
    # via blob_path; we rely on the embed task's own fallback DB lookup if needed).
    #
    # The chain is: parse_document_task(document_id, storage_path, job_id)
    #               → chunk_embed_store_task(prev, document_id, job_id)
    ingestion_chain = chain(
        parse_document_task.s(document_id, storage_path, job_id),
        chunk_embed_store_task.s(document_id, job_id),
    )
    ingestion_chain.apply_async(task_id=job_id)
    logger.info(
        "[%s] Dispatched ingestion chain (parse→embed) task_id=%s",
        document_id, job_id,
    )
