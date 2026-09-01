"""
Storage service — routes content to Supabase stores by *content type*.

Text routes by document nature:
  policy / entity / financial → vector_store
  legal                       → clause_store  (with Groq-enriched metadata)

Content streams routed independent of document_type:
  tables → table_store   (ANY document with tables — embedded + searchable)
  images → image_store   (written earlier in stage 1b; not handled here)
"""
import csv
import io
import json
import logging

import numpy as np
import psycopg2.extras

from app.config import settings
from app.db.connection import get_db
from app.models.document import Chunk, LegalClause, ParsedDocument, TableChunk

logger = logging.getLogger(__name__)


def _clear_existing_chunks(document_id: str) -> None:
    """Delete all stored text/table data for this document before re-inserting on
    reprocess. image_store is intentionally excluded: images are written in a
    separate, earlier pipeline stage (1b) and own their own idempotency via
    store_images(); clearing image_store here would wipe the images that stage
    already inserted for this same document during this run."""
    # Ordered so FK children are deleted before parents (table_chunk_store
    # references table_store).  Only tables that exist are deleted — guards
    # against migration 008 (table_chunk_store) not yet applied.
    _CLEAR_TABLES = (
        "table_chunk_store", "vector_store", "table_store",
        "clause_store", "document_store",
    )
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'multi_store_rag_working'
                  AND table_name = ANY(%s)
                """,
                (list(_CLEAR_TABLES),),
            )
            existing = {row[0] for row in cur.fetchall()}
            for table in _CLEAR_TABLES:
                if table not in existing:
                    logger.warning(
                        "Skipping clear for %s — table not found "
                        "(migration may be pending).", table
                    )
                    continue
                cur.execute(
                    f"DELETE FROM multi_store_rag_working.{table} WHERE document_id = %s",
                    (document_id,),
                )


def store_chunks(
    document_id: str,
    document_type: str,
    chunks: list[Chunk],
    embeddings: np.ndarray,
    parsed_doc: ParsedDocument,
    legal_clauses: list[LegalClause] | None = None,
    clause_embeddings: np.ndarray | None = None,
    table_image_paths: dict | None = None,
    table_embeddings: np.ndarray | None = None,
    table_source_image_ids: dict | None = None,
    table_extraction: dict | None = None,
    table_enrichment: dict | None = None,
    table_structured_content: dict | None = None,
    table_sc_embeddings: np.ndarray | None = None,
) -> dict[str, list[str]]:
    """Route content to stores by *content type*, not by document_type alone.

    Text routes by the document's nature (legal→clause_store, everything else→
    vector_store). Tables are a universal content stream: ANY document that contains
    tables stores them in table_store, regardless of its document_type. Images are
    handled earlier (stage 1b → image_store) and are not touched here.

    table_source_image_ids / table_extraction (migration 014, Slice 2a): optional
    dicts keyed by table_index carrying write-time lineage — the image_store crop
    UUID and the extraction method/confidence/provenance for each Docling table.
    Both are fail-open: a missing table_index simply yields NULL/'pdf_grid' for
    that row, never a crash.

    table_enrichment (Slice 3): optional dict keyed by table_index carrying the
    6 metadata columns (fiscal_year, reporting_period, currency, table_category,
    detected_units, table_summary) produced by table_enrichment.enrich_table().
    Fail-open: a missing table_index causes _store_tables to derive it on the
    fly (via enrich_table) rather than write NULLs.

    Returns a dict mapping store name → list of inserted Postgres UUID strings
    (only stores that were actually written to are included).
    """
    _clear_existing_chunks(document_id)

    stored_ids: dict[str, list[str]] = {}

    # ── TEXT — routed by document nature ──────────────────────────────
    if legal_clauses and clause_embeddings is not None and len(clause_embeddings) > 0:
        ids = _store_clauses(document_id, legal_clauses, clause_embeddings, parsed_doc)
        if ids:
            stored_ids["clause_store"] = ids

    if chunks and len(embeddings) > 0:
        ids = _store_vector_chunks(document_id, chunks, embeddings)
        if ids:
            stored_ids["vector_store"] = ids

    # ── TABLES — universal content stream (any document type) ─────────
    if parsed_doc.tables:
        ids = _store_tables(
            document_id, parsed_doc, table_image_paths, table_embeddings,
            table_source_image_ids=table_source_image_ids,
            table_extraction=table_extraction,
            table_enrichment=table_enrichment,
            table_structured_content=table_structured_content,
            table_sc_embeddings=table_sc_embeddings,
        )
        if ids:
            stored_ids["table_store"] = ids

    return stored_ids


def _store_vector_chunks(
    document_id: str,
    chunks: list[Chunk],
    embeddings: np.ndarray,
) -> list[str]:
    if not chunks:
        return []

    rows = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        bbox_json = None
        if chunk.bbox:
            bbox_json = json.dumps({
                "x1": chunk.bbox.x1, "y1": chunk.bbox.y1,
                "x2": chunk.bbox.x2, "y2": chunk.bbox.y2,
            })
        rows.append((
            document_id,
            chunk.chunk_index,
            chunk.chunk_text,
            len(chunk.chunk_text.split()),
            len(chunk.chunk_text),
            chunk.page_number,
            chunk.page_numbers,
            bbox_json,
            chunk.section_title,
            chunk.section_level,
            chunk.semantic_type,
            chunk.keywords,
            emb.tolist(),
            json.dumps(chunk.chunk_metadata),
        ))

    with get_db() as conn:
        with conn.cursor() as cur:
            result = psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO multi_store_rag_working.vector_store
                    (document_id, chunk_index, chunk_text, chunk_word_count, chunk_char_count,
                     page_number, page_numbers, bbox, section_title, section_level,
                     semantic_type, keywords, embedding, chunk_metadata)
                VALUES %s
                RETURNING id
                """,
                rows,
                template="""(
                    %s, %s, %s, %s, %s, %s, %s::int[], %s::jsonb, %s, %s, %s, %s::text[], %s::vector, %s::jsonb
                )""",
                page_size=500,
                fetch=True,
            )
    ids = [str(r[0]) for r in result]
    logger.info("Stored %d vector chunks for document %s", len(rows), document_id)
    return ids


def _extraction_quality_bucket(confidence: float | None) -> str | None:
    """Map a 0.0-1.0 confidence score to a coarse quality bucket.

    Returns None when no confidence score is available — extraction_quality is
    left NULL rather than guessed (migration 014)."""
    if confidence is None:
        return None
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _store_tables(
    document_id: str,
    parsed_doc: ParsedDocument,
    image_paths: dict | None = None,
    table_embeddings: np.ndarray | None = None,
    table_source_image_ids: dict | None = None,
    table_extraction: dict | None = None,
    table_enrichment: dict | None = None,
    table_structured_content: dict | None = None,
    table_sc_embeddings: np.ndarray | None = None,
) -> list[str]:
    image_paths = image_paths or {}
    table_source_image_ids = table_source_image_ids or {}
    table_extraction = table_extraction or {}
    table_enrichment = table_enrichment or {}
    table_structured_content = table_structured_content or {}
    # Embeddings are positional, aligned with parsed_doc.tables order (same order
    # the orchestrator embeds them in). Missing/short → NULL embedding (defensive).
    # Embeddings are written for ALL document types (not financial-only).
    have_embeddings = table_embeddings is not None and len(table_embeddings) >= len(parsed_doc.tables)
    # structured_content_embedding (universal VLM pipeline): positional too.
    have_sc_embeddings = table_sc_embeddings is not None and len(table_sc_embeddings) >= len(parsed_doc.tables)
    rows = []
    for i, table in enumerate(parsed_doc.tables):
        json_data = json.dumps({"headers": table.headers, "rows": table.rows})
        csv_data = _to_csv(table.headers, table.rows)
        bbox_json = None
        if table.bbox:
            bbox_json = json.dumps({
                "x1": table.bbox.x1, "y1": table.bbox.y1,
                "x2": table.bbox.x2, "y2": table.bbox.y2,
            })

        has_currency = any(
            any(c in cell for c in ["$", "€", "£", "₹", "USD", "EUR", "INR"])
            for row in table.rows for cell in row
        )
        has_percentages = any("%" in cell for row in table.rows for cell in row)
        has_numeric = any(
            _is_numeric(cell) for row in table.rows for cell in row
        )

        embedding = table_embeddings[i].tolist() if have_embeddings else None
        # structured_content: the VLM's clean, retrieval-ready extraction (keyed
        # by table_index), falling back to markdown_text/raw_text so the column
        # is populated for every table — but only for SMALL tables (migration
        # 018). A big table (row_count > TABLE_CHUNK_MAX_ROWS) already gets this
        # same content sliced per-window in table_chunk_store with its own
        # embedding; duplicating the whole-table blob (and one diluted vector
        # covering all 200+ rows) here would be redundant, so it's left NULL.
        is_big_table = len(table.rows) > settings.TABLE_CHUNK_MAX_ROWS
        structured_content = None
        sc_embedding = None
        if not is_big_table:
            structured_content = (
                table_structured_content.get(table.table_index)
                or table.markdown_text or table.raw_text or ""
            )
            sc_embedding = table_sc_embeddings[i].tolist() if have_sc_embeddings else None

        # ── Write-time lineage (migration 014, Slice 2a) — fail-open: a missing
        # table_index entry yields NULL source_image_id / 'pdf_grid' method / no
        # confidence, never a crash.
        #
        # _store_tables() never sets from_image_store (it stays at its column
        # default, FALSE — that flag is reserved for rows written by the
        # image-cross-store pathway in store_router.py). Keep source_image_id
        # consistent with that: only a row whose content genuinely came from
        # the image pipeline (from_image_store=TRUE) should carry a link back
        # to it, so it is always NULL here regardless of whether a crop image
        # exists for this table. ──────────────────────────────────────────────
        source_image_id = None
        extraction_info = table_extraction.get(table.table_index) or {}
        extraction_method = extraction_info.get("method") or "pdf_grid"
        source_confidence = extraction_info.get("confidence")
        # Slice 2b: reconcile_table computes an explicit quality verdict (e.g.
        # 'low' on a failed faithfulness gate even though confidence is None
        # because Docling — not the VLM — is canonical). Prefer that explicit
        # verdict over the confidence-bucket heuristic; fall back to bucketing
        # when the caller didn't provide one (e.g. no crop/VLM ran at all).
        extraction_quality = extraction_info.get("quality") or _extraction_quality_bucket(source_confidence)
        provenance = extraction_info.get("provenance") or {}

        # ── Uniform metadata enrichment (Slice 3) — populate fiscal_year,
        # reporting_period, currency, table_category, detected_units,
        # table_summary for EVERY table, not just image-derived ones. Prefer
        # the caller-supplied enrichment dict (built once by the orchestrator,
        # reusing VLM meta already produced during crop reconstruction — no
        # new LLM call); fall back to deriving it here so a direct call to
        # _store_tables() (e.g. from a test, or a caller that skipped the
        # orchestrator step) still gets fully-populated rows. Fail-open: any
        # error yields an all-None dict with a best-effort table_summary. ──
        enrichment = table_enrichment.get(table.table_index)
        if enrichment is None:
            from app.services.table_enrichment import enrich_table
            enrichment = enrich_table(table.headers, table.rows, table.caption)

        # Merged-cell span metadata (document_parser._detect_merged_cells) and
        # multi-page continuation metadata (document_parser._merge_continued_tables),
        # preserved as-is in the pre-existing table_metadata JSONB column —
        # additive, no migration needed. Nothing downstream reads the merged-cell
        # part yet; it's forward-compatible capture only (deferred typed span model).
        table_metadata_dict = dict(getattr(table, "table_metadata", None) or {})
        row_page_numbers = getattr(table, "row_page_numbers", None)
        if row_page_numbers is not None:
            table_metadata_dict["row_page_numbers"] = row_page_numbers
        table_metadata_json = json.dumps(table_metadata_dict)

        rows.append((
            document_id,
            table.table_index,
            table.caption,
            table.page_number,
            bbox_json,
            table.raw_text,
            table.markdown_text,
            json_data,
            csv_data,
            len(table.rows),
            len(table.headers),
            has_numeric,
            has_currency,
            has_percentages,
            image_paths.get(table.table_index),
            embedding,
            source_image_id,
            extraction_method,
            extraction_quality,
            source_confidence,
            json.dumps(provenance),
            enrichment.get("fiscal_year"),
            enrichment.get("reporting_period"),
            enrichment.get("currency"),
            enrichment.get("table_category"),
            enrichment.get("detected_units"),
            enrichment.get("table_summary"),
            table_metadata_json,
            structured_content,
            sc_embedding,
        ))

    with get_db() as conn:
        with conn.cursor() as cur:
            result = psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO multi_store_rag_working.table_store
                    (document_id, table_index, table_title, page_number, bbox,
                     raw_text, markdown_text, json_data, csv_data,
                     row_count, col_count, has_numeric_data, has_currency, has_percentages,
                     image_storage_path, embedding,
                     source_image_id, extraction_method, extraction_quality,
                     source_confidence, provenance,
                     fiscal_year, reporting_period, currency, table_category,
                     detected_units, table_summary, table_metadata,
                     structured_content, structured_content_embedding)
                VALUES %s
                RETURNING id
                """,
                rows,
                template=(
                    "(%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::vector,"
                    " %s::uuid, %s, %s, %s, %s::jsonb,"
                    " %s, %s, %s, %s, %s::text[], %s, %s::jsonb,"
                    " %s, %s::vector)"
                ),
                page_size=200,
                fetch=True,
            )
    ids = [str(r[0]) for r in result]
    logger.info("Stored %d tables for document %s", len(rows), document_id)
    return ids


def _store_clauses(
    document_id: str,
    clauses: list[LegalClause],
    embeddings: np.ndarray,
    parsed_doc: ParsedDocument,
) -> list[str]:
    rows = []
    for clause, emb in zip(clauses, embeddings):
        rows.append((
            document_id,
            clause.clause_index,
            clause.clause_number,
            clause.clause_title,
            clause.clause_text,
            len(clause.clause_text.split()),
            clause.clause_type,
            clause.risk_level,
            clause.risk_rationale,
            clause.obligor,
            clause.obligee,
            clause.parties_mentioned or [],
            json.dumps(clause.key_dates) if clause.key_dates else None,
            json.dumps(clause.monetary_values) if clause.monetary_values else None,
            clause.page_number,
            clause.page_numbers,
            clause.section_path,
            emb.tolist(),
            json.dumps(clause.clause_metadata),
        ))

    with get_db() as conn:
        with conn.cursor() as cur:
            result = psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO multi_store_rag_working.clause_store
                    (document_id, clause_index, clause_number, clause_title,
                     clause_text, clause_word_count, clause_type,
                     risk_level, risk_rationale,
                     obligor, obligee, parties_mentioned,
                     key_dates, monetary_values,
                     page_number, page_numbers, section_path,
                     embedding, clause_metadata)
                VALUES %s
                RETURNING id
                """,
                rows,
                template=(
                    "(%s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s,"
                    " %s, %s, %s::text[],"
                    " %s::jsonb, %s::jsonb,"
                    " %s, %s::int[], %s::text[],"
                    " %s::vector, %s::jsonb)"
                ),
                page_size=200,
                fetch=True,
            )
    ids = [str(r[0]) for r in result]
    logger.info("Stored %d clauses for document %s", len(rows), document_id)
    return ids


def _table_image_path(document_id: str, table_index: int) -> str:
    return f"tables/{document_id}/{table_index}.png"


def _image_rows(document_id: str, records: list[dict]) -> list[tuple]:
    """Build image_store rows. No embedding — image_store is a pure extraction
    repository; semantic embeddings live only in the destination stores
    (see store_image_derived_chunks + migration 008)."""
    rows = []
    for rec in records:
        bbox_json = json.dumps(rec["bbox"]) if rec.get("bbox") else None
        rows.append((
            document_id,                                    # document_id
            rec["image_index"],                             # image_index
            rec.get("page_number"),                         # page_number
            bbox_json,                                      # bbox (::jsonb)
            rec["storage_path"],                            # storage_path
            rec["storage_bucket"],                          # storage_bucket
            rec.get("mime_type", "image/png"),              # mime_type
            rec.get("width"),                               # width
            rec.get("height"),                              # height
            rec.get("ocr_text"),                            # ocr_text (raw OCR, plain TEXT)
            rec.get("vlm_ocr_text"),                         # vlm_ocr_text (VLM transcription, plain TEXT)
            rec.get("structured_content"),                  # structured_content (plain TEXT)
            json.dumps(rec.get("image_metadata", {})),      # image_metadata (::jsonb)
            rec.get("content_type", "figure"),              # content_type
            rec.get("detected_store", "image_store"),       # detected_store (plain TEXT)
            # stored_in = where this image's content physically lives. Every image is
            # in image_store, so it STARTS there (never NULL). If its content is later
            # copied to a destination store, store_image_derived_chunks flips this to
            # that store. Skipped/decorative images therefore keep stored_in='image_store'.
            "image_store",                                  # stored_in
            rec.get("processing_status", "VLM_PROCESSED"),  # processing_status
            rec.get("skip_reason"),                         # skip_reason
            rec.get("filter_stage"),                        # filter_stage
            rec.get("image_type"),                          # image_type
            "figure",                                       # asset_role (migration 014)
        ))
    return rows


def delete_document_images(document_id: str) -> None:
    """Delete all image files from the storage bucket AND clear image_store rows.

    Called at the start of every ingest run so a reprocessed document starts
    with a clean slate. The original PDF is stored separately and is NOT touched.
    """
    from app.services.supabase_storage import delete_files

    # Fetch existing storage paths before clearing DB rows
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT storage_path, storage_bucket FROM multi_store_rag_working.image_store WHERE document_id = %s",
                (document_id,),
            )
            rows = cur.fetchall()

    if rows:
        by_bucket: dict[str, list[str]] = {}
        for path, bucket in rows:
            if path and bucket:
                by_bucket.setdefault(bucket, []).append(path)
        for bucket, paths in by_bucket.items():
            try:
                delete_files(bucket, paths)
                logger.info("Deleted %d image file(s) from bucket for document %s", len(paths), document_id)
            except Exception as exc:
                logger.warning("Failed to delete image bucket files for document %s: %s", document_id, exc)

    # Clear DB rows (even if bucket deletion partially failed, avoid stale metadata)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM multi_store_rag_working.image_store WHERE document_id = %s",
                (document_id,),
            )


def _clear_existing_images(document_id: str) -> None:
    """Delete prior image_store rows for this document so re-ingest stays idempotent.

    Bucket files are already deleted by delete_document_images() at pipeline start;
    this is kept for within-run retry safety (no bucket files to remove at this point).
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM multi_store_rag_working.image_store WHERE document_id = %s",
                (document_id,),
            )


def store_images(document_id: str, records: list[dict]) -> None:
    if not records:
        return
    from app.db.repositories.image_store import insert_images
    _clear_existing_images(document_id)
    n = insert_images(_image_rows(document_id, records))
    logger.info("Stored %d images for document %s", n, document_id)


def clear_document_images(document_id: str) -> None:
    """Public wrapper around the idempotent image clear — call ONCE before
    incremental per-image storage so each appended row survives."""
    _clear_existing_images(document_id)


def append_images(document_id: str, records: list[dict]) -> int:
    """Insert image rows WITHOUT clearing existing rows first.

    Used by the incremental images stage: each figure is OCR'd + analysed +
    stored one at a time so the UI can render it the moment it lands, instead of
    waiting for the whole batch. No embedding is computed here — image_store is a
    pure repository; the destination-store embedding is generated later in
    store_image_derived_chunks. Caller must call clear_document_images() once at
    the start of the stage."""
    if not records:
        return 0
    from app.db.repositories.image_store import insert_images
    return insert_images(_image_rows(document_id, records))


def store_image_derived_chunks(document_id: str) -> None:
    """Cross-store step run after store_chunks().

    For every image_store row whose detected_store differs from 'image_store', this
    function dispatches to a registry-driven handler (store_router.STORE_REGISTRY) that
    writes the extracted content into the appropriate destination store and then updates
    image_store.stored_in so callers can discover where the data lives.  Rows with
    detected_store='image_store' (figures) are left as-is; rows whose detected_store has
    no registered handler are also left unchanged and counted as skipped.

    Must run AFTER store_chunks() so the regular-chunk clear does not wipe the rows this
    function inserts.

    Idempotency (reprocess safety): before the per-image loop, any prior image-derived
    rows (identified by the 50 000-base index offset) are deleted from all destination
    stores so that plain INSERTs in the handlers do not duplicate rows.

    Per-image atomicity: the cross-store INSERT, the validate() call, and the stored_in
    UPDATE all share a single get_db() context so they commit together or roll back
    together.  One image's failure is caught, logged, and the loop continues so the rest
    of the batch is unaffected.  stored_in therefore always reflects reality: it starts
    NULL at extraction and is set to detected_store ONLY after the destination row is
    inserted + validated; on skip/failure it stays NULL (no destination row exists).
    """
    from app.services.store_router import ImageCtx, get_handler  # local import — avoids circular deps

    # ── SELECT all images that need cross-store routing ──────────────────────────
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, image_index, page_number, bbox,
                          storage_path, ocr_text, vlm_ocr_text,
                          structured_content, detected_store, image_metadata
                   FROM multi_store_rag_working.image_store
                   WHERE document_id = %s
                     AND detected_store <> 'image_store'
                     AND image_index < %s
                   ORDER BY image_index""",
                (document_id, _TABLE_CROP_IMAGE_STORE_OFFSET),
            )
            rows = cur.fetchall()

    if not rows:
        return

    # ── Idempotency: delete prior image-derived rows from all destination stores ─
    # Image-derived rows use index >= 50 000 (the _IMAGE_*_INDEX_OFFSET).
    _OFFSET = 50_000
    with get_db() as conn:
        with conn.cursor() as cur:
            # vector_store, clause_store, and table_store
            for tbl, col in (
                ("vector_store", "chunk_index"),
                ("clause_store", "clause_index"),
                ("table_store", "table_index"),
            ):
                cur.execute(
                    f"DELETE FROM multi_store_rag_working.{tbl}"
                    f" WHERE document_id = %s AND {col} >= %s",
                    (document_id, _OFFSET),
                )

    succeeded = 0
    skipped = 0
    failed = 0

    # ── Phase 1: parse every row into a canonical text, building an ordered
    #    work-list. Rows that fail to produce canonical text are skipped here
    #    and never get a work-list slot, so they cannot misalign the batch
    #    embedding call in Phase 2. ─────────────────────────────────────────
    worklist: list[dict] = []

    for (
        img_id, img_idx, page_num, bbox_raw,
        storage_path, ocr_text, vlm_ocr_text,
        structured_content, detected_store, image_metadata,
    ) in rows:
        # psycopg2 returns JSONB columns as Python dicts; serialise back for handlers
        bbox_json = json.dumps(bbox_raw) if isinstance(bbox_raw, dict) else bbox_raw

        # ── Resolve handler from registry — skip if none registered ──────────
        handler = get_handler(detected_store)
        if handler is None:
            logger.debug(
                "image %s (doc %s): no handler for detected_store=%s — skipping",
                img_idx, document_id, detected_store,
            )
            skipped += 1
            continue

        # ── Pull confidence/reason from image_metadata JSONB if present ──────
        meta = image_metadata if isinstance(image_metadata, dict) else {}
        confidence = float(meta.get("confidence") or 0.0)
        reason = str(meta.get("reason") or "")

        ctx = ImageCtx(
            document_id=document_id,
            image_id=str(img_id),
            image_index=img_idx,
            page_number=page_num,
            bbox_json=bbox_json,
            storage_path=storage_path,
            ocr_text=ocr_text,
            vlm_ocr_text=vlm_ocr_text,
            detected_store=detected_store,
            confidence=confidence,
            reason=reason,
            structured_content=structured_content,
        )

        try:
            parsed = handler.parse(structured_content or "", ctx)

            # ── Derive canonical text for destination embedding ───────────
            canonical = handler.canonical_text(parsed, ctx)
            if not canonical or not canonical.strip():
                # Fall back to raw structured_content then ocr_text
                canonical = (structured_content or ocr_text or "").strip()
        except Exception as exc:
            logger.error(
                "image %s (doc %s): failed to parse content for %s — skipping: %s",
                img_idx, document_id, detected_store, exc,
            )
            skipped += 1
            continue

        if not canonical:
            logger.debug(
                "image %s (doc %s): no canonical text for %s — skipping",
                img_idx, document_id, detected_store,
            )
            skipped += 1
            continue

        worklist.append({
            "img_id": img_id,
            "img_idx": img_idx,
            "detected_store": detected_store,
            "handler": handler,
            "parsed": parsed,
            "canonical": canonical,
            "ctx": ctx,
        })

    if not worklist:
        logger.info(
            "store_image_derived_chunks: doc %s — %d succeeded, %d skipped, %d failed",
            document_id, succeeded, skipped, failed,
        )
        return

    # ── Phase 2: ONE batched embedding call for every canonical text, in the
    #    same order as worklist so indices stay aligned (embed_passages
    #    returns one vector per input, in order). ──────────────────────────
    from app.services.embedding_service import embed_passages  # noqa: PLC0415
    canonical_texts = [entry["canonical"] for entry in worklist]
    embeddings = embed_passages(canonical_texts)
    assert len(embeddings) == len(worklist), (
        f"embed_passages returned {len(embeddings)} vectors for "
        f"{len(worklist)} work-list entries — alignment broken"
    )

    # ── Phase 3: per-image atomic write (INSERT + validate + stored_in flip),
    #    exactly as before — one bad insert doesn't abort the rest. ────────
    for entry, embedding_vec in zip(worklist, embeddings):
        img_id = entry["img_id"]
        img_idx = entry["img_idx"]
        detected_store = entry["detected_store"]
        handler = entry["handler"]
        parsed = entry["parsed"]
        ctx = entry["ctx"]
        embedding = embedding_vec.tolist()

        try:
            with get_db() as conn:
                n = handler.insert(conn, parsed, embedding, ctx)
                if n < 1:
                    raise RuntimeError(
                        f"insert affected 0 rows for image {img_idx} -> {detected_store}"
                    )
                handler.validate(conn, ctx)
                _update_image_stored_in_conn(conn, img_id, detected_store)
            logger.debug("image %s stored in %s", img_idx, detected_store)
            succeeded += 1
        except Exception as exc:
            # stored_in stays NULL (the get_db() context rolled back the
            # partially-executed INSERT + validate + UPDATE). Log and continue.
            logger.error(
                "image %s (doc %s) cross-store write to %s failed — stored_in unchanged: %s",
                img_idx, document_id, detected_store, exc,
            )
            failed += 1

    logger.info(
        "store_image_derived_chunks: doc %s — %d succeeded, %d skipped, %d failed",
        document_id, succeeded, skipped, failed,
    )


def store_table_crop_images(document_id: str, records: list[dict], embeddings) -> dict[int, str]:
    """Register table crop images in image_store and return their new UUIDs.

    Each record dict must contain: table_index, page_number, bbox (dict|None),
    storage_path, storage_bucket, caption (str|None), ocr_text (str|None).

    A table crop is a VISUAL MIRROR of a table that the normal table pipeline
    (_store_tables) extracts into table_store. It is NOT routed through the
    VLM/store_router flow, so:
      - its image_store row carries stored_in = 'table_store' (the table's
        searchable data lives in table_store, produced from this same crop).
      - its image_store row has no embedding (image_store is a pure repository).

    Slice 2a (migration 014, write-time lineage): this function is now called
    BEFORE _store_tables so the crop's image_store UUID exists in time to be
    threaded into the table_store row as source_image_id. Returns a dict mapping
    table_index -> image_store UUID (str) for every crop successfully inserted,
    so the caller (ingestion_orchestrator) can build table_source_image_ids and
    pass it into store_chunks()/_store_tables().
    """
    if not records:
        return {}

    # Idempotency: clear any prior table-crop rows for this document
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM multi_store_rag_working.image_store
                   WHERE document_id = %s AND image_index >= %s""",
                (document_id, _TABLE_CROP_IMAGE_STORE_OFFSET),
            )

    rec_emb = list(zip(records, embeddings))
    img_rows = []
    for rec, _emb in rec_emb:
        bbox_json = json.dumps(rec["bbox"]) if isinstance(rec.get("bbox"), dict) else rec.get("bbox")
        ocr_text = rec.get("ocr_text")
        # When the table crop was reconstructed by the VLM (Task 1) the orchestrator
        # passes vlm_ocr_text / structured_content / processing_status on the record;
        # otherwise fall back to the OCR mirror + OCR_ONLY (pre-VLM behaviour).
        vlm_ocr_text = rec.get("vlm_ocr_text") or ocr_text
        structured_content = rec.get("structured_content") or rec.get("ocr_text") or rec.get("caption") or ""
        processing_status = rec.get("processing_status") or "OCR_ONLY"
        img_rows.append((
            document_id,                                                           # document_id
            _TABLE_CROP_IMAGE_STORE_OFFSET + rec["table_index"],                   # image_index
            rec.get("page_number"),                                                # page_number
            bbox_json,                                                             # bbox (::jsonb)
            rec["storage_path"],                                                   # storage_path
            rec["storage_bucket"],                                                 # storage_bucket
            "image/png",                                                           # mime_type
            None,                                                                  # width
            None,                                                                  # height
            ocr_text,                                                              # ocr_text (raw)
            vlm_ocr_text,                                                          # vlm_ocr_text (VLM transcription if run, else OCR mirror)
            structured_content,                                                    # structured_content
            json.dumps({"source": "table_crop", "table_index": rec["table_index"]}),  # image_metadata (::jsonb)
            "table",                                                               # content_type
            "table_store",                                                         # detected_store
            # The table's structured data lives in table_store (written by _store_tables),
            # so the crop's content is represented there — stored_in = table_store (not NULL).
            "table_store",                                                         # stored_in
            processing_status,                                                     # processing_status
            None,                                                                  # skip_reason
            "table_crop",                                                          # filter_stage
            "table",                                                               # image_type
            "table_crop",                                                          # asset_role (migration 014)
        ))

    from app.db.repositories.image_store import insert_images
    n = insert_images(img_rows)

    # Map table_index -> the crop's image_store row id so the table_store write can
    # record source_image_id (traceability back to the originating crop image).
    crop_image_id: dict[int, str] = {}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT image_index, id::text FROM multi_store_rag_working.image_store
                   WHERE document_id = %s AND image_index >= %s""",
                (document_id, _TABLE_CROP_IMAGE_STORE_OFFSET),
            )
            for img_idx, iid in cur.fetchall():
                crop_image_id[img_idx - _TABLE_CROP_IMAGE_STORE_OFFSET] = iid

    logger.info("Stored %d table crop images in image_store for document %s", n, document_id)
    return crop_image_id


# ── index offsets — keep image-derived rows from colliding with regular indices ──
# image_store.image_index: regular images use 0–N; table crop images use 20_000+
_TABLE_CROP_IMAGE_STORE_OFFSET = 20_000
# Content-store index offsets (50 000+) for image-derived rows written by
# store_image_derived_chunks() are declared in store_router (single source of truth).
# store_table_crop_images does not use those offsets, so they are not re-exported here.



def _update_image_stored_in_conn(conn, image_id: str, stored_in: str) -> None:
    """Update image_store.stored_in using the supplied connection.

    Must be called on the same connection as the cross-store INSERT so that
    both writes commit (or roll back) atomically.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE multi_store_rag_working.image_store
               SET stored_in = %s,
                   asset_role = CASE WHEN %s = 'table_store' THEN 'table_crop' ELSE asset_role END
               WHERE id = %s""",
            (stored_in, stored_in, image_id),
        )


def _update_image_stored_in(image_id: str, stored_in: str) -> None:
    """Update image_store.stored_in (opens its own connection).

    Thin wrapper around _update_image_stored_in_conn for callers that do not
    already hold an open connection (e.g. standalone correction scripts).
    """
    with get_db() as conn:
        _update_image_stored_in_conn(conn, image_id, stored_in)


def _normalise_vector(value) -> list | None:
    """Return a plain Python list from a pgvector embedding column value.
    psycopg2 with pgvector registered returns a list directly; without it,
    the value may arrive as a string like '[0.1,0.2,...]'."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            import ast
            result = ast.literal_eval(value)
            if isinstance(result, list):
                return result
        except Exception:
            pass
    return None


def _to_csv(headers: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    if headers:
        writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue()


def _is_numeric(s: str) -> bool:
    cleaned = s
    for symbol in (",", "$", "€", "£", "₹", "%"):
        cleaned = cleaned.replace(symbol, "")
    cleaned = cleaned.strip()
    try:
        float(cleaned)
        return True
    except ValueError:
        return False
