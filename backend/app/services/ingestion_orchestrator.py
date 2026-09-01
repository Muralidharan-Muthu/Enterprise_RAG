"""
Celery ingestion pipeline task.

Stages (in order):
  1. parsing   — Docling PDF → ParsedDocument
  2. routing   — Groq / rule-based → document_type
  3. chunking  — type-aware chunker → Chunk list
  4. embedding — BGE bge-large-en-v1.5 → numpy vectors
  5. storing   — bulk insert into correct Supabase store

Each stage writes progress updates to ingestion_jobs so the frontend
can poll GET /api/v1/ingest/status/{job_id} for live updates.
"""
import json
import logging
import tempfile
import time
import traceback
from pathlib import Path

from app.core.background_tasks import celery_app
from app.core.worker_identity import CODE_VERSION, WORKER_ID
from app.db.repositories import document_registry as doc_repo
from app.db.repositories import ingestion_jobs as job_repo
from app.services.supabase_storage import upload_file

logger = logging.getLogger(__name__)

# Embedding model loads lazily on first task execution to keep server startup instant


def _stamp_worker_identity(job_id: str, document_id: str) -> None:
    """Best-effort: stamp ingestion_jobs.worker_id/code_version for this job.

    Defensive by design (fix for the stale-native-worker incident): if
    migration 015 hasn't been applied yet on some environment (columns don't
    exist) or the DB write fails for any other reason, log a warning and
    move on — this must never fail or delay the ingestion job itself.
    """
    try:
        job_repo.update_job(job_id, "parsing", progress=0,
                             worker_id=WORKER_ID, code_version=CODE_VERSION)
    except Exception as exc:
        logger.warning(
            "[%s] worker/code-version stamp failed (non-fatal — likely migration "
            "015 not yet applied): %s", document_id, exc,
        )


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def ingest_document(self, document_id: str, storage_path: str, job_id: str) -> dict:
    """
    Full ingestion pipeline for a single document.

    Feature 1.6 back-compat wrapper
    --------------------------------
    When INGESTION_STAGED_ENABLED=True, this task delegates immediately to the
    two-task chain via ingestion_tasks.dispatch_ingestion().  The chain's first
    task (parse_document_task) will execute in the "parse" queue; the second
    (chunk_embed_store_task) in the "embed" queue.  This task itself returns
    quickly; status tracking is done inside the chain tasks.

    When INGESTION_STAGED_ENABLED=False (default), the full monolithic body
    below runs unchanged — behaviour is byte-identical to before Feature 1.6.

    Downloads from Supabase Storage, processes, then removes the local temp copy.
    Returns a summary dict with chunk counts per store.
    """
    from app.config import settings as _cfg
    if _cfg.INGESTION_STAGED_ENABLED:
        # Delegate to the two-task chain.  dispatch_ingestion() uses apply_async
        # with task_id=job_id so the job_id convention is preserved.
        from app.services.ingestion_tasks import dispatch_ingestion
        dispatch_ingestion(document_id, storage_path, job_id)
        return {"delegated": True, "document_id": document_id, "job_id": job_id}
    # ── Flag OFF: run the original monolithic pipeline ────────────────────────
    stage_times: dict[str, float] = {}
    temp_file: Path | None = None

    try:
        # ── Clean up previous run's image data ───────────────
        # Delete old bucket files (images/ and tables/ paths) and image_store rows
        # so reprocessing always starts from a clean slate. No-op on first ingestion.
        from app.services.storage_service import delete_document_images
        try:
            delete_document_images(document_id)
        except Exception as _cleanup_exc:
            logger.warning("[%s] Pre-ingest image cleanup failed (non-fatal): %s", document_id, _cleanup_exc)

        # ── Download from Supabase Storage ───────────────────
        from app.config import settings
        from app.services.supabase_storage import download_file
        file_bytes = download_file(settings.SUPABASE_STORAGE_BUCKET, storage_path)

        # Docling routes by file extension — the temp file MUST carry the real
        # extension so a .docx is not silently processed as a PDF.
        import os as _os
        _real_ext = _os.path.splitext(storage_path)[1].lower() or ".pdf"
        tmp = tempfile.NamedTemporaryFile(suffix=_real_ext, delete=False)
        tmp.write(file_bytes)
        tmp.close()
        temp_file = Path(tmp.name)
        local_path = str(temp_file)

        # ── Stage 1: PARSING ─────────────────────────────────
        # Stamp worker_id/code_version as early as possible (migration 015) so
        # even a job that fails partway still shows which worker/version
        # touched it — see _stamp_worker_identity for the fail-open guard.
        _stamp_worker_identity(job_id, document_id)
        doc_repo.update_status(document_id, "parsing")
        t0 = time.monotonic()

        # Fast pre-scan (PyMuPDF, ~instant) → total page count + per-page image
        # estimate written BEFORE Docling, so the UI shows all pages (and the
        # upcoming workload) the moment parsing starts.
        # Non-PDF formats (DOCX/PPTX/XLSX/HTML/MD) have no meaningful "pages"
        # to pre-scan and PyMuPDF cannot open them — skip silently.
        _prescan = None
        if _real_ext == ".pdf":
            try:
                _prescan = _prescan_pages(local_path)
                job_repo.update_job(
                    job_id, "parsing", progress=5,
                    stage_detail={"parsing": {"pages": _prescan, "total_pages": len(_prescan),
                                              "pages_done": 0, "phase": "parsing"}},
                )
            except Exception as _ps_exc:
                # warning (not debug): a failed pre-scan means NO live per-page UI
                # during parsing — usually a missing PyMuPDF (fitz) install. Surface
                # it so a silent dependency gap doesn't masquerade as "parse is slow".
                logger.warning("[%s] page pre-scan skipped (live parsing UI will be blank): %s",
                               document_id, _ps_exc)

        # Live per-page progress: the chunked parser calls this after every page
        # chunk so the frontend can show "X of N pages done" with real counts.
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

        stage_times["parsing"] = time.monotonic() - t0
        # Final accurate per-page breakdown (every page marked done).
        try:
            _breakdown = _build_page_breakdown(parsed_doc)
            for _p in _breakdown:
                _p["done"] = True
        except Exception as _pb_exc:
            logger.debug("[%s] page breakdown skipped: %s", document_id, _pb_exc)
            _breakdown = None
        job_repo.update_job(
            job_id, "parsing", progress=100,
            stage_timing=("parsing", stage_times["parsing"]),
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
        logger.info("[%s] Parsed: %d pages, %d blocks, %d tables",
                    document_id, parsed_doc.page_count,
                    len(parsed_doc.text_blocks), len(parsed_doc.tables))

        # ── Stage 1b: IMAGES (extract → OCR + VLM extraction → upload → embed → store) ──
        # Phase 1 (pre-filter + OCR + upload) runs sequentially per image so the
        # ImagePrefilter's stateful duplicate detection stays deterministic.
        # Phase 2 (the slow VLM network call) runs with bounded concurrency
        # (VLM_MAX_CONCURRENCY) via a ThreadPoolExecutor. Each record is written
        # to image_store the moment its VLM call (or fallback) completes — inside
        # the on_record callback below, which as_completed() invokes on the main
        # thread one at a time, so these DB writes never race each other — so the
        # UI's image count climbs live instead of jumping from 0 to N at the end.
        if parsed_doc.images:
            job_repo.update_job(job_id, "images", progress=0)
            t0 = time.monotonic()
            try:
                from app.config import settings as _settings
                from app.services.storage_service import clear_document_images, append_images

                bucket = _settings.SUPABASE_STORAGE_BUCKET
                n_images = len(parsed_doc.images)
                clear_document_images(document_id)   # once — appends below survive

                # image_store is a pure repository — no embedding here. The
                # destination-store embedding is generated later in
                # store_image_derived_chunks from the destination content.
                _stored = {"n": 0}

                def _on_record(_i, _res):
                    append_images(document_id, [_res[0]])   # no clear — append
                    _stored["n"] += 1
                    job_repo.update_job(
                        job_id, "images", progress=round(_stored["n"] / n_images * 100),
                    )

                records, _embed_texts = _build_image_records_parallel(
                    parsed_doc, document_id, bucket,
                    on_record=_on_record,
                    max_workers=_settings.VLM_MAX_CONCURRENCY,
                )
                stored = _stored["n"]
                if stored:
                    logger.info("[%s] Stored %d/%d images (incremental)", document_id, stored, n_images)
                else:
                    logger.warning(
                        "[%s] %d image(s) detected but 0 records built — all uploads/captions failed",
                        document_id, len(parsed_doc.images),
                    )
            except Exception as img_exc:
                logger.error("[%s] Image stage failed (non-fatal):\n%s",
                             document_id, traceback.format_exc())
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
        logger.info("[%s] Routed as '%s' (confidence=%.2f, fallback=%s)",
                    document_id, router_result.document_type,
                    router_result.confidence, router_result.used_fallback)

        # ── Stage 3: CHUNKING ─────────────────────────────────
        job_repo.update_job(job_id, "chunking", progress=0)
        t0 = time.monotonic()

        from app.services.chunker import chunk_document, convert_chunk_to_legal_clause

        # Extract semantic chunks across the document (including LLM chunk classification & metadata)
        all_chunks = chunk_document(parsed_doc, router_result.document_type)

        # Categorize and partition chunks: legal clauses vs general prose
        prose_chunks = []
        clause_list = []
        for c in all_chunks:
            if c.semantic_type == "legal_clause":
                clause_list.append(convert_chunk_to_legal_clause(c, len(clause_list)))
            else:
                prose_chunks.append(c)

        legal_clauses = clause_list if clause_list else None

        # Fallback: if document was classified as legal or has legal aspects, but chunking found no clauses,
        # run extract_clauses
        if router_result.has_type("legal") and not legal_clauses:
            if getattr(settings, "CHUNK_ENRICH_WITH_GROQ", False):
                from app.services.groq_clause_extractor import extract_clauses_groq
                job_repo.update_job(job_id, "chunking", progress=30)
                legal_clauses, _extraction_meta = extract_clauses_groq(parsed_doc)
                logger.info(
                    "[%s] Clause extraction fallback: %d clauses, source=%s%s",
                    document_id, len(legal_clauses), _extraction_meta.source,
                    f" (fallback: {_extraction_meta.fallback_reason})"
                    if _extraction_meta.fallback_reason else "",
                )
                if _extraction_meta.source == "regex" and legal_clauses:
                    try:
                        from app.services.clause_enrichment_service import enrich_clauses_batch
                        job_repo.update_job(job_id, "chunking", progress=70)
                        legal_clauses = enrich_clauses_batch(legal_clauses)
                        logger.info("[%s] Enriched %d regex clauses", document_id, len(legal_clauses))
                    except Exception as _enr_exc:
                        logger.warning("[%s] Clause enrichment failed (non-fatal): %s", document_id, _enr_exc)
            else:
                from app.services.chunker import extract_legal_clauses
                legal_clauses = extract_legal_clauses(parsed_doc)
                logger.info("[%s] Fast regex clause extraction: %d clauses", document_id, len(legal_clauses))

        # Reconcile document types if clauses were discovered in a document not originally labeled as legal
        if legal_clauses and not router_result.has_type("legal"):
            router_result.document_types.append("legal")
            router_result.document_type = ", ".join(router_result.document_types)
            doc_repo.update_status(
                document_id,
                document_type=router_result.document_type,
            )
            logger.info("[%s] Dynamically reconciled 'legal' into document types: %s", document_id, router_result.document_types)

        chunks = prose_chunks
        stage_times["chunking"] = time.monotonic() - t0
        total_units = len(legal_clauses or []) + len(chunks)
        job_repo.update_job(
            job_id, "chunking", progress=100,
            total_chunks=total_units,
            stage_timing=("chunking", stage_times["chunking"]),
        )
        doc_repo.update_status(document_id, "chunked", chunked_at=True)
        logger.info("[%s] Chunked: %d units (%d text chunks, %d legal clauses, types=%s)",
                    document_id, total_units, len(chunks), len(legal_clauses or []), router_result.document_types)

        # ── Stage 4: EMBEDDING ────────────────────────────────
        job_repo.update_job(job_id, "embedding", progress=0)
        t0 = time.monotonic()

        from app.services.embedding_service import embed_passages
        import numpy as np

        clause_texts = [c.clause_text for c in (legal_clauses or [])]
        prose_texts = [c.chunk_text for c in (chunks or [])]

        table_child_chunks: list = []
        parent_summary_texts: list = []
        child_texts: list = []
        if parsed_doc.tables:
            from app.services.table_chunker import chunk_tables
            from app.config import settings as _cfg

            raw_child_chunks, parent_summary_texts = chunk_tables(
                parsed_doc.tables,
                max_tokens=_cfg.TABLE_CHUNK_MAX_TOKENS,
                max_rows=_cfg.TABLE_CHUNK_MAX_ROWS,
                overlap_rows=_cfg.TABLE_CHUNK_OVERLAP_ROWS,
                max_windows_per_table=_cfg.TABLE_MAX_WINDOWS_PER_TABLE,
            )
            _big_idx = {
                t.table_index for t in parsed_doc.tables
                if len(t.rows) > _cfg.TABLE_CHUNK_MAX_ROWS
            }
            table_child_chunks = [
                c for c in raw_child_chunks if c.table_index in _big_idx
            ]
            if table_child_chunks:
                child_texts = [c.serialized_text for c in table_child_chunks]
                logger.info(
                    "[%s] Table children: %d windows across %d tables",
                    document_id, len(table_child_chunks), len(parsed_doc.tables),
                )

        # Single-pass batch embedding across all document assets
        all_texts = clause_texts + prose_texts + parent_summary_texts + child_texts
        if all_texts:
            all_embeddings = embed_passages(all_texts)
        else:
            all_embeddings = np.empty((0, 1024), dtype="float32")

        n_clause = len(clause_texts)
        n_prose = len(prose_texts)
        n_summary = len(parent_summary_texts)
        n_child = len(child_texts)

        clause_embeddings = all_embeddings[0:n_clause] if n_clause else None
        embeddings = all_embeddings[n_clause:n_clause + n_prose] if n_prose else np.empty((0, 1024), dtype="float32")
        table_embeddings = all_embeddings[n_clause + n_prose:n_clause + n_prose + n_summary] if n_summary else np.empty((0, 1024), dtype="float32")
        table_child_embeddings = all_embeddings[n_clause + n_prose + n_summary:] if n_child else np.empty((0, 1024), dtype="float32")

        stage_times["embedding"] = time.monotonic() - t0
        job_repo.update_job(
            job_id, "embedding", progress=100,
            stage_timing=("embedding", stage_times["embedding"]),
        )
        doc_repo.update_status(document_id, "embedded", embedded_at=True)
        logger.info("[%s] Embedded: %d prose vectors, %d clause vectors (dim=1024)",
                    document_id, len(embeddings), len(clause_embeddings) if clause_embeddings is not None else 0)

        # ── Stage 5: STORING ──────────────────────────────────
        job_repo.update_job(job_id, "storing", progress=0)
        t0 = time.monotonic()

        from app.config import settings as _settings
        from app.services.storage_service import (
            store_chunks, store_image_derived_chunks,
            store_table_crop_images, _table_image_path,
        )

        # Upload cropped table images to the bucket (best-effort), collect paths
        # and build records for image_store registration.
        table_image_paths: dict = {}
        table_image_records: list[dict] = []
        for t in parsed_doc.tables:
            if getattr(t, "image_png_bytes", None):
                p = _table_image_path(document_id, t.table_index)
                try:
                    upload_file(_settings.SUPABASE_STORAGE_BUCKET, p, t.image_png_bytes, "image/png")
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
                        "storage_bucket": _settings.SUPABASE_STORAGE_BUCKET,
                        "caption": t.caption or f"Table {t.table_index}",
                        "ocr_text": t.raw_text or "",
                    })
                except Exception as exc:
                    logger.warning("[%s] table crop upload failed (%d): %s", document_id, t.table_index, exc)

        # Task 1: reconstruct table crops with the VLM (original image + Docling text
        # as OCR evidence) so table_store receives clean structured rows/cells rather
        # than raw OCR / TableFormer output. Mutates parsed_doc.tables in place; on any
        # failure the Docling extraction is kept (no regression). Bounded-parallel.
        table_vlm_analyses: dict = {}
        if any(getattr(t, "image_png_bytes", None) for t in parsed_doc.tables):
            try:
                from app.services.table_reconstruction import reconstruct_tables_with_vlm
                table_vlm_analyses = reconstruct_tables_with_vlm(
                    parsed_doc, max_workers=_settings.VLM_MAX_CONCURRENCY,
                )
            except Exception as _tv_exc:
                logger.warning("[%s] table-crop VLM reconstruction failed (non-fatal): %s",
                               document_id, _tv_exc)
            # Enrich the image_store repository row with the VLM output when present.
            for _r in table_image_records:
                _a = table_vlm_analyses.get(_r["table_index"])
                if _a and _a.get("structured_content"):
                    _r["vlm_ocr_text"] = _a.get("vlm_ocr_text", "") or _r.get("ocr_text", "")
                    _r["structured_content"] = _a.get("structured_content", "")
                    _r["processing_status"] = "VLM_PROCESSED"

        # Slice 2a (migration 014, write-time lineage): register the table-crop
        # image_store rows BEFORE the table_store rows so their UUIDs already
        # exist and can be threaded into _store_tables as source_image_id. This
        # replaces the old post-hoc backfill (_ensure_table_crop_ocr_in_table_store,
        # removed) which only ever fired when _store_tables hadn't already written
        # the row — i.e. never, since _store_tables always runs for every table.
        crop_image_ids: dict[int, str] = {}
        table_crop_registration_error: str | None = None
        if table_image_records:
            _ti_texts = [
                (r["caption"] or "") + ("\n" + r["ocr_text"][:200] if r["ocr_text"] else "")
                for r in table_image_records
            ]
            _ti_attempts = 2
            for _ti_attempt in range(1, _ti_attempts + 1):
                try:
                    _ti_embs = embed_passages(_ti_texts)
                    crop_image_ids = store_table_crop_images(document_id, table_image_records, _ti_embs)
                    table_crop_registration_error = None
                    break
                except Exception as _ti_exc:
                    table_crop_registration_error = str(_ti_exc)
                    logger.warning(
                        "[%s] table crop image_store registration failed (attempt %d/%d): %s",
                        document_id, _ti_attempt, _ti_attempts, _ti_exc,
                    )

        # Build per-table lineage dicts (keyed by table_index) for _store_tables:
        #   table_source_image_ids — the crop's image_store UUID (None if no crop
        #     was uploaded/registered for that table).
        #   table_extraction — {"method": "image_vlm"|"pdf_grid", "confidence": ...,
        #     "quality": ..., "provenance": {...}}. Slice 2b: reconstruct_tables_with_vlm
        #     already ran the Docling-vs-VLM reconciliation (numeric-faithfulness gate,
        #     design Section 10) for every table with a crop — consume its verdict
        #     directly rather than re-deriving "VLM used" from parse_vlm_table alone
        #     (that would blindly trust any parseable VLM table, which is exactly what
        #     the faithfulness gate exists to prevent).
        table_source_image_ids: dict[int, str | None] = {
            t.table_index: crop_image_ids.get(t.table_index) for t in parsed_doc.tables
        }
        table_extraction: dict[int, dict] = {}
        for t in parsed_doc.tables:
            analysis = table_vlm_analyses.get(t.table_index) or {}
            if analysis:
                table_extraction[t.table_index] = {
                    "method": analysis.get("method") or "pdf_grid",
                    "confidence": analysis.get("confidence"),
                    "quality": analysis.get("extraction_quality"),
                    "provenance": analysis.get("provenance") or {
                        "reconciled": False, "source": "docling",
                    },
                }
            else:
                # No crop/VLM ran for this table (no image_png_bytes) — pure
                # Docling extraction, no confidence signal to bucket from.
                table_extraction[t.table_index] = {
                    "method": "pdf_grid",
                    "confidence": None,
                    "quality": None,
                    "provenance": {"reconciled": False, "source": "docling"},
                }

        # Slice 3 (table-store enterprise design): populate fiscal_year,
        # reporting_period, currency, table_category, detected_units,
        # table_summary UNIFORMLY for every table — no new LLM call. When the
        # VLM already ran for this table's crop (table_vlm_analyses), its
        # structured_content JSON was produced for exactly this purpose
        # (TableStoreHandler.parse consumes the same fields); reuse it as
        # enrich_table's vlm_meta so image-derived and Docling-grid tables end
        # up with identical metadata quality. Docling-grid tables (no crop /
        # VLM did not run / VLM output wasn't parseable JSON) fall back to
        # enrich_table's rules-based derivation from the table's own cells.
        from app.services.store_router import _try_json  # local import — avoids circular deps
        from app.services.table_enrichment import enrich_table

        table_enrichment: dict[int, dict] = {}
        for t in parsed_doc.tables:
            analysis = table_vlm_analyses.get(t.table_index) or {}
            vlm_meta = None
            structured_content = analysis.get("structured_content")
            if structured_content:
                try:
                    vlm_meta = _try_json(structured_content)
                except Exception as _vm_exc:
                    logger.debug(
                        "[%s] table %s: VLM structured_content not parseable for "
                        "enrichment meta (falling back to rules): %s",
                        document_id, t.table_index, _vm_exc,
                    )
                    vlm_meta = None
            table_enrichment[t.table_index] = enrich_table(
                t.headers, t.rows, t.caption, vlm_meta=vlm_meta,
            )

        # Universal VLM pipeline: persist the VLM structured_content (the clean,
        # retrieval-ready extraction) per table and embed it into a dedicated
        # structured_content_embedding column. Fall back to markdown_text/raw_text
        # so the column + embedding are populated for every SMALL table even when
        # the VLM produced nothing (crop render or VLM call failed). Positional
        # embeddings align with parsed_doc.tables order (same contract as
        # table_embeddings and _store_tables). Big tables (row_count >
        # TABLE_CHUNK_MAX_ROWS) are skipped here — storage_service leaves their
        # table_store.structured_content NULL since the same content is already
        # sliced per-window into table_chunk_store below — so there's no point
        # spending an embed call on a whole-table blob that gets discarded.
        from app.config import settings as _cfg

        table_structured_content: dict[int, str] = {}
        _sc_texts: list[str] = []
        _sc_positions: list[int] = []
        for idx, t in enumerate(parsed_doc.tables):
            if len(t.rows) > _cfg.TABLE_CHUNK_MAX_ROWS:
                continue
            analysis = table_vlm_analyses.get(t.table_index) or {}
            sc = (analysis.get("structured_content") or "").strip()
            text = sc or (t.markdown_text or t.raw_text or "")
            table_structured_content[t.table_index] = text
            _sc_positions.append(idx)
            _sc_texts.append(text)
        table_sc_embeddings = np.zeros((len(parsed_doc.tables), 1024), dtype="float32")
        if _sc_texts:
            for pos, vec in zip(_sc_positions, embed_passages(_sc_texts)):
                table_sc_embeddings[pos] = vec

        stored_ids = store_chunks(
            document_id=document_id,
            document_type=router_result.document_type,
            chunks=chunks,
            embeddings=embeddings,
            parsed_doc=parsed_doc,
            legal_clauses=legal_clauses,
            clause_embeddings=clause_embeddings if legal_clauses else None,
            table_image_paths=table_image_paths,
            table_embeddings=table_embeddings if len(table_embeddings) > 0 else None,
            table_source_image_ids=table_source_image_ids,
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
        if _cfg.TABLE_CELL_STORE_ENABLED:
            try:
                from app.services import table_schema_service
                from app.services.embedding_service import embed_passages
                from app.db.repositories.table_cell_store import insert_table_rows

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
                    unified = table_schema_service.build_unified_rows(t.headers, t.rows)
                    row_texts = [r.row_text for r in unified if r.row_text]
                    row_embs = None
                    if row_texts:
                        try:
                            row_embs = embed_passages(row_texts)
                        except Exception as _emb_err:
                            logger.warning("[%s] table row embedding failed: %s", document_id, _emb_err)
                    row_tuples = table_schema_service.build_row_store_rows(
                        document_id, table_uuid, t.headers, t.rows, embeddings=row_embs,
                    )
                    total_rows += insert_table_rows(row_tuples)

                if total_rows:
                    logger.info(
                        "[%s] Stored %d unified row(s) with embeddings in table_row_store",
                        document_id, total_rows,
                    )
            except Exception as _cell_exc:
                logger.warning(
                    "[%s] table_row_store population failed (non-fatal — "
                    "falls back to tier-2 structured query engine): %s",
                    document_id, _cell_exc,
                )

        # Stage 5a-ext: insert table child row-windows into table_chunk_store.
        # Pairing: stored_ids["table_store"] is the list of table_store UUIDs in
        # table_index order (returned by _store_tables via RETURNING id).  Each
        # TableRowChunk carries .table_index which maps to position in that list.
        # We build a dict {table_index → table_store_uuid} and pair each child.
        # Gated on TABLE_CHILD_SEARCH_ENABLED; non-fatal if it fails.
        from app.config import settings as _cfg
        if _cfg.TABLE_CHILD_SEARCH_ENABLED and table_child_chunks and len(table_child_embeddings) > 0:
            try:
                from app.db.repositories.table_chunk_store import insert_table_chunks
                from app.services.table_chunker import build_window_structured_content
                import json as _json

                parent_ids = stored_ids.get("table_store", [])
                # Build mapping: position in parsed_doc.tables → table_store UUID
                # parsed_doc.tables is in the same order that _store_tables inserts
                # (and RETURNING id returns rows in insert order).
                table_index_to_uuid: dict[int, str] = {
                    t.table_index: parent_ids[i]
                    for i, t in enumerate(parsed_doc.tables)
                    if i < len(parent_ids)
                }

                # Per-window structured_content for LARGE tables only (migration
                # 018). A big table's whole-table structured_content_embedding on
                # table_store is one diluted vector; here each ≤25-row window gets
                # a JSON slice of its own canonical rows + embedding, so retrieval
                # can match/surface the structured view at window granularity.
                # Rows are table.rows[row_start:row_end+1] — the reconciled rows
                # (VLM when it won the faithfulness gate, Docling otherwise), never
                # a re-parse of the raw VLM blob.
                table_by_index: dict[int, object] = {
                    t.table_index: t for t in parsed_doc.tables
                }
                # Pass 1: decide which children are "big-table" and build their
                # structured_content JSON. Collect texts for a single batch embed.
                child_scs: list[str | None] = []          # aligned to eligible children
                eligible: list[tuple] = []                # (child, emb, table_uuid)
                sc_texts: list[str] = []
                sc_positions: list[int] = []              # index into eligible for each sc_text
                for child, emb in zip(table_child_chunks, table_child_embeddings):
                    table_uuid = table_index_to_uuid.get(child.table_index)
                    if table_uuid is None:
                        # Parent was not stored (e.g. table had no rows → skipped)
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

                # Batch-embed the big-table structured_content slices in one call
                # (matches the parent embed_passages pattern). Degrade to no
                # structured_content on failure — base serialized_text chunk still
                # inserts, query falls back via COALESCE.
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
                else:
                    logger.warning(
                        "[%s] table_chunk_store: 0 child rows built from %d table "
                        "child chunks (nothing to insert)",
                        document_id, len(table_child_chunks),
                    )
            except Exception as _tc_exc:
                # Non-fatal (the parent table_store rows are already committed),
                # but log LOUD with a full traceback: a silent empty
                # table_chunk_store (e.g. a schema/migration mismatch) is
                # otherwise invisible until someone inspects the table by hand.
                logger.error(
                    "[%s] table_chunk_store insert FAILED — table children NOT "
                    "stored (non-fatal, parent tables still searchable): %s",
                    document_id, _tc_exc, exc_info=True,
                )

        # Stage 5b: write image OCR content into the correct store and update stored_in.
        # Runs after store_chunks() so the regular-chunk clear doesn't wipe these rows.
        if parsed_doc.images:
            try:
                store_image_derived_chunks(document_id)
            except Exception as _img_store_exc:
                logger.warning("[%s] store_image_derived_chunks failed (non-fatal): %s",
                               document_id, _img_store_exc)

        # (Table-crop image_store rows were registered earlier — before store_chunks —
        # so their UUIDs could be threaded into table_store.source_image_id. See the
        # "Slice 2a" block above. The old post-store_chunks registration was removed.)

        stage_times["storing"] = time.monotonic() - t0
        job_repo.update_job(
            job_id, "storing", progress=100,
            processed_chunks=total_units,
            stage_timing=("storing", stage_times["storing"]),
        )
        doc_repo.update_status(document_id, "storing", stored_at=True)

        # ── Stage 6: GRAPH (multi-PDF connections + full GraphRAG, best-effort) ──
        # Unified stage-6 helper handles both the lightweight legacy path
        # (GRAPHRAG_ENABLED=False) and the full GraphRAG path (GRAPHRAG_ENABLED=True).
        # Entirely non-fatal: a down/disabled Neo4j logs and continues.
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
        # Lineage-completeness gate: verify every table that had a crop
        # candidate actually got a registered source_image_id. A silent
        # registration failure (table_crop_registration_error) or a partial
        # failure must not be reported as a clean "completed" — see
        # _find_table_lineage_gap for the pure-function detection logic.
        lineage_gap = _find_table_lineage_gap(table_image_records, crop_image_ids)

        # Table-count sanity gate: a general storage-completeness invariant —
        # every table the parser decided on for this run must produce exactly
        # one table_store row. Catches silent structural corruption (e.g. a
        # stale worker racing the live one on the same queue, partial writes)
        # that the lineage gap above does not cover, since it only inspects
        # crop-image registration, not table_store insert counts.
        parsed_table_count = len(parsed_doc.tables)
        stored_table_count = len(stored_ids.get("table_store", []))
        table_count_mismatch = _find_table_count_mismatch(parsed_table_count, stored_table_count)

        if lineage_gap:
            lineage_msg = (
                f"Table image lineage incomplete for table_index {lineage_gap}: "
                f"crop candidate present but source_image_id was not registered "
                f"(last error: {table_crop_registration_error or 'unknown'})"
            )
            logger.error("[%s] %s", document_id, lineage_msg)
            job_repo.update_job(job_id, "error", error_message=lineage_msg)
            doc_repo.update_status(
                document_id, "completed_with_errors",
                error_stage="storing",
                error_message=lineage_msg[:500],
            )
        elif table_count_mismatch:
            count_msg = (
                f"Table storage mismatch: parser produced {parsed_table_count} table(s) "
                f"but {stored_table_count} table_store row(s) were inserted "
                f"(worker_id={WORKER_ID}, code_version={CODE_VERSION}) -- "
                f"possible stale worker or partial write."
            )
            logger.error("[%s] %s", document_id, count_msg)
            job_repo.update_job(job_id, "error", error_message=count_msg)
            doc_repo.update_status(
                document_id, "completed_with_errors",
                error_stage="storing",
                error_message=count_msg[:500],
            )
        else:
            job_repo.update_job(job_id, "done", progress=100)
            doc_repo.update_status(document_id, "completed", completed_at=True)

        logger.info(
            "[%s] Pipeline complete. Times: %s",
            document_id,
            {k: f"{v:.1f}s" for k, v in stage_times.items()},
        )

        # Remove local temp copy (original stays in Supabase Storage)
        if temp_file and temp_file.exists():
            temp_file.unlink()

        return {
            "document_id": document_id,
            "document_type": router_result.document_type,
            "chunks_stored": total_units,
            "stage_times": stage_times,
        }

    except Exception as exc:
        err_tb = traceback.format_exc()
        logger.error("[%s] Pipeline failed: %s\n%s", document_id, exc, err_tb)

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

        # Only retry transient errors (network blips, temporary DB issues).
        # Permanent errors (file not found, auth denied) will never succeed on retry.
        if not _is_permanent_error(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        raise


def _build_table_embeddings(tables: list, document_id: str):
    """Build parent-summary and (optionally) child row-window embeddings for
    every table in a parsed document.

    Gating: child row-window chunking (chunk_tables()) and its embedding call
    are SKIPPED entirely when settings.TABLE_CHILD_SEARCH_ENABLED is False —
    that work is otherwise wasted, since the only consumer (the
    table_chunk_store insert) is itself gated on the same flag. Parent summary
    texts/embeddings are always built; tables are a universal content stream
    (table_store), independent of the child-search flag.

    Returns
    -------
    (table_embeddings, table_child_chunks, table_child_embeddings)
    - table_embeddings: np.ndarray, one row per table (empty (0, 1024) if no tables)
    - table_child_chunks: list[TableRowChunk] (empty when tables is empty or
      TABLE_CHILD_SEARCH_ENABLED is False)
    - table_child_embeddings: np.ndarray, one row per child chunk (empty (0, 1024)
      when there are no child chunks)
    """
    import numpy as np
    from app.services.embedding_service import embed_passages

    table_embeddings = np.empty((0, 1024), dtype="float32")
    table_child_chunks: list = []       # list[TableRowChunk]
    table_child_embeddings = np.empty((0, 1024), dtype="float32")

    if not tables:
        return table_embeddings, table_child_chunks, table_child_embeddings

    from app.services.table_chunker import chunk_tables, build_table_summary_text
    from app.config import settings as _cfg

    if _cfg.TABLE_CHILD_SEARCH_ENABLED:
        # Build parent summary texts AND all child row-windows in one call
        table_child_chunks, parent_summary_texts = chunk_tables(
            tables,
            max_tokens=_cfg.TABLE_CHUNK_MAX_TOKENS,
            max_rows=_cfg.TABLE_CHUNK_MAX_ROWS,
            overlap_rows=_cfg.TABLE_CHUNK_OVERLAP_ROWS,
            max_windows_per_table=_cfg.TABLE_MAX_WINDOWS_PER_TABLE,
        )
        # Only LARGE tables (row_count > TABLE_CHUNK_MAX_ROWS) get children in
        # table_chunk_store. A small table (<= cap) fits in a single window =
        # the whole table, which is already fully represented in table_store
        # (structured_content + embedding), so it needs no child rows. Drop its
        # windows here — before embedding — so we neither store redundant chunk
        # rows nor waste an embed call on them.
        _big_idx = {
            t.table_index for t in tables
            if len(t.rows) > _cfg.TABLE_CHUNK_MAX_ROWS
        }
        table_child_chunks = [
            c for c in table_child_chunks if c.table_index in _big_idx
        ]
    else:
        # Child search disabled: skip the expensive chunk_tables() row-window
        # split and its embed_passages() call entirely — that work is only
        # ever consumed by the table_chunk_store insert, which is itself
        # gated on this same flag. Parent summaries are still needed.
        parent_summary_texts = [build_table_summary_text(t) for t in tables]

    # Parent summary embeddings (one per table, aligned to tables order)
    table_embeddings = embed_passages(parent_summary_texts)
    # Child window embeddings (one per TableRowChunk)
    if table_child_chunks:
        child_texts = [c.serialized_text for c in table_child_chunks]
        table_child_embeddings = embed_passages(child_texts)
        logger.info(
            "[%s] Table children: %d windows across %d tables",
            document_id, len(table_child_chunks), len(tables),
        )

    return table_embeddings, table_child_chunks, table_child_embeddings


def _find_table_lineage_gap(table_image_records: list[dict], crop_image_ids: dict) -> list[int]:
    """Return the table_index values that had a crop candidate but ended up
    with no registered source_image_id.

    A table only counts as a lineage gap when it HAD a crop candidate (an
    entry in table_image_records) — pure pdf_grid tables that never produced
    an image crop are correctly excluded, not false-positives. A falsy id
    (None, "") registered against a table_index still counts as missing.
    """
    gap: list[int] = []
    for record in table_image_records:
        table_index = record["table_index"]
        if not crop_image_ids.get(table_index):
            gap.append(table_index)
    return gap


def _find_table_count_mismatch(parsed_table_count: int, stored_table_count: int) -> bool:
    """Return True when the parser's final table count and the number of
    table_store rows actually inserted for this run disagree.

    General storage-completeness invariant (not multi-page-continuation
    specific): every table the parser decided on must produce exactly one
    table_store row. 0 parsed / 0 stored is a match, not a mismatch — the
    overwhelming common case (documents with tables, N parsed == N stored)
    must never false-positive.
    """
    return parsed_table_count != stored_table_count


def _is_permanent_error(exc: Exception) -> bool:
    """Returns True for errors that retrying will never fix (file missing, auth denied)."""
    try:
        # storage3 StorageException stores the response dict as args[0]
        if exc.args:
            info = exc.args[0]
            if isinstance(info, dict):
                status = info.get("statusCode") or info.get("status_code")
                if status and int(status) in (400, 401, 403, 404):
                    return True
                if "not_found" in str(info.get("error", "")).lower():
                    return True
    except Exception:
        pass
    exc_str = str(exc).lower()
    return "not_found" in exc_str or "object not found" in exc_str


def _prescan_pages(local_path: str) -> list[dict]:
    """Fast PyMuPDF pass: per-page image count + char count, before Docling.
    Cheap (~ms/page) and gives the UI an immediate workload map so a long parse
    is explainable in real time. Returns [] if PyMuPDF is unavailable."""
    import fitz  # PyMuPDF
    pages: list[dict] = []
    pdf = fitz.open(local_path)
    try:
        for i, page in enumerate(pdf):
            text = page.get_text() or ""
            pages.append({
                "page": i + 1,
                "images": len(page.get_images(full=True)),
                "chars": len(text),
                "est_words": len(text.split()),
            })
    finally:
        pdf.close()
    return pages


def _build_page_breakdown(parsed_doc) -> list[dict]:
    """Accurate per-page map from the Docling result: text blocks, tables and
    images on each page. Used by the parsing detail panel after parsing done."""
    from collections import defaultdict
    blocks = defaultdict(int)
    words = defaultdict(int)
    for b in getattr(parsed_doc, "text_blocks", []) or []:
        blocks[b.page_number] += 1
        words[b.page_number] += getattr(b, "token_count", 0) or len((b.text or "").split())
    tables = defaultdict(int)
    for t in getattr(parsed_doc, "tables", []) or []:
        tables[t.page_number] += 1
    images = defaultdict(int)
    for im in getattr(parsed_doc, "images", []) or []:
        images[im.page_number] += 1

    total = parsed_doc.page_count or (max([*blocks, *tables, *images], default=0))
    return [
        {
            "page": p,
            "blocks": blocks.get(p, 0),
            "tables": tables.get(p, 0),
            "images": images.get(p, 0),
            "est_words": words.get(p, 0),
        }
        for p in range(1, (total or 0) + 1)
    ]


def _build_graph_inputs(parsed_doc, document_id: str, filename: str, doc_type: str) -> tuple[dict, list]:
    """Pure helper: assemble the Document metadata + extracted entities for the
    multi-PDF graph. Entities come from the document's raw text (capped) via
    entity_service. Does not touch Neo4j — returns inputs for graph_service."""
    from app.services import entity_service

    doc_meta = {"doc_id": document_id, "filename": filename or "", "doc_type": doc_type or ""}
    raw_text = (getattr(parsed_doc, "raw_text", "") or "")[:20000]
    entities = entity_service.extract_entities(raw_text) if raw_text.strip() else []
    return doc_meta, entities


def _prepare_image_record(img, document_id: str, bucket: str, prefilter=None):
    """Phase 1 (sequential): pre-filter + OCR + upload for a SINGLE extracted image.

    Runs everything that must stay deterministic/sequential per document —
    in particular the ImagePrefilter's stateful perceptual-hash duplicate
    detection — and everything cheap (OCR, upload). Does NOT call the VLM.

    Returns a "pending" dict with all the fields needed to assemble the final
    record, plus ``needs_vlm`` / the (unbounded — bounding happens inside
    analyze_image) OCR text to pass to the VLM later. Returns None when the
    image must be skipped (e.g. its upload failed) — same contract as before.
    """
    from app.services.ocr_service import ocr_image
    from app.services.image_prefilter import ImagePrefilter, STATUS_OCR_ONLY

    if prefilter is None:
        prefilter = ImagePrefilter()

    # Step 1: pre-filter (OCR runs inside, only if Stage 1 passes). This is the
    # part that MUST run in strict per-document order — do not parallelize.
    decision, raw_ocr = prefilter.evaluate(img.png_bytes, lambda: ocr_image(img.png_bytes))

    # Step 2: upload PNG (every kept image — even skipped — for audit/display)
    path = f"images/{document_id}/{img.image_index}.png"
    try:
        upload_file(bucket, path, img.png_bytes, "image/png")
    except Exception as exc:
        logger.warning("[%s] image upload failed (img %d): %s", document_id, img.image_index, exc)
        return None

    bbox = None
    if img.bbox:
        bbox = {"x1": img.bbox.x1, "y1": img.bbox.y1, "x2": img.bbox.x2, "y2": img.bbox.y2}

    # OCR_ONLY images carry document text (no visual structure): route by content —
    # genuinely searchable OCR goes to vector_store (Task 3), otherwise it stays a
    # repository row. SKIPPED images (logos/icons/blanks/etc.) are non-informative
    # and always stay in image_store as pure repository rows.
    if decision.processing_status == STATUS_OCR_ONLY:
        from app.services.image_router import decide_route
        _route = decide_route(
            canonical_store="image_store",
            structured_content=raw_ocr, ocr_text=raw_ocr,
            confidence=0.0, vlm_succeeded=False, base_reason="OCR-only text label",
        )
        fallback_analysis = {
            "structured_content": raw_ocr, "vlm_ocr_text": "",
            "detected_store": _route.destination_store, "confidence": 0.0,
            "reason_for_store_selection": _route.reason,
            "content_type": _route.content_type,
            "content_class": _route.content_class,
            "extraction_quality": _route.extraction_quality,
        }
    else:
        fallback_analysis = {
            "structured_content": "", "vlm_ocr_text": "",
            "detected_store": "image_store", "confidence": 0.0,
            "reason_for_store_selection": decision.skip_reason or "pre-filtered (skipped)",
            "content_type": "figure",
            "content_class": "decorative",
            "extraction_quality": "low",
        }

    return {
        "img": img,
        "decision": decision,
        "raw_ocr": raw_ocr,
        "path": path,
        "bucket": bucket,
        "bbox": bbox,
        "needs_vlm": decision.run_vlm,
        "fallback_analysis": fallback_analysis,
    }


def _finalize_image_record(pending: dict, analysis: dict) -> tuple[dict, str]:
    """Phase 2/assembly: merge a (possibly VLM-derived) analysis dict into the
    final record shape — unchanged from the original single-phase contract."""
    img = pending["img"]
    decision = pending["decision"]
    raw_ocr = pending["raw_ocr"]

    structured_content = analysis["structured_content"]
    record = {
        "image_index": img.image_index,
        "page_number": img.page_number,
        "bbox": pending["bbox"],
        "storage_path": pending["path"],
        "storage_bucket": pending["bucket"],
        "mime_type": "image/png",
        "width": img.width,
        "height": img.height,
        "ocr_text": raw_ocr,
        "vlm_ocr_text": analysis.get("vlm_ocr_text", ""),
        "structured_content": structured_content,
        "content_type": analysis["content_type"],
        "detected_store": analysis["detected_store"],
        "image_metadata": {
            "confidence": analysis["confidence"],
            "reason_for_store_selection": analysis["reason_for_store_selection"],
            "content_class": analysis.get("content_class"),
            "extraction_quality": analysis.get("extraction_quality"),
            "prefilter": decision.features,
        },
        # ── pre-filter tracking columns ──
        "processing_status": decision.processing_status,
        "skip_reason": decision.skip_reason,
        "filter_stage": decision.filter_stage,
        "image_type": decision.image_type,
    }
    embed_text = (
        (structured_content or raw_ocr or f"image page {img.page_number}").strip()
        or f"image page {img.page_number}"
    )
    return record, embed_text


def _run_vlm_for_pending(document_id: str, index: int, pending: dict) -> dict:
    """Call analyze_image for one pending image, with the same fail-open
    fallback behaviour as before. Safe to run on a worker thread."""
    from app.services.image_analysis_service import analyze_image

    img = pending["img"]
    try:
        return analyze_image(img.png_bytes, pending["raw_ocr"])
    except Exception as exc:
        logger.warning("[%s] analyze_image failed (img %d): %s", document_id, img.image_index, exc)
        # Kept image, VLM crashed — route by content: searchable OCR -> vector_store,
        # otherwise keep it as a repository asset (never orphan informative content,
        # never force noise into vector_store). Consistent with analyze_image's own
        # content-driven fallback.
        from app.services.image_router import decide_route
        _r = decide_route(
            canonical_store="image_store",
            structured_content=pending["raw_ocr"], ocr_text=pending["raw_ocr"],
            confidence=0.0, vlm_succeeded=False, base_reason="VLM analyze failed",
        )
        return {
            "structured_content": pending["raw_ocr"], "vlm_ocr_text": "",
            "detected_store": _r.destination_store, "confidence": 0.0,
            "reason_for_store_selection": _r.reason, "content_type": _r.content_type,
            "content_class": _r.content_class, "extraction_quality": _r.extraction_quality,
        }


def _build_one_image_record(img, document_id: str, bucket: str, prefilter=None):
    """Pre-filter + (conditional) OCR/VLM + upload for a SINGLE extracted image.

    Returns (record, embed_text) or None when the image must be skipped
    (e.g. its upload failed). Per-image failures are non-fatal.

    Kept as a sequential single-image helper (used directly by tests and any
    caller that wants strict per-image processing). Internally this now
    composes _prepare_image_record (Phase 1) + the VLM call + _finalize_image_record
    (assembly) — the two-phase split used for parallelism in
    _build_image_records_parallel below.
    """
    pending = _prepare_image_record(img, document_id, bucket, prefilter)
    if pending is None:
        return None

    analysis = (
        _run_vlm_for_pending(document_id, img.image_index, pending)
        if pending["needs_vlm"] else pending["fallback_analysis"]
    )
    return _finalize_image_record(pending, analysis)


def _build_image_records(parsed_doc, document_id: str, bucket: str) -> tuple[list, list]:
    """Batch variant: pre-filter + (conditional) OCR/VLM + upload every image.
    Returns (records, embed_texts). Thin wrapper over _build_one_image_record
    (kept for callers that store in one shot)."""
    from app.services.image_prefilter import ImagePrefilter
    prefilter = ImagePrefilter()   # one per doc (duplicate detection state)
    records: list = []
    embed_texts: list = []
    for img in parsed_doc.images:
        res = _build_one_image_record(img, document_id, bucket, prefilter)
        if res is None:
            continue
        rec, txt = res
        records.append(rec)
        embed_texts.append(txt)
    return records, embed_texts


def _build_image_records_parallel(
    parsed_doc, document_id: str, bucket: str, on_record=None, max_workers: int = 1,
) -> tuple[list, list]:
    """Two-phase image-record builder used by the ingestion pipeline.

    Phase 1 (sequential): iterate images in order, run the pre-filter (and its
    OCR) exactly as today so the ImagePrefilter's stateful duplicate-detection
    (perceptual aHash set) is updated deterministically. Images whose upload
    fails are dropped here, same as before.

    Phase 2 (bounded parallel): for the images that need the VLM, call
    ``analyze_image`` concurrently via a ThreadPoolExecutor with at most
    ``max_workers`` in flight. Images that don't need the VLM use their
    fallback analysis immediately — no thread pool involvement.

    ``on_record`` (optional) is invoked once per finished record — for VLM
    images this fires in VLM-completion order (as_completed), NOT necessarily
    original image order, so callers relying on it for live progress must
    treat it as a count/percentage signal only, not a strict per-index stream.

    With max_workers <= 1 this executes the VLM calls sequentially in
    original image order, which is behaviourally identical to the pre-refactor
    single-loop implementation.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.image_prefilter import ImagePrefilter

    prefilter = ImagePrefilter()   # one per doc (duplicate detection state) — sequential only

    # ── Phase 1: sequential pre-filter + OCR + upload ──
    pendings: list = [None] * len(parsed_doc.images)
    for i, img in enumerate(parsed_doc.images):
        pendings[i] = _prepare_image_record(img, document_id, bucket, prefilter)

    # ── Phase 2: VLM calls, bounded concurrency for images that need it ──
    results: list = [None] * len(parsed_doc.images)   # (record, embed_text) or None, by original index

    def _finish(i: int, pending: dict, analysis: dict) -> None:
        results[i] = _finalize_image_record(pending, analysis)
        if on_record is not None:
            on_record(i, results[i])

    vlm_indices = [i for i, p in enumerate(pendings) if p is not None and p["needs_vlm"]]

    if max_workers is None or max_workers <= 1:
        # Sequential path — identical order/behaviour to the original loop.
        for i, pending in enumerate(pendings):
            if pending is None:
                continue
            analysis = (
                _run_vlm_for_pending(document_id, i, pending)
                if pending["needs_vlm"] else pending["fallback_analysis"]
            )
            _finish(i, pending, analysis)
    else:
        # Non-VLM images resolve immediately (cheap, no need for a thread).
        for i, pending in enumerate(pendings):
            if pending is not None and not pending["needs_vlm"]:
                _finish(i, pending, pending["fallback_analysis"])

        if vlm_indices:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(_run_vlm_for_pending, document_id, i, pendings[i]): i
                    for i in vlm_indices
                }
                for future in as_completed(future_to_index):
                    i = future_to_index[future]
                    analysis = future.result()
                    _finish(i, pendings[i], analysis)

    records: list = []
    embed_texts: list = []
    for res in results:
        if res is None:
            continue
        rec, txt = res
        records.append(rec)
        embed_texts.append(txt)
    return records, embed_texts


def _get_current_stage(job_id: str) -> str:
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_stage FROM multi_store_rag_working.ingestion_jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
                return row[0] if row else "unknown"
    except Exception:
        return "unknown"
