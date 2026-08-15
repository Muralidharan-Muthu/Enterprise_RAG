"""
Store router — generic registry for routing image-derived content into destination stores.

Wave 1 of the image cross-store routing design (2026-06-30).  The two subsequent waves
(storage_service.py rewrite, image_analysis_service.py extension) import from this module
and must NOT be imported here to avoid circular dependencies.

Architecture
------------
STORE_REGISTRY maps canonical store names to StoreHandler instances.  Each handler knows
how to:

  1. ``parse()``         — deserialise the VLM-produced structured_content string into a
                           typed dict of destination columns (JSON preferred, plain-text
                           tolerated; never raises).
  2. ``canonical_text()`` — derive the text that will be embedded for the *destination*
                            store (not the generic image blob).
  3. ``insert()``        — write a fully-populated, schema-compliant row into the
                           destination store using a caller-owned connection/transaction.
  4. ``validate()``      — re-SELECT the row and assert required columns are non-null;
                           raises ValueError so the caller can roll back and leave
                           ``stored_in`` honest.

The VLM prompt is assembled from ``build_vlm_schema_block()`` so the schema hints and the
parsers never drift.

Index offsets
-------------
Image-derived rows share tables with normal pipeline rows.  To avoid PK collisions the
same 50 000-base offsets used by storage_service._store_image_as_*_conn helpers are
re-declared here so this module is self-contained:

  _IMAGE_TABLE_INDEX_OFFSET  = 50_000   (table_store.table_index)
  _IMAGE_CHUNK_INDEX_OFFSET  = 50_000   (chunk_index / clause_index in other stores)

Migration note (document_store.from_image_store)
-------------------------------------------------
Migration 007 (app/db/migrations/007_from_image_store.sql) adds ``from_image_store`` to
all five stores — vector_store, table_store, clause_store, document_store, and
image_store — and has been applied to the live database. DocumentHandler.insert() can
rely on the column existing.
"""

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg2.extras

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index offsets — must match storage_service._IMAGE_*_OFFSET constants
# ---------------------------------------------------------------------------

_IMAGE_TABLE_INDEX_OFFSET = 50_000
"""table_store.table_index base for image-derived rows (matches storage_service)."""

_IMAGE_CHUNK_INDEX_OFFSET = 50_000
"""chunk_index / clause_index base for image-derived rows (matches storage_service)."""

# ---------------------------------------------------------------------------
# ImageCtx — traceability carrier passed through parse/insert/validate
# ---------------------------------------------------------------------------


@dataclass
class ImageCtx:
    """Carries source-image identity and traceability data for one cross-store write.

    Built by the caller (storage_service.store_image_derived_chunks) from an
    image_store row before the handler is invoked.

    Attributes
    ----------
    document_id:
        UUID of the parent document in document_registry.
    image_id:
        UUID primary key of the originating image_store row.
    image_index:
        Zero-based index of the image within the document (image_store.image_index).
    page_number:
        Page the image was extracted from; may be None.
    bbox_json:
        JSON string ``{"x1":..,"y1":..,"x2":..,"y2":..}`` or None.
    storage_path:
        Supabase Storage object path for the image file, or None.
    ocr_text:
        Raw OCR transcript produced by Docling/Tesseract; may be None.
    vlm_ocr_text:
        VLM transcription of the image; may be None.
    detected_store:
        Canonical store name chosen by the VLM (e.g. ``'table_store'``).
    confidence:
        Router confidence in [0, 1].
    reason:
        Human-readable rationale for the store selection.
    structured_content:
        Raw image_store.structured_content string (the VLM's structured JSON
        extraction, pre-parse). Used by prose-store handlers (clause/vector/
        document) as the preferred fallback source ahead of ocr_text — the
        VLM's own extraction is richer than blind Docling/Tesseract OCR of
        the same image. May be None.
    """

    document_id: str
    image_id: str
    image_index: int
    page_number: int | None
    bbox_json: str | None
    storage_path: str | None
    ocr_text: str | None
    vlm_ocr_text: str | None
    detected_store: str
    confidence: float = 0.0
    reason: str = ""
    structured_content: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_json(raw: str) -> dict | None:
    """Tolerant JSON parser — strips ```json fences and attempts json.loads.

    Returns a dict on success, or None if the input is not valid JSON.
    This is kept local to avoid importing image_analysis_service (which would
    create a circular import since image_analysis_service imports store_router).
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown code-fences sometimes emitted by VLMs
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence line (may be ```json or ```)
        lines = lines[1:] if lines else lines
        # Drop closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _traceability(ctx: ImageCtx) -> dict:
    """Return the standard traceability dict embedded in every *_metadata column."""
    return {
        "source": "image",
        "source_image_id": ctx.image_id,
        "image_index": ctx.image_index,
        "page_number": ctx.page_number,
        "detected_store": ctx.detected_store,
        "confidence": ctx.confidence,
        "reason_for_store_selection": ctx.reason,
    }


def _to_csv(headers: list[str], rows: list[list[str]]) -> str:
    """Render headers + rows as a CSV string (matches storage_service._to_csv)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    if headers:
        writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue()


def _is_numeric(s: str) -> bool:
    """Return True if the string represents a number after stripping common symbols."""
    cleaned = s.replace(",", "").replace("$", "").replace("€", "").replace("%", "").strip()
    try:
        float(cleaned)
        return True
    except ValueError:
        return False


def _table_extraction_quality_bucket(confidence: float | None) -> str | None:
    """Map a 0.0-1.0 confidence score to a coarse quality bucket (migration 014).

    Mirrors storage_service._extraction_quality_bucket; kept local to this module
    to avoid a storage_service <-> store_router import cycle. Returns None when no
    confidence score is available."""
    if confidence is None:
        return None
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a simple Markdown table from headers and rows."""
    if not headers and not rows:
        return ""
    lines = []
    if headers:
        lines.append("| " + " | ".join(str(h) for h in headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# StoreHandler base class
# ---------------------------------------------------------------------------


class StoreHandler:
    """Abstract store handler.  One concrete instance per destination store.

    Subclasses must define ``name``, ``content_type``, ``schema_hint`` as class
    attributes and implement ``parse``, ``canonical_text``, ``insert``, and
    ``validate``.
    """

    #: Canonical store name — key in STORE_REGISTRY.
    name: str
    #: image_store.content_type value this store is associated with.
    content_type: str
    #: JSON-shape instructions injected into the VLM prompt for this store.
    schema_hint: str

    def parse(self, structured_raw: str, ctx: ImageCtx) -> dict:
        """Deserialise VLM structured_content into a dict of destination columns.

        Parameters
        ----------
        structured_raw:
            Raw string from image_store.structured_content.
        ctx:
            Image context for fallback values (ocr_text, page_number, etc.).

        Returns
        -------
        dict
            Always returns a dict — never raises.  Falls back to a minimal plain-text
            representation when the input is not valid JSON.
        """
        raise NotImplementedError

    def canonical_text(self, parsed: dict, ctx: ImageCtx) -> str:
        """Return the text that should be embedded for the destination store.

        This must be derived from the *parsed destination content*, not from the
        raw image blob, so the vector reflects what the store actually contains.

        Parameters
        ----------
        parsed:
            Output of ``self.parse()``.
        ctx:
            Image context for fallback values.

        Returns
        -------
        str
            Text to embed; may be empty to signal "skip this image".
        """
        raise NotImplementedError

    def insert(self, conn, parsed: dict, embedding: list, ctx: ImageCtx) -> int:
        """INSERT a fully-populated row into the destination store.

        The caller owns the transaction — commit and rollback happen outside this
        method.  The INSERT must populate EVERY applicable schema column plus the
        traceability dict in the store's ``*_metadata`` JSONB.

        Parameters
        ----------
        conn:
            Open psycopg2 connection.  Do NOT commit inside this method.
        parsed:
            Output of ``self.parse()``.
        embedding:
            1024-dim list produced from ``canonical_text()`` by the caller.
        ctx:
            Image context carrying document_id, image_id, offsets, etc.

        Returns
        -------
        int
            Affected row count.  Must be >= 1 when the row is present; the caller
            raises if 0 is returned so stored_in is never flipped on a silent skip.
        """
        raise NotImplementedError

    def validate(self, conn, ctx: ImageCtx) -> None:
        """Re-SELECT the just-inserted row and assert it is complete.

        Raises ValueError if the row is absent, if the embedding is NULL, or if the
        store's primary text column is empty.  A raised exception causes the caller's
        transaction to roll back, which keeps stored_in honest.

        Parameters
        ----------
        conn:
            The same open connection used for the INSERT (same transaction).
        ctx:
            Image context identifying the row (document_id + index).
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# TableStoreHandler
# ---------------------------------------------------------------------------


class TableStoreHandler(StoreHandler):
    """Handler for table_store.

    VLM emits::

        {
            "title": "...",
            "headers": ["Col A", "Col B", ...],
            "rows": [["v1", "v2"], ...],
            "units": "USD millions",
            "fiscal_year": "FY2024",
            "reporting_period": "Q3",
            "currency": "USD",
            "table_category": "income_statement",
            "notes": "..."
        }

    Fallback (plain text): empty headers/rows; raw_text/markdown_text set to the input.
    """

    name = "table_store"
    content_type = "table"
    schema_hint = (
        "Return a JSON object with keys: "
        '"title" (string, table heading), '
        '"headers" (array of column-name strings), '
        '"rows" (array of arrays — one inner array per data row, cells as strings), '
        '"units" (string, e.g. "USD millions"), '
        '"fiscal_year" (string, e.g. "FY2024"), '
        '"reporting_period" (string, e.g. "Q3 2024"), '
        '"currency" (string ISO-4217, e.g. "USD"), '
        '"table_category" (one of: balance_sheet, income_statement, cash_flow, kpi, '
        'comparison, other), '
        '"notes" (string, any footnotes or caveats).'
    )

    def parse(self, structured_raw: str, ctx: ImageCtx) -> dict:
        """Parse VLM JSON into table columns; plain-text fallback to empty grid."""
        data = _try_json(structured_raw)
        if data is not None:
            headers = data.get("headers") or []
            rows = data.get("rows") or []
            # Coerce every cell to str for downstream CSV/markdown rendering
            headers = [str(h) for h in headers]
            rows = [[str(c) for c in row] for row in rows]
            return {
                "table_title": data.get("title") or f"Image table (page {ctx.page_number})",
                "headers": headers,
                "rows": rows,
                "units": data.get("units") or None,
                "fiscal_year": data.get("fiscal_year") or None,
                "reporting_period": data.get("reporting_period") or None,
                "currency": data.get("currency") or None,
                "table_category": data.get("table_category") or None,
                "notes": data.get("notes") or None,
            }
        # Plain-text fallback
        text = (structured_raw or ctx.ocr_text or "").strip()
        return {
            "table_title": f"Image table (page {ctx.page_number})",
            "headers": [],
            "rows": [],
            "units": None,
            "fiscal_year": None,
            "reporting_period": None,
            "currency": None,
            "table_category": None,
            "notes": None,
            "_fallback_text": text,
        }

    def canonical_text(self, parsed: dict, ctx: ImageCtx) -> str:
        """Canonical text = markdown_text when we have structure, else raw OCR."""
        headers = parsed.get("headers") or []
        rows = parsed.get("rows") or []
        if headers or rows:
            return _markdown_table(headers, rows)
        return parsed.get("_fallback_text") or ctx.ocr_text or ""

    def insert(self, conn, parsed: dict, embedding: list, ctx: ImageCtx) -> int:  # noqa: PLR0914
        """INSERT a fully-populated table_store row.

        Populates: document_id, table_index, table_title, page_number, bbox,
        raw_text, markdown_text, json_data, csv_data, row_count, col_count,
        has_numeric_data, has_currency, has_percentages, detected_units,
        fiscal_year, reporting_period, currency, table_category,
        context_before (NULL — a crop has no preceding page text), context_after
        (VLM notes/footnotes), image_storage_path, table_summary, embedding,
        table_metadata, from_image_store.
        """
        headers = parsed.get("headers") or []
        rows = parsed.get("rows") or []
        fallback = parsed.get("_fallback_text", "")

        json_data = json.dumps({"headers": headers, "rows": rows})
        csv_data = _to_csv(headers, rows) if (headers or rows) else fallback
        markdown_text = _markdown_table(headers, rows) if (headers or rows) else fallback
        raw_text = ctx.ocr_text or markdown_text or fallback

        all_cells = [cell for row in rows for cell in row]
        has_currency_flag = any(
            any(sym in cell for sym in ["$", "€", "£", "₹", "USD", "EUR", "INR"])
            for cell in all_cells
        )
        has_percentages = any("%" in cell for cell in all_cells)
        has_numeric = any(_is_numeric(cell) for cell in all_cells)

        units_raw = parsed.get("units")
        detected_units = [units_raw] if units_raw else None  # TEXT[]

        table_index = _IMAGE_TABLE_INDEX_OFFSET + ctx.image_index
        table_summary = (
            f"{parsed['table_title']} — {len(rows)} rows × {len(headers)} columns"
            if headers or rows
            else raw_text[:200]
        )
        metadata = _traceability(ctx)

        # ── Write-time lineage (migration 014, Slice 2a) — first-class columns
        # alongside the existing JSONB traceability (kept for backward-compat).
        # ctx.image_id is the image_store UUID this table was reconstructed from,
        # so extraction_method is always 'image_vlm' for image-derived rows. ──
        extraction_method = "image_vlm"
        extraction_quality = _table_extraction_quality_bucket(ctx.confidence)
        provenance = json.dumps({"source": "image", "reason": ctx.reason})

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO multi_store_rag_working.table_store
                (document_id, table_index, table_title, page_number, bbox,
                 raw_text, markdown_text, json_data, csv_data,
                 row_count, col_count,
                 has_numeric_data, has_currency, has_percentages,
                 detected_units,
                 fiscal_year, reporting_period, currency, table_category,
                 context_before, context_after,
                 image_storage_path,
                 table_summary,
                 embedding,
                 table_metadata,
                 from_image_store,
                 source_image_id, extraction_method, extraction_quality,
                 source_confidence, provenance,
                 structured_content, structured_content_embedding)
            VALUES (
                %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s::jsonb, %s,
                %s, %s,
                %s, %s, %s,
                %s::text[],
                %s, %s, %s, %s,
                %s, %s,
                %s,
                %s,
                %s::vector,
                %s::jsonb,
                TRUE,
                %s::uuid, %s, %s,
                %s, %s::jsonb,
                %s, %s::vector
            )
            """,
            (
                ctx.document_id,
                table_index,
                parsed["table_title"],
                ctx.page_number,
                ctx.bbox_json,
                raw_text,
                markdown_text,
                json_data,
                csv_data,
                len(rows),
                len(headers),
                has_numeric,
                has_currency_flag,
                has_percentages,
                detected_units,
                parsed.get("fiscal_year"),
                parsed.get("reporting_period"),
                parsed.get("currency"),
                parsed.get("table_category"),
                None,                  # context_before — a crop has no preceding page text
                parsed.get("notes"),   # context_after — VLM footnotes/caveats (else NULL)
                ctx.storage_path,
                table_summary,
                embedding,
                json.dumps(metadata),
                ctx.image_id,
                extraction_method,
                extraction_quality,
                ctx.confidence,
                provenance,
                # Universal VLM pipeline: persist the VLM's raw structured_content
                # (the clean extraction) and reuse the row embedding — already
                # built from this same VLM content — as its sc embedding so
                # retrieval's COALESCE(sc_emb, embedding) prefers it. Fall back to
                # markdown_text when the VLM string is absent.
                ctx.structured_content or markdown_text or "",
                embedding,
            ),
        )
        return cur.rowcount

    def validate(self, conn, ctx: ImageCtx) -> None:
        """Assert the row exists with a non-null embedding and non-empty raw_text."""
        table_index = _IMAGE_TABLE_INDEX_OFFSET + ctx.image_index
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT raw_text, embedding IS NOT NULL AS has_embedding
                FROM multi_store_rag_working.table_store
                WHERE document_id = %s AND table_index = %s
                """,
                (ctx.document_id, table_index),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"validate failed: table_store row missing for "
                f"document_id={ctx.document_id!r}, table_index={table_index}"
            )
        raw_text, has_embedding = row
        if not has_embedding:
            raise ValueError(
                f"validate failed: table_store embedding NULL for "
                f"document_id={ctx.document_id!r}, table_index={table_index}"
            )
        if not raw_text:
            raise ValueError(
                f"validate failed: table_store raw_text empty for "
                f"document_id={ctx.document_id!r}, table_index={table_index}"
            )


# ---------------------------------------------------------------------------
# ClauseStoreHandler
# ---------------------------------------------------------------------------


class ClauseStoreHandler(StoreHandler):
    """Handler for clause_store.

    VLM emits::

        {
            "clause_title": "...",
            "clause_text": "...",
            "clause_type": "obligation",
            "parties": ["Acme Corp", "Widget Inc"],
            "obligor": "Acme Corp",
            "obligee": "Widget Inc",
            "key_dates": {"effective_date": "2024-01-01", "expiry": "2027-01-01"},
            "monetary_values": {"amount": 500000, "currency": "USD"},
            "obligations": ["Party A must ...", "Party B shall ..."],
            "risk_level": "high",
            "risk_rationale": "..."
        }

    Fallback (plain text): clause_text set to raw input; all other fields defaulted.
    """

    name = "clause_store"
    content_type = "text"
    schema_hint = (
        "Return a JSON object with keys: "
        '"clause_title" (string, short title of this clause), '
        '"clause_number" (string clause/article number like "12.3.1" or "Article III", or null), '
        '"clause_subtype" (string, finer-grained category of the clause, or null), '
        '"clause_text" (string, full verbatim text of the clause), '
        '"clause_type" (one of: obligation, prohibition, right, definition, liability, '
        'indemnification, termination, confidentiality, dispute_resolution, force_majeure, '
        'warranty, penalty, governing_law, general), '
        '"parties" (array of party name strings), '
        '"obligor" (string, the party bearing the obligation), '
        '"obligee" (string, the party receiving the benefit), '
        '"key_dates" (object with named date keys, values as ISO-8601 strings), '
        '"monetary_values" (object with amount/currency keys), '
        '"obligations" (array of obligation-summary strings), '
        '"risk_level" (one of: high, medium, low), '
        '"risk_rationale" (string, explanation of the risk assessment).'
    )

    def parse(self, structured_raw: str, ctx: ImageCtx) -> dict:
        """Parse VLM JSON into clause columns; plain-text fallback."""
        data = _try_json(structured_raw)
        if data is not None:
            return {
                "clause_title": data.get("clause_title") or None,
                "clause_number": data.get("clause_number") or None,
                "clause_subtype": data.get("clause_subtype") or None,
                # Prefer the raw structured_content over ocr_text when the VLM's
                # JSON omitted clause_text — the VLM's own extraction is richer
                # than blind OCR of the same image.
                "clause_text": data.get("clause_text") or (structured_raw or ""),
                "clause_type": data.get("clause_type") or "general",
                "parties": data.get("parties") or [],
                "obligor": data.get("obligor") or None,
                "obligee": data.get("obligee") or None,
                "key_dates": data.get("key_dates") or {},
                "monetary_values": data.get("monetary_values") or {},
                "obligations": data.get("obligations") or [],
                "risk_level": data.get("risk_level") or None,
                "risk_rationale": data.get("risk_rationale") or None,
            }
        # Plain-text fallback
        text = (structured_raw or ctx.ocr_text or "").strip()
        return {
            "clause_title": None,
            "clause_number": None,
            "clause_subtype": None,
            "clause_text": text,
            "clause_type": "general",
            "parties": [],
            "obligor": None,
            "obligee": None,
            "key_dates": {},
            "monetary_values": {},
            "obligations": [],
            "risk_level": None,
            "risk_rationale": None,
        }

    def canonical_text(self, parsed: dict, ctx: ImageCtx) -> str:
        """Canonical text = clause_text (the text that gets embedded and searched).

        Falls back to structured_content (the VLM's raw extraction) ahead of
        ocr_text — see ImageCtx.structured_content.
        """
        return parsed.get("clause_text") or ctx.structured_content or ""

    def insert(self, conn, parsed: dict, embedding: list, ctx: ImageCtx) -> int:
        """INSERT a fully-populated clause_store row.

        Populates: document_id, clause_index, clause_title, clause_text,
        clause_word_count, clause_type, risk_level, risk_rationale, obligor,
        obligee, parties_mentioned, key_dates, monetary_values, conditions,
        page_number, page_numbers, section_path, embedding, clause_metadata,
        from_image_store, source_image_id.
        """
        clause_text = parsed.get("clause_text") or ""
        clause_index = _IMAGE_CHUNK_INDEX_OFFSET + ctx.image_index
        parties = parsed.get("parties") or []
        obligations = parsed.get("obligations") or []
        key_dates = parsed.get("key_dates") or {}
        monetary_values = parsed.get("monetary_values") or {}
        metadata = _traceability(ctx)

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO multi_store_rag_working.clause_store
                (document_id, clause_index,
                 clause_number, clause_title, clause_text, clause_word_count,
                 clause_type, clause_subtype, risk_level, risk_rationale,
                 obligor, obligee, parties_mentioned,
                 key_dates, monetary_values,
                 conditions,
                 page_number, page_numbers, section_path,
                 embedding,
                 clause_metadata,
                 from_image_store,
                 source_image_id)
            VALUES (
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s::text[],
                %s::jsonb, %s::jsonb,
                %s::text[],
                %s, %s::int[], %s::text[],
                %s::vector,
                %s::jsonb,
                TRUE,
                %s::uuid
            )
            """,
            (
                ctx.document_id,
                clause_index,
                parsed.get("clause_number"),
                parsed.get("clause_title"),
                clause_text,
                len(clause_text.split()),
                parsed.get("clause_type") or "general",
                parsed.get("clause_subtype"),
                parsed.get("risk_level"),
                parsed.get("risk_rationale"),
                parsed.get("obligor"),
                parsed.get("obligee"),
                parties,
                json.dumps(key_dates),
                json.dumps(monetary_values),
                obligations,
                ctx.page_number,
                [ctx.page_number] if ctx.page_number is not None else [],
                [],  # section_path — no structural context for image-derived clauses
                embedding,
                json.dumps(metadata),
                ctx.image_id,
            ),
        )
        return cur.rowcount

    def validate(self, conn, ctx: ImageCtx) -> None:
        """Assert the row exists with a non-null embedding and non-empty clause_text."""
        clause_index = _IMAGE_CHUNK_INDEX_OFFSET + ctx.image_index
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT clause_text, embedding IS NOT NULL AS has_embedding
                FROM multi_store_rag_working.clause_store
                WHERE document_id = %s AND clause_index = %s
                """,
                (ctx.document_id, clause_index),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"validate failed: clause_store row missing for "
                f"document_id={ctx.document_id!r}, clause_index={clause_index}"
            )
        clause_text, has_embedding = row
        if not has_embedding:
            raise ValueError(
                f"validate failed: clause_store embedding NULL for "
                f"document_id={ctx.document_id!r}, clause_index={clause_index}"
            )
        if not clause_text:
            raise ValueError(
                f"validate failed: clause_store clause_text empty for "
                f"document_id={ctx.document_id!r}, clause_index={clause_index}"
            )


# ---------------------------------------------------------------------------
# VectorStoreHandler
# ---------------------------------------------------------------------------


class VectorStoreHandler(StoreHandler):
    """Handler for vector_store.

    VLM emits::

        {
            "text": "...",
            "section_title": "...",
            "keywords": ["word1", "word2"],
            "semantic_type": "paragraph"
        }

    Fallback (plain text): chunk_text set to raw input; semantic_type='image_text'.
    """

    name = "vector_store"
    content_type = "text"
    schema_hint = (
        "Return a JSON object with keys: "
        '"text" (string, the clean retrieval text — NOT a caption, the actual content), '
        '"section_title" (string, heading under which this text appears, or null), '
        '"keywords" (array of important keyword strings), '
        '"semantic_type" (one of: paragraph, list, header, caption, image_text).'
    )

    def parse(self, structured_raw: str, ctx: ImageCtx) -> dict:
        """Parse VLM JSON into vector_store columns; plain-text fallback."""
        data = _try_json(structured_raw)
        if data is not None:
            return {
                # Prefer the raw structured_content over ocr_text when the
                # VLM's JSON omitted "text" — the VLM's own extraction is
                # richer than blind OCR of the same image.
                "chunk_text": data.get("text") or (structured_raw or ""),
                "section_title": data.get("section_title") or None,
                "keywords": data.get("keywords") or [],
                "semantic_type": data.get("semantic_type") or "image_text",
            }
        # Plain-text fallback
        text = (structured_raw or ctx.ocr_text or "").strip()
        return {
            "chunk_text": text,
            "section_title": None,
            "keywords": [],
            "semantic_type": "image_text",
        }

    def canonical_text(self, parsed: dict, ctx: ImageCtx) -> str:
        """Canonical text = chunk_text.

        Falls back to structured_content (the VLM's raw extraction) ahead of
        ocr_text — see ImageCtx.structured_content.
        """
        return parsed.get("chunk_text") or ctx.structured_content or ""

    def insert(self, conn, parsed: dict, embedding: list, ctx: ImageCtx) -> int:
        """INSERT a fully-populated vector_store row.

        Populates: document_id, chunk_index, chunk_text, chunk_word_count,
        chunk_char_count, page_number, page_numbers, bbox, section_title,
        section_level, semantic_type, keywords, embedding, chunk_metadata,
        from_image_store, source_image_id.
        """
        chunk_text = parsed.get("chunk_text") or ""
        chunk_index = _IMAGE_CHUNK_INDEX_OFFSET + ctx.image_index
        keywords = parsed.get("keywords") or []
        metadata = _traceability(ctx)

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO multi_store_rag_working.vector_store
                (document_id, chunk_index,
                 chunk_text, chunk_word_count, chunk_char_count,
                 page_number, page_numbers, bbox,
                 section_title, section_level,
                 semantic_type, keywords,
                 embedding,
                 chunk_metadata,
                 from_image_store,
                 source_image_id)
            VALUES (
                %s, %s,
                %s, %s, %s,
                %s, %s::int[], %s::jsonb,
                %s, %s,
                %s, %s::text[],
                %s::vector,
                %s::jsonb,
                TRUE,
                %s::uuid
            )
            """,
            (
                ctx.document_id,
                chunk_index,
                chunk_text,
                len(chunk_text.split()),
                len(chunk_text),
                ctx.page_number,
                [ctx.page_number] if ctx.page_number is not None else [],
                ctx.bbox_json,
                parsed.get("section_title"),
                None,  # section_level — not extractable from image context alone
                parsed.get("semantic_type") or "image_text",
                keywords,
                embedding,
                json.dumps(metadata),
                ctx.image_id,
            ),
        )
        return cur.rowcount

    def validate(self, conn, ctx: ImageCtx) -> None:
        """Assert the row exists with a non-null embedding and non-empty chunk_text."""
        chunk_index = _IMAGE_CHUNK_INDEX_OFFSET + ctx.image_index
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_text, embedding IS NOT NULL AS has_embedding
                FROM multi_store_rag_working.vector_store
                WHERE document_id = %s AND chunk_index = %s
                """,
                (ctx.document_id, chunk_index),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"validate failed: vector_store row missing for "
                f"document_id={ctx.document_id!r}, chunk_index={chunk_index}"
            )
        chunk_text, has_embedding = row
        if not has_embedding:
            raise ValueError(
                f"validate failed: vector_store embedding NULL for "
                f"document_id={ctx.document_id!r}, chunk_index={chunk_index}"
            )
        if not chunk_text:
            raise ValueError(
                f"validate failed: vector_store chunk_text empty for "
                f"document_id={ctx.document_id!r}, chunk_index={chunk_index}"
            )


# ---------------------------------------------------------------------------
# DocumentStoreHandler
# ---------------------------------------------------------------------------


class DocumentStoreHandler(StoreHandler):
    """Handler for document_store.

    VLM emits::

        {
            "chunk_text": "...",
            "chunk_type": "body",
            "section_title": "...",
            "citation": {
                "key": "smith2024",
                "title": "...",
                "authors": ["Alice Smith"],
                "year": 2024,
                "doi": "10.1234/xyz",
                "url": "https://...",
                "journal": "Nature",
                "confidence": 0.9
            },
            "entities": ["CRISPR", "mRNA"]
        }

    Fallback (plain text): chunk_text set to raw input; all citation fields NULL.

    Migration note
    --------------
    ``from_image_store`` is set TRUE in the INSERT. Migration 007
    (app/db/migrations/007_from_image_store.sql) already adds this column to
    document_store and has been applied to the live database.
    """

    name = "document_store"
    content_type = "text"
    schema_hint = (
        "Return a JSON object with keys: "
        '"chunk_text" (string, the verbatim passage text), '
        '"chunk_type" (one of: abstract, introduction, methodology, results, discussion, '
        'conclusion, body, figure_caption, reference), '
        '"section_title" (string, section heading or null), '
        '"citation" (object with keys: key, title, authors (array), year (int), doi, url, '
        'journal, confidence (float 0-1)), '
        '"entities" (array of named-entity strings such as chemicals, genes, organisations), '
        '"contains_hypothesis" (boolean, true if the passage states a hypothesis), '
        '"contains_finding" (boolean, true if it reports a finding or result), '
        '"contains_method" (boolean, true if it describes a method or procedure).'
    )

    def parse(self, structured_raw: str, ctx: ImageCtx) -> dict:
        """Parse VLM JSON into document_store columns; plain-text fallback."""
        data = _try_json(structured_raw)
        if data is not None:
            citation = data.get("citation") or {}
            return {
                # Prefer the raw structured_content over ocr_text when the
                # VLM's JSON omitted chunk_text — the VLM's own extraction is
                # richer than blind OCR of the same image.
                "chunk_text": data.get("chunk_text") or (structured_raw or ""),
                "chunk_type": data.get("chunk_type") or "body",
                "section_title": data.get("section_title") or None,
                "citation_key": citation.get("key") or None,
                "source_title": citation.get("title") or None,
                "source_authors": citation.get("authors") or [],
                "source_year": citation.get("year") or None,
                "source_doi": citation.get("doi") or None,
                "source_url": citation.get("url") or None,
                "source_journal": citation.get("journal") or None,
                "source_confidence": citation.get("confidence") or None,
                "entities": data.get("entities") or [],
                "contains_hypothesis": bool(data.get("contains_hypothesis", False)),
                "contains_finding": bool(data.get("contains_finding", False)),
                "contains_method": bool(data.get("contains_method", False)),
            }
        # Plain-text fallback
        text = (structured_raw or ctx.ocr_text or "").strip()
        return {
            "chunk_text": text,
            "chunk_type": "body",
            "section_title": None,
            "citation_key": None,
            "source_title": None,
            "source_authors": [],
            "source_year": None,
            "source_doi": None,
            "source_url": None,
            "source_journal": None,
            "source_confidence": None,
            "entities": [],
            "contains_hypothesis": False,
            "contains_finding": False,
            "contains_method": False,
        }

    def canonical_text(self, parsed: dict, ctx: ImageCtx) -> str:
        """Canonical text = chunk_text.

        Falls back to structured_content (the VLM's raw extraction) ahead of
        ocr_text — see ImageCtx.structured_content.
        """
        return parsed.get("chunk_text") or ctx.structured_content or ""

    def insert(self, conn, parsed: dict, embedding: list, ctx: ImageCtx) -> int:
        """INSERT a fully-populated document_store row.

        Populates: document_id, chunk_index, chunk_text, chunk_type,
        citation_key, source_title, source_authors, source_year, source_doi,
        source_url, source_journal, source_confidence, page_number,
        section_title, section_type, contains_hypothesis, contains_finding,
        contains_method, entities_mentioned, embedding, chunk_metadata,
        from_image_store, source_image_id.

        Requires migration 007 (app/db/migrations/007_from_image_store.sql), which
        already adds this column to document_store and is applied to the live
        database.
        """
        chunk_text = parsed.get("chunk_text") or ""
        chunk_index = _IMAGE_CHUNK_INDEX_OFFSET + ctx.image_index
        entities = parsed.get("entities") or []
        source_authors = parsed.get("source_authors") or []
        metadata = _traceability(ctx)

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO multi_store_rag_working.document_store
                (document_id, chunk_index,
                 chunk_text, chunk_type,
                 citation_key, source_title, source_authors, source_year,
                 source_doi, source_url, source_journal, source_confidence,
                 page_number, section_title, section_type,
                 contains_hypothesis, contains_finding, contains_method,
                 entities_mentioned,
                 embedding,
                 chunk_metadata,
                 from_image_store,
                 source_image_id)
            VALUES (
                %s, %s,
                %s, %s,
                %s, %s, %s::text[], %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s::text[],
                %s::vector,
                %s::jsonb,
                TRUE,
                %s::uuid
            )
            """,
            (
                ctx.document_id,
                chunk_index,
                chunk_text,
                parsed.get("chunk_type") or "body",
                parsed.get("citation_key"),
                parsed.get("source_title"),
                source_authors,
                parsed.get("source_year"),
                parsed.get("source_doi"),
                parsed.get("source_url"),
                parsed.get("source_journal"),
                parsed.get("source_confidence"),
                ctx.page_number,
                parsed.get("section_title"),
                parsed.get("chunk_type") or "body",  # section_type mirrors chunk_type
                parsed.get("contains_hypothesis", False),
                parsed.get("contains_finding", False),
                parsed.get("contains_method", False),
                entities,
                embedding,
                json.dumps(metadata),
                ctx.image_id,
            ),
        )
        return cur.rowcount

    def validate(self, conn, ctx: ImageCtx) -> None:
        """Assert the row exists with a non-null embedding and non-empty chunk_text."""
        chunk_index = _IMAGE_CHUNK_INDEX_OFFSET + ctx.image_index
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_text, embedding IS NOT NULL AS has_embedding
                FROM multi_store_rag_working.document_store
                WHERE document_id = %s AND chunk_index = %s
                """,
                (ctx.document_id, chunk_index),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"validate failed: document_store row missing for "
                f"document_id={ctx.document_id!r}, chunk_index={chunk_index}"
            )
        chunk_text, has_embedding = row
        if not has_embedding:
            raise ValueError(
                f"validate failed: document_store embedding NULL for "
                f"document_id={ctx.document_id!r}, chunk_index={chunk_index}"
            )
        if not chunk_text:
            raise ValueError(
                f"validate failed: document_store chunk_text empty for "
                f"document_id={ctx.document_id!r}, chunk_index={chunk_index}"
            )


# ---------------------------------------------------------------------------
# Registry and public API
# ---------------------------------------------------------------------------

#: Singleton handler instances — one per destination store.
_TABLE_HANDLER = TableStoreHandler()
_CLAUSE_HANDLER = ClauseStoreHandler()
_VECTOR_HANDLER = VectorStoreHandler()
_DOCUMENT_HANDLER = DocumentStoreHandler()

STORE_REGISTRY: dict[str, StoreHandler] = {
    "table_store": _TABLE_HANDLER,
    "clause_store": _CLAUSE_HANDLER,
    "vector_store": _VECTOR_HANDLER,
    "document_store": _DOCUMENT_HANDLER,
}
"""Canonical store name → StoreHandler.

Keys are the values used in image_store.detected_store and image_store.stored_in.
Add a new entry here (plus a concrete StoreHandler subclass) to support a new store
without touching storage_service.py or any other caller.
"""


def get_handler(detected_store: str) -> StoreHandler | None:
    """Return the handler for *detected_store*, or None if no routing is needed.

    Returns None for ``'image_store'`` (the image stays in image_store) and for
    any unrecognised store name.  The caller's generic loop should skip None results
    and leave ``stored_in`` unchanged.

    Parameters
    ----------
    detected_store:
        Value of image_store.detected_store, e.g. ``'table_store'``.

    Returns
    -------
    StoreHandler or None
    """
    return STORE_REGISTRY.get(detected_store)


def build_vlm_schema_block() -> str:
    """Assemble all handlers' schema hints into a single VLM-prompt section.

    The VLM module (image_analysis_service.py) should inject the return value
    verbatim into the prompt it sends to the model.  Because the schema hints
    are the single source of truth (parsers are derived from the same data
    structures), the prompt and the parsers can never drift.

    Returns
    -------
    str
        A multi-line string listing each supported store with its expected
        JSON output shape.  Example::

            === Structured Content Schemas by Destination Store ===

            table_store:
              Return a JSON object with keys: ...

            clause_store:
              Return a JSON object with keys: ...
            ...
    """
    lines = ["=== Structured Content Schemas by Destination Store ===", ""]
    for store_name, handler in sorted(STORE_REGISTRY.items()):
        lines.append(f"{store_name}:")
        lines.append(f"  {handler.schema_hint}")
        lines.append("")
    return "\n".join(lines)
