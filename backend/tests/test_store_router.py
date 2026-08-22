"""
Tests for app.services.store_router — Wave 1 of the image cross-store routing design.

All tests are pure-Python with NO live database.  DB interactions are exercised through
fake connections / cursors that record the SQL and parameters sent to them, following
exactly the same mock pattern used in tests/test_image_derived_storage.py.

Coverage
--------
- get_handler returns the correct StoreHandler per known store and None for
  'image_store' / unknown values.
- build_vlm_schema_block includes every store's name.
- TableStoreHandler.parse: JSON object → structured dict; plain-text fallback.
- ClauseStoreHandler.parse: JSON object → clause dict; plain-text fallback.
- VectorStoreHandler.parse: JSON object → chunk dict; plain-text fallback.
- DocumentStoreHandler.parse: JSON object with citation → doc dict; plain-text fallback.
- Traceability dict carries source_image_id in every *_metadata field.
- insert() returns rowcount >= 1 and the executed SQL includes the required columns.
- validate() raises ValueError when the re-SELECT returns no row or embedding NULL.
"""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.services.store_router as sr
from app.services.store_router import (
    ClauseStoreHandler,
    DocumentStoreHandler,
    ImageCtx,
    StoreHandler,
    TableStoreHandler,
    VectorStoreHandler,
    _IMAGE_CHUNK_INDEX_OFFSET,
    _IMAGE_TABLE_INDEX_OFFSET,
    build_vlm_schema_block,
    get_handler,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _ctx(**kwargs) -> ImageCtx:
    """Build an ImageCtx with sensible defaults; override with kwargs."""
    defaults = dict(
        document_id="doc-uuid-1",
        image_id="img-uuid-42",
        image_index=3,
        page_number=7,
        bbox_json='{"x1":0,"y1":0,"x2":100,"y2":100}',
        storage_path="images/doc-uuid-1/3.png",
        ocr_text="OCR text from image",
        vlm_ocr_text="VLM OCR text",
        detected_store="table_store",
        confidence=0.87,
        reason="Detected financial data",
    )
    defaults.update(kwargs)
    return ImageCtx(**defaults)


def _fake_embedding(dim: int = 4) -> list:
    return [0.25] * dim


def _make_conn(rowcount: int = 1, fetchone_return=None):
    """Return (conn, cur) where cur.rowcount == rowcount and fetchone returns
    fetchone_return.  Supports both plain ``cur = conn.cursor()`` and
    ``with conn.cursor() as cur:`` usage patterns."""
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchone.return_value = fetchone_return
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _insert_sql_and_params(handler, parsed, embedding, ctx):
    """Call handler.insert() with a fake conn and return (sql, params, rowcount)."""
    conn, cur = _make_conn(rowcount=1)
    captured_sql = []
    captured_params = []

    original_execute = cur.execute

    def spy_execute(sql, params=None):
        captured_sql.append(sql)
        captured_params.append(params)

    cur.execute = spy_execute
    rc = handler.insert(conn, parsed, embedding, ctx)
    return captured_sql[0] if captured_sql else "", captured_params[0] if captured_params else (), rc


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_get_handler_table_store(self):
        h = get_handler("table_store")
        assert isinstance(h, TableStoreHandler)
        assert h.name == "table_store"

    def test_get_handler_clause_store(self):
        h = get_handler("clause_store")
        assert isinstance(h, ClauseStoreHandler)
        assert h.name == "clause_store"

    def test_get_handler_vector_store(self):
        h = get_handler("vector_store")
        assert isinstance(h, VectorStoreHandler)
        assert h.name == "vector_store"

    def test_get_handler_document_store(self):
        h = get_handler("document_store")
        assert isinstance(h, VectorStoreHandler)
        assert h.name == "vector_store"

    def test_get_handler_returns_none_for_image_store(self):
        assert get_handler("image_store") is None

    def test_get_handler_returns_none_for_unknown(self):
        assert get_handler("unknown_store") is None

    def test_get_handler_returns_none_for_empty_string(self):
        assert get_handler("") is None

    def test_store_registry_keys(self):
        assert set(sr.STORE_REGISTRY.keys()) == {
            "table_store", "clause_store", "vector_store", "document_store"
        }

    def test_build_vlm_schema_block_contains_all_stores(self):
        block = build_vlm_schema_block()
        for name in ("table_store", "clause_store", "vector_store", "document_store"):
            assert name in block, f"'{name}' missing from schema block"

    def test_build_vlm_schema_block_is_string(self):
        assert isinstance(build_vlm_schema_block(), str)

    def test_index_offsets_match_storage_service(self):
        """The offset constants must equal 50_000 (matching storage_service)."""
        assert _IMAGE_TABLE_INDEX_OFFSET == 50_000
        assert _IMAGE_CHUNK_INDEX_OFFSET == 50_000


# ---------------------------------------------------------------------------
# TableStoreHandler
# ---------------------------------------------------------------------------

TABLE_JSON = json.dumps({
    "title": "Revenue Summary",
    "headers": ["Quarter", "Revenue"],
    "rows": [["Q1", "$100M"], ["Q2", "$120M"]],
    "units": "USD millions",
    "fiscal_year": "FY2024",
    "reporting_period": "H1",
    "currency": "USD",
    "table_category": "income_statement",
    "notes": "Unaudited",
})


class TestTableStoreHandler:
    handler = TableStoreHandler()

    def test_parse_json_headers_and_rows(self):
        ctx = _ctx(detected_store="table_store")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        assert parsed["headers"] == ["Quarter", "Revenue"]
        assert parsed["rows"] == [["Q1", "$100M"], ["Q2", "$120M"]]
        assert parsed["table_title"] == "Revenue Summary"
        assert parsed["fiscal_year"] == "FY2024"
        assert parsed["table_category"] == "income_statement"

    def test_parse_json_row_count_and_col_count(self):
        ctx = _ctx(detected_store="table_store")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        assert len(parsed["rows"]) == 2
        assert len(parsed["headers"]) == 2

    def test_parse_plain_text_fallback_empty_grid(self):
        ctx = _ctx(detected_store="table_store")
        parsed = self.handler.parse("not json at all — plain text", ctx)
        assert parsed["headers"] == []
        assert parsed["rows"] == []
        assert "not json at all" in parsed["_fallback_text"]

    def test_parse_plain_text_fallback_raw_text_populated(self):
        ctx = _ctx(ocr_text="Revenue: 500")
        parsed = self.handler.parse("", ctx)
        # _fallback_text comes from ocr_text when structured_raw is empty
        assert "Revenue: 500" in parsed.get("_fallback_text", "")

    def test_canonical_text_returns_markdown_for_structured(self):
        ctx = _ctx(detected_store="table_store")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        text = self.handler.canonical_text(parsed, ctx)
        assert "Quarter" in text
        assert "Revenue" in text
        assert "|" in text  # markdown table uses pipe chars

    def test_canonical_text_fallback_returns_ocr(self):
        ctx = _ctx(ocr_text="Revenue 500M")
        parsed = self.handler.parse("", ctx)
        text = self.handler.canonical_text(parsed, ctx)
        assert "Revenue 500M" in text

    def test_insert_sql_contains_from_image_store(self):
        ctx = _ctx(detected_store="table_store")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        sql, params, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "from_image_store" in sql
        assert "TRUE" in sql

    def test_insert_sql_contains_image_storage_path(self):
        ctx = _ctx(detected_store="table_store", storage_path="images/doc/3.png")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        sql, params, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "image_storage_path" in sql
        # storage_path from ctx should appear in the params tuple
        assert "images/doc/3.png" in params

    def test_insert_returns_rowcount_gte_1(self):
        ctx = _ctx(detected_store="table_store")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        _, _, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert rc >= 1

    def test_insert_uses_correct_table_index_offset(self):
        """table_index in params must be _IMAGE_TABLE_INDEX_OFFSET + image_index."""
        ctx = _ctx(image_index=7, detected_store="table_store")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        expected_index = _IMAGE_TABLE_INDEX_OFFSET + 7
        assert expected_index in params, f"Expected table_index {expected_index} in params"

    def test_insert_traceability_in_params(self):
        ctx = _ctx(detected_store="table_store", image_id="img-uuid-42")
        parsed = self.handler.parse(TABLE_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        # table_metadata is a JSON string in params; find it and verify traceability
        metadata_str = next(
            (p for p in params if isinstance(p, str) and "source_image_id" in p), None
        )
        assert metadata_str is not None, "No traceability JSON found in params"
        meta = json.loads(metadata_str)
        assert meta["source_image_id"] == "img-uuid-42"
        assert meta["source"] == "image"

    def test_validate_raises_when_row_missing(self):
        ctx = _ctx(detected_store="table_store")
        conn, cur = _make_conn(fetchone_return=None)
        with pytest.raises(ValueError, match="row missing"):
            self.handler.validate(conn, ctx)

    def test_validate_raises_when_embedding_null(self):
        ctx = _ctx(detected_store="table_store")
        conn, cur = _make_conn(fetchone_return=("some raw text", False))
        with pytest.raises(ValueError, match="embedding NULL"):
            self.handler.validate(conn, ctx)

    def test_validate_raises_when_raw_text_empty(self):
        ctx = _ctx(detected_store="table_store")
        conn, cur = _make_conn(fetchone_return=("", True))
        with pytest.raises(ValueError, match="raw_text empty"):
            self.handler.validate(conn, ctx)

    def test_validate_passes_on_good_row(self):
        ctx = _ctx(detected_store="table_store")
        conn, cur = _make_conn(fetchone_return=("some raw text", True))
        # Should not raise
        self.handler.validate(conn, ctx)


# ---------------------------------------------------------------------------
# ClauseStoreHandler
# ---------------------------------------------------------------------------

CLAUSE_JSON = json.dumps({
    "clause_title": "Indemnification",
    "clause_text": "Party A shall indemnify Party B against all claims.",
    "clause_type": "indemnification",
    "parties": ["Party A", "Party B"],
    "obligor": "Party A",
    "obligee": "Party B",
    "key_dates": {"effective_date": "2024-01-01", "expiry": "2027-01-01"},
    "monetary_values": {"amount": 500000, "currency": "USD"},
    "obligations": ["Party A must indemnify", "Party A must hold harmless"],
    "risk_level": "high",
    "risk_rationale": "Unlimited liability cap",
})


class TestClauseStoreHandler:
    handler = ClauseStoreHandler()

    def test_parse_json_clause_text(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        assert parsed["clause_text"] == "Party A shall indemnify Party B against all claims."
        assert parsed["clause_type"] == "indemnification"

    def test_parse_json_parties(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        assert "Party A" in parsed["parties"]
        assert "Party B" in parsed["parties"]

    def test_parse_json_key_dates(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        assert parsed["key_dates"]["effective_date"] == "2024-01-01"

    def test_parse_json_risk(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        assert parsed["risk_level"] == "high"
        assert "Unlimited" in parsed["risk_rationale"]

    def test_parse_plain_text_fallback_clause_text(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse("This is a plain-text clause.", ctx)
        assert parsed["clause_text"] == "This is a plain-text clause."
        assert parsed["clause_type"] == "general"
        assert parsed["parties"] == []

    def test_parse_plain_text_fallback_empty_collections(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse("plain text", ctx)
        assert parsed["key_dates"] == {}
        assert parsed["monetary_values"] == {}
        assert parsed["obligations"] == []

    def test_parse_json_missing_clause_text_prefers_structured_over_ocr(self):
        """When the VLM's JSON omits clause_text, fall back to the raw
        structured_content string (structured_raw), NOT ctx.ocr_text —
        the VLM's own extraction is richer than blind OCR."""
        raw = json.dumps({"clause_title": "No text field", "clause_type": "general"})
        ctx = _ctx(detected_store="clause_store", ocr_text="stale OCR text")
        parsed = self.handler.parse(raw, ctx)
        assert parsed["clause_text"] == raw
        assert "stale OCR text" not in parsed["clause_text"]

    def test_canonical_text_is_clause_text(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        text = self.handler.canonical_text(parsed, ctx)
        assert text == "Party A shall indemnify Party B against all claims."

    def test_canonical_text_fallback_prefers_structured_content_over_ocr(self):
        ctx = _ctx(
            detected_store="clause_store",
            ocr_text="stale OCR text",
            structured_content="rich VLM extraction",
        )
        parsed = {"clause_text": ""}
        assert self.handler.canonical_text(parsed, ctx) == "rich VLM extraction"

    def test_insert_sql_contains_from_image_store(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        sql, params, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "from_image_store" in sql
        assert "TRUE" in sql

    def test_insert_returns_rowcount_gte_1(self):
        ctx = _ctx(detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        _, _, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert rc >= 1

    def test_insert_uses_correct_clause_index_offset(self):
        ctx = _ctx(image_index=5, detected_store="clause_store")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        expected_index = _IMAGE_CHUNK_INDEX_OFFSET + 5
        assert expected_index in params

    def test_insert_traceability_source_image_id(self):
        ctx = _ctx(detected_store="clause_store", image_id="clause-img-99")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        metadata_str = next(
            (p for p in params if isinstance(p, str) and "source_image_id" in p), None
        )
        assert metadata_str is not None
        meta = json.loads(metadata_str)
        assert meta["source_image_id"] == "clause-img-99"

    def test_insert_sql_contains_source_image_id_column(self):
        ctx = _ctx(detected_store="clause_store", image_id="clause-img-77")
        parsed = self.handler.parse(CLAUSE_JSON, ctx)
        sql, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "source_image_id" in sql
        assert "clause-img-77" in params

    def test_validate_raises_when_row_missing(self):
        ctx = _ctx(detected_store="clause_store")
        conn, cur = _make_conn(fetchone_return=None)
        with pytest.raises(ValueError, match="row missing"):
            self.handler.validate(conn, ctx)

    def test_validate_raises_when_embedding_null(self):
        ctx = _ctx(detected_store="clause_store")
        conn, cur = _make_conn(fetchone_return=("clause text", False))
        with pytest.raises(ValueError, match="embedding NULL"):
            self.handler.validate(conn, ctx)

    def test_validate_passes_on_good_row(self):
        ctx = _ctx(detected_store="clause_store")
        conn, cur = _make_conn(fetchone_return=("clause text", True))
        self.handler.validate(conn, ctx)


# ---------------------------------------------------------------------------
# VectorStoreHandler
# ---------------------------------------------------------------------------

VECTOR_JSON = json.dumps({
    "text": "The annual revenue for FY2024 exceeded projections by 15%.",
    "section_title": "Financial Performance",
    "keywords": ["revenue", "FY2024", "projections"],
    "semantic_type": "paragraph",
})


class TestVectorStoreHandler:
    handler = VectorStoreHandler()

    def test_parse_json_chunk_text(self):
        ctx = _ctx(detected_store="vector_store")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        assert "annual revenue" in parsed["chunk_text"]
        assert parsed["section_title"] == "Financial Performance"
        assert parsed["semantic_type"] == "paragraph"

    def test_parse_json_keywords(self):
        ctx = _ctx(detected_store="vector_store")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        assert "revenue" in parsed["keywords"]

    def test_parse_plain_text_fallback(self):
        ctx = _ctx(detected_store="vector_store")
        parsed = self.handler.parse("plain text content here", ctx)
        assert parsed["chunk_text"] == "plain text content here"
        assert parsed["semantic_type"] == "image_text"
        assert parsed["keywords"] == []

    def test_parse_empty_fallback_uses_ocr(self):
        ctx = _ctx(detected_store="vector_store", ocr_text="OCR extracted text")
        parsed = self.handler.parse("", ctx)
        assert parsed["chunk_text"] == "OCR extracted text"

    def test_parse_json_missing_text_prefers_structured_over_ocr(self):
        """When the VLM's JSON omits "text", fall back to the raw
        structured_content string, NOT ctx.ocr_text."""
        raw = json.dumps({"section_title": "No text field", "semantic_type": "caption"})
        ctx = _ctx(detected_store="vector_store", ocr_text="stale OCR text")
        parsed = self.handler.parse(raw, ctx)
        assert parsed["chunk_text"] == raw
        assert "stale OCR text" not in parsed["chunk_text"]

    def test_canonical_text_is_chunk_text(self):
        ctx = _ctx(detected_store="vector_store")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        assert self.handler.canonical_text(parsed, ctx) == parsed["chunk_text"]

    def test_canonical_text_fallback_prefers_structured_content_over_ocr(self):
        ctx = _ctx(
            detected_store="vector_store",
            ocr_text="stale OCR text",
            structured_content="rich VLM extraction",
        )
        parsed = {"chunk_text": ""}
        assert self.handler.canonical_text(parsed, ctx) == "rich VLM extraction"

    def test_insert_sql_contains_from_image_store(self):
        ctx = _ctx(detected_store="vector_store")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        sql, params, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "from_image_store" in sql
        assert "TRUE" in sql

    def test_insert_returns_rowcount_gte_1(self):
        ctx = _ctx(detected_store="vector_store")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        _, _, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert rc >= 1

    def test_insert_uses_correct_chunk_index_offset(self):
        ctx = _ctx(image_index=2, detected_store="vector_store")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        expected_index = _IMAGE_CHUNK_INDEX_OFFSET + 2
        assert expected_index in params

    def test_insert_traceability_source_image_id(self):
        ctx = _ctx(detected_store="vector_store", image_id="vec-img-7")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        metadata_str = next(
            (p for p in params if isinstance(p, str) and "source_image_id" in p), None
        )
        assert metadata_str is not None
        meta = json.loads(metadata_str)
        assert meta["source_image_id"] == "vec-img-7"

    def test_insert_sql_contains_source_image_id_column(self):
        ctx = _ctx(detected_store="vector_store", image_id="vec-img-33")
        parsed = self.handler.parse(VECTOR_JSON, ctx)
        sql, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "source_image_id" in sql
        assert "vec-img-33" in params

    def test_validate_raises_when_row_missing(self):
        ctx = _ctx(detected_store="vector_store")
        conn, cur = _make_conn(fetchone_return=None)
        with pytest.raises(ValueError, match="row missing"):
            self.handler.validate(conn, ctx)

    def test_validate_raises_when_embedding_null(self):
        ctx = _ctx(detected_store="vector_store")
        conn, cur = _make_conn(fetchone_return=("chunk text", False))
        with pytest.raises(ValueError, match="embedding NULL"):
            self.handler.validate(conn, ctx)

    def test_validate_passes_on_good_row(self):
        ctx = _ctx(detected_store="vector_store")
        conn, cur = _make_conn(fetchone_return=("some chunk text", True))
        self.handler.validate(conn, ctx)


# ---------------------------------------------------------------------------
# DocumentStoreHandler
# ---------------------------------------------------------------------------

DOCUMENT_JSON = json.dumps({
    "chunk_text": "This study demonstrates a novel CRISPR-based gene editing approach.",
    "chunk_type": "results",
    "section_title": "Results and Discussion",
    "citation": {
        "key": "smith2024crispr",
        "title": "CRISPR advances in gene therapy",
        "authors": ["Alice Smith", "Bob Jones"],
        "year": 2024,
        "doi": "10.1234/crispr.2024",
        "url": "https://doi.org/10.1234/crispr.2024",
        "journal": "Nature Biotechnology",
        "confidence": 0.93,
    },
    "entities": ["CRISPR", "gene editing", "therapy"],
})


class TestDocumentStoreHandler:
    handler = DocumentStoreHandler()

    def test_parse_json_chunk_text(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        assert "CRISPR" in parsed["chunk_text"]
        assert parsed["chunk_type"] == "results"

    def test_parse_json_citation_fields(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        assert parsed["citation_key"] == "smith2024crispr"
        assert parsed["source_title"] == "CRISPR advances in gene therapy"
        assert "Alice Smith" in parsed["source_authors"]
        assert parsed["source_year"] == 2024
        assert parsed["source_doi"] == "10.1234/crispr.2024"
        assert parsed["source_journal"] == "Nature Biotechnology"
        assert abs(parsed["source_confidence"] - 0.93) < 1e-6

    def test_parse_json_entities(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        assert "CRISPR" in parsed["entities"]

    def test_parse_plain_text_fallback(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse("plain text paragraph here", ctx)
        assert parsed["chunk_text"] == "plain text paragraph here"
        assert parsed["chunk_type"] == "body"
        assert parsed["citation_key"] is None
        assert parsed["source_authors"] == []
        assert parsed["entities"] == []

    def test_parse_empty_fallback_uses_ocr(self):
        ctx = _ctx(detected_store="vector_store", ocr_text="document finding here")
        parsed = self.handler.parse("", ctx)
        assert parsed["chunk_text"] == "document finding here"

    def test_parse_json_missing_chunk_text_prefers_structured_over_ocr(self):
        """When the VLM's JSON omits chunk_text, fall back to the raw
        structured_content string, NOT ctx.ocr_text."""
        raw = json.dumps({"chunk_type": "results", "entities": ["X"]})
        ctx = _ctx(detected_store="document_store", ocr_text="stale OCR text")
        parsed = self.handler.parse(raw, ctx)
        assert parsed["chunk_text"] == raw
        assert "stale OCR text" not in parsed["chunk_text"]

    def test_canonical_text_is_chunk_text(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        assert self.handler.canonical_text(parsed, ctx) == parsed["chunk_text"]

    def test_canonical_text_fallback_prefers_structured_content_over_ocr(self):
        ctx = _ctx(
            detected_store="document_store",
            ocr_text="stale OCR text",
            structured_content="rich VLM extraction",
        )
        parsed = {"chunk_text": ""}
        assert self.handler.canonical_text(parsed, ctx) == "rich VLM extraction"

    def test_insert_sql_contains_from_image_store(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        sql, params, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "from_image_store" in sql
        assert "TRUE" in sql

    def test_insert_sql_contains_source_columns(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        sql, params, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "source_title" in sql
        assert "source_authors" in sql
        assert "citation_key" in sql

    def test_insert_returns_rowcount_gte_1(self):
        ctx = _ctx(detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        _, _, rc = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert rc >= 1

    def test_insert_uses_correct_chunk_index_offset(self):
        ctx = _ctx(image_index=9, detected_store="document_store")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        expected_index = _IMAGE_CHUNK_INDEX_OFFSET + 9
        assert expected_index in params

    def test_insert_traceability_source_image_id(self):
        ctx = _ctx(detected_store="document_store", image_id="doc-img-55")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        _, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        metadata_str = next(
            (p for p in params if isinstance(p, str) and "source_image_id" in p), None
        )
        assert metadata_str is not None
        meta = json.loads(metadata_str)
        assert meta["source_image_id"] == "doc-img-55"
        assert meta["source"] == "image"

    def test_insert_sql_contains_source_image_id_column(self):
        ctx = _ctx(detected_store="document_store", image_id="doc-img-88")
        parsed = self.handler.parse(DOCUMENT_JSON, ctx)
        sql, params, _ = _insert_sql_and_params(self.handler, parsed, _fake_embedding(), ctx)
        assert "source_image_id" in sql
        assert "doc-img-88" in params

    def test_validate_raises_when_row_missing(self):
        ctx = _ctx(detected_store="document_store")
        conn, cur = _make_conn(fetchone_return=None)
        with pytest.raises(ValueError, match="row missing"):
            self.handler.validate(conn, ctx)

    def test_validate_raises_when_embedding_null(self):
        ctx = _ctx(detected_store="document_store")
        conn, cur = _make_conn(fetchone_return=("chunk text", False))
        with pytest.raises(ValueError, match="embedding NULL"):
            self.handler.validate(conn, ctx)

    def test_validate_passes_on_good_row(self):
        ctx = _ctx(detected_store="document_store")
        conn, cur = _make_conn(fetchone_return=("some chunk text", True))
        self.handler.validate(conn, ctx)


# ---------------------------------------------------------------------------
# Traceability cross-cutting tests
# ---------------------------------------------------------------------------

class TestTraceability:
    """Verify that every handler's insert() puts source_image_id in *_metadata."""

    @pytest.mark.parametrize("store,raw_json,handler_cls", [
        ("table_store", TABLE_JSON, TableStoreHandler),
        ("clause_store", CLAUSE_JSON, ClauseStoreHandler),
        ("vector_store", VECTOR_JSON, VectorStoreHandler),
        ("document_store", DOCUMENT_JSON, DocumentStoreHandler),
    ])
    def test_traceability_dict_has_source_image_id(self, store, raw_json, handler_cls):
        handler = handler_cls()
        ctx = _ctx(detected_store=store, image_id=f"traceability-{store}-uuid")
        parsed = handler.parse(raw_json, ctx)
        _, params, _ = _insert_sql_and_params(handler, parsed, _fake_embedding(), ctx)
        metadata_str = next(
            (p for p in params if isinstance(p, str) and "source_image_id" in p), None
        )
        assert metadata_str is not None, f"No traceability JSON found for {store}"
        meta = json.loads(metadata_str)
        assert meta["source_image_id"] == f"traceability-{store}-uuid"
        assert meta["source"] == "image"
        assert meta["image_index"] == ctx.image_index
        assert meta["page_number"] == ctx.page_number
        assert meta["detected_store"] == store
        assert meta["confidence"] == ctx.confidence
        assert "reason_for_store_selection" in meta

    @pytest.mark.parametrize("store,raw_json,handler_cls", [
        ("table_store", TABLE_JSON, TableStoreHandler),
        ("clause_store", CLAUSE_JSON, ClauseStoreHandler),
        ("vector_store", VECTOR_JSON, VectorStoreHandler),
        ("document_store", DOCUMENT_JSON, DocumentStoreHandler),
    ])
    def test_parse_never_raises_on_garbage_input(self, store, raw_json, handler_cls):
        handler = handler_cls()
        ctx = _ctx(detected_store=store)
        # Should not raise on empty, null-like, or malformed input
        for bad in ("", "   ", "{not valid json", "null", "[]", "123"):
            parsed = handler.parse(bad, ctx)
            assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# _try_json helper tests (internal but exercised indirectly — test directly too)
# ---------------------------------------------------------------------------

class TestTryJson:
    def test_returns_none_for_empty(self):
        from app.services.store_router import _try_json
        assert _try_json("") is None

    def test_returns_dict_for_valid_json(self):
        from app.services.store_router import _try_json
        result = _try_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_strips_json_fence(self):
        from app.services.store_router import _try_json
        raw = "```json\n{\"key\": \"value\"}\n```"
        result = _try_json(raw)
        assert result == {"key": "value"}

    def test_strips_plain_fence(self):
        from app.services.store_router import _try_json
        raw = "```\n{\"key\": \"value\"}\n```"
        result = _try_json(raw)
        assert result == {"key": "value"}

    def test_returns_none_for_array(self):
        from app.services.store_router import _try_json
        # Top-level arrays are not dicts — should return None
        assert _try_json("[1, 2, 3]") is None

    def test_returns_none_for_invalid_json(self):
        from app.services.store_router import _try_json
        assert _try_json("{bad json}") is None
