from unittest.mock import patch
from app.services.retriever_service import RetrievedChunk
import app.api.routes.query as q


def _chunk(**kw):
    base = dict(chunk_id="c1", document_id="d1", text="t", store_type="vector",
                distance=0.1, document_filename="r.pdf", relevance_score=0.8,
                page_number=7, pdf_storage_path="documents/d1.pdf", pdf_bucket="rag-documents",
                bbox={"x1": 1, "y1": 2, "x2": 3, "y2": 4})
    base.update(kw)
    return RetrievedChunk(**base)


def test_citation_pdf_url_is_page_anchored():
    with patch.object(q, "create_signed_url", return_value="https://signed/d1.pdf?token=x") as m:
        item = q._citation_from_chunk(_chunk(), bucket="rag-documents")
    m.assert_called_once_with("rag-documents", "documents/d1.pdf")
    assert item.pdf_url == "https://signed/d1.pdf?token=x#page=7"
    assert item.bbox == {"x1": 1, "y1": 2, "x2": 3, "y2": 4}


def test_citation_pdf_url_no_page_anchor_when_page_unknown():
    with patch.object(q, "create_signed_url", return_value="https://signed/d1.pdf?token=x"):
        item = q._citation_from_chunk(_chunk(page_number=None), bucket="rag-documents")
    assert item.pdf_url == "https://signed/d1.pdf?token=x"


def test_citation_pdf_url_failure_is_non_fatal():
    with patch.object(q, "create_signed_url", side_effect=RuntimeError("boom")):
        item = q._citation_from_chunk(_chunk(), bucket="rag-documents")
    assert item.pdf_url is None
    assert item.bbox == {"x1": 1, "y1": 2, "x2": 3, "y2": 4}
