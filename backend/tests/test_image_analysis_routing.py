"""Tests for image_analysis_service document_store routing and JSON-object structured_content.

Covers:
- _canonical_store document synonym mappings (document, research, paper, citation, scientific)
- _canonical_store ordering — document synonyms win over vector_store text-trigger words
- _content_type_from_store document_store entry
- analyze_image: Table Store response with JSON-object structured_content round-trips correctly
- analyze_image: Document Store response maps to document_store
- analyze_image: VLM-unavailable returns image_store fallback
- _VLM_PROMPT contains the store_router schema block (table_store schema_hint present)
- Schema block from build_vlm_schema_block() is present verbatim in _VLM_PROMPT
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import app.services.image_analysis_service as ias
from app.services.store_router import build_vlm_schema_block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gemma_response(content: str):
    """Build a minimal mock httpx response that returns *content* as VLM text."""
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


def _patch_httpx(response_content: str):
    """Context manager: patches ias.httpx so the VLM returns *response_content*."""
    client = MagicMock()
    client.__enter__.return_value.post.return_value = _gemma_response(response_content)
    return patch.object(ias, "httpx", **{"Client.return_value": client})


def _settings_ctx(base_url="http://gemma/v1"):
    """Patch app.config.settings with minimal VLM config."""
    return patch(
        "app.config.settings",
        GEMMA4_BASE_URL=base_url,
        GEMMA4_API_KEY="",
        GEMMA4_MODEL_NAME="gemma-4-27b-it",
        GEMMA4_TIMEOUT_SECONDS=30,
    )


# ---------------------------------------------------------------------------
# _canonical_store — document synonyms
# ---------------------------------------------------------------------------


class TestCanonicalStoreDocumentSynonyms:
    """_canonical_store maps all document-store synonyms to document_store."""

    def test_document_store_literal(self):
        assert ias._canonical_store("Document Store") == "document_store"

    def test_document_lowercase(self):
        assert ias._canonical_store("document") == "document_store"

    def test_document_snake(self):
        assert ias._canonical_store("document_store") == "document_store"

    def test_research(self):
        assert ias._canonical_store("Research Store") == "document_store"

    def test_paper(self):
        assert ias._canonical_store("Paper Store") == "document_store"

    def test_citation(self):
        assert ias._canonical_store("Citation Store") == "document_store"

    def test_scientific(self):
        assert ias._canonical_store("Scientific Document Store") == "document_store"

    def test_upper(self):
        assert ias._canonical_store("DOCUMENT STORE") == "document_store"


class TestCanonicalStoreOrdering:
    """Ordering: table > clause > document > vector > image.

    'Document Store' must NOT fall through to vector_store even though it
    does not contain 'text'/'chunk'/'normal' — the document branch fires first.
    """

    def test_table_wins_over_clause(self):
        # edge case: both 'table' and 'clause' in raw → table_store wins
        assert ias._canonical_store("Table Clause Store") == "table_store"

    def test_document_wins_over_vector(self):
        # 'research' before 'text' — document_store must win
        assert ias._canonical_store("research text") == "document_store"

    def test_plain_text_still_vector(self):
        assert ias._canonical_store("Normal Chunk Store") == "vector_store"
        assert ias._canonical_store("text_store") == "vector_store"
        assert ias._canonical_store("vector store") == "vector_store"

    def test_unknown_falls_back_to_image_store(self):
        assert ias._canonical_store("something_random") == "image_store"
        assert ias._canonical_store("") == "image_store"
        assert ias._canonical_store("Image Store") == "image_store"


# ---------------------------------------------------------------------------
# _content_type_from_store
# ---------------------------------------------------------------------------


class TestContentTypeFromStore:
    def test_document_store_returns_text(self):
        assert ias._content_type_from_store("document_store") == "text"

    def test_existing_mappings_unchanged(self):
        assert ias._content_type_from_store("table_store") == "table"
        assert ias._content_type_from_store("vector_store") == "text"
        assert ias._content_type_from_store("clause_store") == "text"
        assert ias._content_type_from_store("image_store") == "figure"

    def test_unknown_defaults_to_figure(self):
        assert ias._content_type_from_store("unknown_store") == "figure"


# ---------------------------------------------------------------------------
# _VLM_PROMPT — schema block injection
# ---------------------------------------------------------------------------


class TestVlmPromptSchemaBlock:
    """Confirm the assembled prompt contains the store_router schema hints."""

    def test_prompt_contains_schema_block_header(self):
        assert "=== Structured Content Schemas by Destination Store ===" in ias._VLM_PROMPT

    def test_prompt_contains_table_store_hint(self):
        # table_store schema_hint mentions 'headers'
        assert "table_store" in ias._VLM_PROMPT
        assert "headers" in ias._VLM_PROMPT

    def test_prompt_contains_document_store_hint(self):
        # document_store schema_hint mentions 'chunk_text'
        assert "document_store" in ias._VLM_PROMPT
        assert "chunk_text" in ias._VLM_PROMPT

    def test_prompt_contains_clause_store_hint(self):
        assert "clause_store" in ias._VLM_PROMPT
        assert "clause_text" in ias._VLM_PROMPT

    def test_prompt_contains_vector_store_hint(self):
        assert "vector_store" in ias._VLM_PROMPT

    def test_schema_block_verbatim_in_prompt(self):
        """build_vlm_schema_block() output must appear verbatim in _VLM_PROMPT."""
        schema_block = build_vlm_schema_block()
        # The block has multiple lines; check a distinctive line is present
        first_meaningful_line = next(
            line for line in schema_block.splitlines() if line.strip()
        )
        assert first_meaningful_line in ias._VLM_PROMPT

    def test_prompt_lists_document_store_as_option(self):
        assert "Document Store" in ias._VLM_PROMPT

    def test_detected_store_enum_includes_document_store(self):
        # The response format section must enumerate Document Store
        assert '"Document Store"' in ias._VLM_PROMPT


# ---------------------------------------------------------------------------
# analyze_image — Table Store with JSON-object structured_content
# ---------------------------------------------------------------------------


class TestAnalyzeImageTableStoreJsonObject:
    """When the VLM returns structured_content as a JSON object (dict), analyze_image
    must json.dumps it so the downstream parser (store_router.TableStoreHandler.parse)
    can round-trip it back to a dict."""

    _TABLE_SC = {
        "title": "Revenue Summary",
        "headers": ["Quarter", "Revenue"],
        "rows": [["Q1", "100"], ["Q2", "200"]],
        "units": "USD millions",
        "fiscal_year": "FY2024",
        "reporting_period": "H1",
        "currency": "USD",
        "table_category": "income_statement",
        "notes": "Unaudited",
    }

    def _vlm_payload(self):
        return json.dumps({
            "detected_store": "Table Store",
            "structured_content": self._TABLE_SC,
            "ocr_text": "Q1 100 Q2 200",
            "confidence": 0.95,
            "reason_for_store_selection": "Contains tabular financial data",
        })

    def test_detected_store_is_table_store(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "Q1 100 Q2 200")
        assert out["detected_store"] == "table_store"

    def test_structured_content_is_json_string(self):
        """structured_content must be a JSON string (not a dict) for downstream parsers."""
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "Q1 100 Q2 200")
        assert isinstance(out["structured_content"], str), (
            "structured_content must be a JSON string so downstream parsers can re-parse it"
        )

    def test_structured_content_round_trips_to_original_object(self):
        """json.loads(structured_content) must equal the original dict the VLM returned."""
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "Q1 100 Q2 200")
        recovered = json.loads(out["structured_content"])
        assert recovered == self._TABLE_SC

    def test_content_type_is_table(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "Q1 100 Q2 200")
        assert out["content_type"] == "table"

    def test_confidence_and_reason_preserved(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "Q1 100 Q2 200")
        assert abs(out["confidence"] - 0.95) < 1e-6
        assert "tabular" in out["reason_for_store_selection"].lower()


# ---------------------------------------------------------------------------
# analyze_image — Document Store routing
# ---------------------------------------------------------------------------


class TestAnalyzeImageDocumentStore:
    """When the VLM selects 'Document Store', detected_store must be document_store."""

    _DOC_SC = {
        "chunk_text": "CRISPR-Cas9 enables precise genome editing in mammalian cells.",
        "chunk_type": "results",
        "section_title": "Results",
        "citation": {
            "key": "zhang2013",
            "title": "Multiplex Genome Engineering Using CRISPR/Cas Systems",
            "authors": ["Feng Zhang"],
            "year": 2013,
            "doi": "10.1126/science.1231143",
            "url": None,
            "journal": "Science",
            "confidence": 0.98,
        },
        "entities": ["CRISPR", "Cas9", "genome"],
    }

    def _vlm_payload(self):
        return json.dumps({
            "detected_store": "Document Store",
            "structured_content": self._DOC_SC,
            "ocr_text": "CRISPR-Cas9 enables precise genome editing.",
            "confidence": 0.88,
            "reason_for_store_selection": "Academic research passage with citation",
        })

    def test_detected_store_is_document_store(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "")
        assert out["detected_store"] == "document_store"

    def test_content_type_is_text(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "")
        assert out["content_type"] == "text"

    def test_structured_content_round_trips_to_document_schema(self):
        """structured_content JSON string must round-trip back to the document dict."""
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "")
        recovered = json.loads(out["structured_content"])
        assert recovered["chunk_text"] == self._DOC_SC["chunk_text"]
        assert recovered["chunk_type"] == "results"
        assert "CRISPR" in recovered["entities"]

    def test_confidence_preserved(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "")
        assert abs(out["confidence"] - 0.88) < 1e-6


# ---------------------------------------------------------------------------
# analyze_image — VLM-unavailable fallback
# ---------------------------------------------------------------------------


class TestAnalyzeImageFallback:
    """When GEMMA4_BASE_URL is empty, analyze_image still keeps the kept image
    searchable: it routes the raw OCR text to vector_store (never image_store,
    which is a pure repository for skipped images)."""

    def test_returns_vector_store(self):
        with patch("app.config.settings", GEMMA4_BASE_URL=""):
            out = ias.analyze_image(b"\x89PNG", "ocr text here")
        assert out["detected_store"] == "vector_store"
        assert out["confidence"] == 0.0
        assert out["content_type"] == "text"

    def test_structured_content_is_raw_ocr(self):
        raw_ocr = "fallback ocr text"
        with patch("app.config.settings", GEMMA4_BASE_URL=""):
            out = ias.analyze_image(b"\x89PNG", raw_ocr)
        assert out["structured_content"] == raw_ocr

    def test_vlm_ocr_text_is_empty(self):
        with patch("app.config.settings", GEMMA4_BASE_URL=""):
            out = ias.analyze_image(b"\x89PNG", "some ocr")
        assert out["vlm_ocr_text"] == ""

    def test_all_required_keys_present(self):
        with patch("app.config.settings", GEMMA4_BASE_URL=""):
            out = ias.analyze_image(b"\x89PNG", "")
        expected_keys = {
            "structured_content",
            "vlm_ocr_text",
            "detected_store",
            "confidence",
            "reason_for_store_selection",
            "content_type",
        }
        assert expected_keys.issubset(out.keys())


# ---------------------------------------------------------------------------
# analyze_image — plain-text structured_content (string from VLM) kept as-is
# ---------------------------------------------------------------------------


class TestAnalyzeImageStringStructuredContent:
    """When VLM emits structured_content as a plain string (not JSON), keep it as-is."""

    def _vlm_payload(self):
        return json.dumps({
            "detected_store": "Normal Chunk Store",
            "structured_content": "Policy section about data governance and retention.",
            "ocr_text": "data governance",
            "confidence": 0.75,
            "reason_for_store_selection": "Plain text paragraph",
        })

    def test_string_structured_content_preserved(self):
        client = MagicMock()
        client.__enter__.return_value.post.return_value = _gemma_response(self._vlm_payload())
        with patch.object(ias, "httpx", **{"Client.return_value": client}):
            with _settings_ctx():
                out = ias.analyze_image(b"\x89PNG", "data governance")
        assert out["structured_content"] == "Policy section about data governance and retention."
        assert out["detected_store"] == "vector_store"


# ---------------------------------------------------------------------------
# analyze_image — routing floor (kept images never resolve to image_store)
# ---------------------------------------------------------------------------


class TestAnalyzeImageSemanticRouting:
    """Content-driven routing (image_router.decide_route):
      - a real store the VLM picks wins;
      - an explicit "image" verdict is HONOURED (non-semantic figure -> repository);
      - unknown/empty store routes to vector_store only when the text is searchable,
        otherwise stays in image_store. Kept content is never orphaned, and genuinely
        non-semantic images are never forced into vector_store."""

    def _payload(self, store, sc="A pie chart of revenue by region.", ocr="revenue by region"):
        return json.dumps({
            "detected_store": store,
            "structured_content": sc,
            "ocr_text": ocr,
            "confidence": 0.8,
            "reason_for_store_selection": "figure",
        })

    def test_vlm_image_verdict_overridden_when_content_searchable(self):
        # VLMs mislabel informative charts as "Image Store"; with real content the
        # figure must stay retrievable -> vector_store, never orphaned in image_store.
        with _patch_httpx(self._payload("Image Store")), _settings_ctx():
            out = ias.analyze_image(b"\x89PNG", "revenue by region")
        assert out["detected_store"] == "vector_store"

    def test_unknown_store_with_searchable_text_goes_to_vector(self):
        with _patch_httpx(self._payload("Figure")), _settings_ctx():
            out = ias.analyze_image(b"\x89PNG", "x")
        assert out["detected_store"] == "vector_store"

    def test_empty_store_with_searchable_text_goes_to_vector(self):
        with _patch_httpx(self._payload("")), _settings_ctx():
            out = ias.analyze_image(b"\x89PNG", "x")
        assert out["detected_store"] == "vector_store"

    # NB: the "unknown store + no searchable text -> image_store" branch is covered
    # in test_image_router (decide_route). At the analyze_image level it can't be
    # exercised: analyze_image backfills empty structured_content with the raw VLM
    # response text, so there is always some content to consider.

    def test_structured_table_content_goes_to_table_store(self):
        # Real structured table content (JSON rows) is classified structured_table.
        sc = json.dumps({"headers": ["Q", "Rev"], "rows": [["Q1", "100"], ["Q2", "200"]]})
        with _patch_httpx(self._payload("Table Store", sc=sc)), _settings_ctx():
            out = ias.analyze_image(b"\x89PNG", "x")
        assert out["detected_store"] == "table_store"

    def test_table_label_without_structure_goes_to_vector(self):
        # VLM claims a table but emits only prose (no rows) -> mixed -> vector_store.
        with _patch_httpx(self._payload("Table Store", sc="just a paragraph of prose here")), _settings_ctx():
            out = ias.analyze_image(b"\x89PNG", "x")
        assert out["detected_store"] == "vector_store"
