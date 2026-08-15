"""Tests for analyze_image (replaced the removed describe_image function)."""
import json
from unittest.mock import MagicMock, patch
import app.services.image_analysis_service as ias


def _gemma_response(content: str):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# analyze_image — happy path
# ---------------------------------------------------------------------------

def test_analyze_image_parses_all_new_keys():
    # Structured table content (JSON rows) is classified structured_table -> table_store.
    sc = json.dumps({"headers": ["Q", "Rev"], "rows": [["Q1", "100"], ["Q2", "200"]]})
    payload = json.dumps({
        "structured_content": sc,
        "ocr_text": "Q1 100 Q2 200",
        "detected_store": "Table Store",
        "confidence": 0.92,
        "reason_for_store_selection": "Contains tabular financial data",
    })
    client = MagicMock()
    client.__enter__.return_value.post.return_value = _gemma_response(payload)
    with patch.object(ias, "httpx") as httpx_mod:
        httpx_mod.Client.return_value = client
        with patch("app.config.settings") as s:
            s.GEMMA4_BASE_URL = "http://gemma/v1"
            s.GEMMA4_API_KEY = ""
            s.GEMMA4_MODEL_NAME = "gemma-4-27b-it"
            s.GEMMA4_TIMEOUT_SECONDS = 30
            out = ias.analyze_image(b"\x89PNG-fake", "Q1 100 Q2 200")
    assert out["structured_content"] == sc
    assert out["vlm_ocr_text"] == "Q1 100 Q2 200"
    assert out["detected_store"] == "table_store"
    assert abs(out["confidence"] - 0.92) < 1e-6
    assert "tabular" in out["reason_for_store_selection"].lower()
    assert out["content_type"] == "table"


def test_analyze_image_vector_store_detection():
    payload = json.dumps({
        "structured_content": "Policy section about data governance",
        "detected_store": "Normal Chunk Store",
        "confidence": 0.85,
        "reason_for_store_selection": "Plain text content",
    })
    client = MagicMock()
    client.__enter__.return_value.post.return_value = _gemma_response(payload)
    with patch.object(ias, "httpx") as httpx_mod:
        httpx_mod.Client.return_value = client
        with patch("app.config.settings") as s:
            s.GEMMA4_BASE_URL = "http://gemma/v1"
            s.GEMMA4_API_KEY = ""
            s.GEMMA4_MODEL_NAME = "gemma-4-27b-it"
            s.GEMMA4_TIMEOUT_SECONDS = 30
            out = ias.analyze_image(b"\x89PNG-fake", "some ocr text")
    assert out["detected_store"] == "vector_store"
    assert out["content_type"] == "text"


def test_analyze_image_clause_store_detection():
    payload = json.dumps({
        "structured_content": "Clause 3.2: Termination rights",
        "detected_store": "Clause Store",
        "confidence": 0.78,
        "reason_for_store_selection": "Legal clause content",
    })
    client = MagicMock()
    client.__enter__.return_value.post.return_value = _gemma_response(payload)
    with patch.object(ias, "httpx") as httpx_mod:
        httpx_mod.Client.return_value = client
        with patch("app.config.settings") as s:
            s.GEMMA4_BASE_URL = "http://gemma/v1"
            s.GEMMA4_API_KEY = ""
            s.GEMMA4_MODEL_NAME = "gemma-4-27b-it"
            s.GEMMA4_TIMEOUT_SECONDS = 30
            out = ias.analyze_image(b"\x89PNG-fake", "Clause 3.2")
    assert out["detected_store"] == "clause_store"
    assert out["content_type"] == "text"


# ---------------------------------------------------------------------------
# analyze_image — no endpoint fallback
# ---------------------------------------------------------------------------

def test_analyze_image_no_endpoint_returns_fallback():
    raw_ocr = "some raw ocr text"
    with patch("app.config.settings") as s:
        s.GEMMA4_BASE_URL = ""
        out = ias.analyze_image(b"\x89PNG-fake", raw_ocr)
    assert out["structured_content"] == raw_ocr
    # A kept image stays searchable: OCR text routes to vector_store, not image_store.
    assert out["detected_store"] == "vector_store"
    assert out["confidence"] == 0.0
    assert "reason_for_store_selection" in out
    assert out["content_type"] == "text"


def test_analyze_image_no_endpoint_empty_ocr_returns_empty_structured_content():
    with patch("app.config.settings") as s:
        s.GEMMA4_BASE_URL = ""
        out = ias.analyze_image(b"\x89PNG-fake", "")
    assert out["structured_content"] == ""
    # No VLM decision AND no meaningful OCR text -> nothing searchable, so it stays a
    # pure repository asset in image_store (not forced into vector_store).
    assert out["detected_store"] == "image_store"


# ---------------------------------------------------------------------------
# _canonical_store mapping
# ---------------------------------------------------------------------------

def test_canonical_store_table():
    assert ias._canonical_store("Table Store") == "table_store"
    assert ias._canonical_store("table_store") == "table_store"
    assert ias._canonical_store("TABLE") == "table_store"


def test_canonical_store_vector():
    assert ias._canonical_store("Normal Chunk Store") == "vector_store"
    assert ias._canonical_store("text_store") == "vector_store"
    assert ias._canonical_store("chunk") == "vector_store"


def test_canonical_store_clause():
    assert ias._canonical_store("Clause Store") == "clause_store"
    assert ias._canonical_store("clause_store") == "clause_store"


def test_canonical_store_image():
    assert ias._canonical_store("Image Store") == "image_store"
    assert ias._canonical_store("image_store") == "image_store"


def test_canonical_store_unknown_falls_back_to_image_store():
    assert ias._canonical_store("unknown_store") == "image_store"
    assert ias._canonical_store("") == "image_store"
    assert ias._canonical_store("something_random") == "image_store"
