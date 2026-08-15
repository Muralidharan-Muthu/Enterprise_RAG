"""Tests for _citation_from_chunk with image chunks under the new contract.

In the new contract, caption comes from c.caption which is None for image chunks
(structured_content drives c.text; caption field on RetrievedChunk is set to None
by _rows_to_image_chunks).
"""
from unittest.mock import patch
from app.services.retriever_service import RetrievedChunk
import app.api.routes.query as q


def _img_chunk():
    return RetrievedChunk(
        chunk_id="i1", document_id="d1",
        text="Revenue chart showing Q1 and Q2 figures\nQ1 100",
        store_type="image", distance=0.1, document_filename="r.pdf",
        document_type="financial", relevance_score=0.9, page_number=4,
        image_storage_path="images/d1/0.png",
        caption=None,           # new contract: caption is None for image chunks
        ocr_text="Q1 100",
    )


def test_citation_from_image_chunk_has_signed_url():
    with patch.object(q, "create_signed_url", return_value="https://signed/url") as m:
        item = q._citation_from_chunk(_img_chunk(), bucket="rag-documents")
    assert item.store_type == "image"
    assert item.image_url == "https://signed/url"
    # caption is None in the new contract
    assert item.caption is None
    assert item.ocr_text == "Q1 100"
    m.assert_called_once_with("rag-documents", "images/d1/0.png")


def test_citation_signed_url_failure_is_non_fatal():
    with patch.object(q, "create_signed_url", side_effect=RuntimeError("boom")):
        item = q._citation_from_chunk(_img_chunk(), bucket="rag-documents")
    assert item.image_url is None
    # caption is None in the new contract
    assert item.caption is None


def test_citation_chunk_text_contains_structured_content():
    """chunk_text in CitationItem is c.text which is built from structured_content."""
    with patch.object(q, "create_signed_url", return_value="https://signed/url"):
        item = q._citation_from_chunk(_img_chunk(), bucket="rag-documents")
    assert "Revenue chart showing Q1 and Q2 figures" in item.chunk_text
