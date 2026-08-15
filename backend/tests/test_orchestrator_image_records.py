"""Tests for _build_image_records: record-building contract when an image is
routed to the VLM. The pre-VLM filter is patched to a VLM_PROCESSED decision so
these tests exercise record assembly (the filter heuristics live in
test_image_prefilter.py)."""
from unittest.mock import patch
from app.models.document import ParsedDocument, ExtractedImage, BoundingBox
import app.services.ingestion_orchestrator as orch
from app.services.image_prefilter import PrefilterDecision, STATUS_VLM


def _doc_with_one_image():
    return ParsedDocument(
        doc_id="d1", filename="f.pdf", raw_text="", text_blocks=[], tables=[],
        page_count=1, word_count=0, has_tables=False, has_images=True,
        images=[ExtractedImage(image_index=0, page_number=2,
                               bbox=BoundingBox(1, 2, 3, 4),
                               png_bytes=b"\x89PNG", width=10, height=8)],
    )


def _vlm_decision():
    return PrefilterDecision(STATUS_VLM, "table", True, None, "decision_engine", {"n_colors": 30})


_ANALYSIS_RESULT = {
    "structured_content": "Revenue table: Q1=100 Q2=200",
    "vlm_ocr_text": "Q1 100 Q2 200 (vlm)",
    "detected_store": "table_store",
    "confidence": 0.91,
    "reason_for_store_selection": "Tabular financial data",
    "content_type": "table",
}


def test_build_image_records_new_contract():
    """Records carry content fields + the prefilter tracking fields, no caption."""
    with patch.object(orch, "upload_file", return_value="images/d1/0.png"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "Q1 100 Q2 200")), \
         patch("app.services.image_analysis_service.analyze_image",
               return_value=_ANALYSIS_RESULT):
        records, texts = orch._build_image_records(_doc_with_one_image(), "d1", "rag-documents")

    assert len(records) == 1
    rec = records[0]
    assert rec["storage_path"] == "images/d1/0.png"
    assert rec["page_number"] == 2
    assert rec["bbox"] == {"x1": 1, "y1": 2, "x2": 3, "y2": 4}
    assert rec["ocr_text"] == "Q1 100 Q2 200"
    assert rec["vlm_ocr_text"] == "Q1 100 Q2 200 (vlm)"
    assert rec["structured_content"] == "Revenue table: Q1=100 Q2=200"
    assert rec["detected_store"] == "table_store"
    assert rec["content_type"] == "table"
    assert rec["image_metadata"]["confidence"] == 0.91
    assert "reason_for_store_selection" in rec["image_metadata"]
    # prefilter tracking fields
    assert rec["processing_status"] == "VLM_PROCESSED"
    assert rec["image_type"] == "table"
    assert "caption" not in rec
    assert texts == ["Revenue table: Q1=100 Q2=200"]


def test_build_image_records_skips_failed_upload():
    with patch.object(orch, "upload_file", side_effect=RuntimeError("bucket down")), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "some ocr")), \
         patch("app.services.image_analysis_service.analyze_image",
               return_value=_ANALYSIS_RESULT):
        records, texts = orch._build_image_records(_doc_with_one_image(), "d1", "rag-documents")
    assert records == [] and texts == []


def test_build_image_records_ocr_failure_falls_back_to_empty():
    """Empty OCR from the prefilter -> analyze_image still runs with empty raw_ocr."""
    analysis_result = {
        "structured_content": "Chart showing revenue", "vlm_ocr_text": "",
        "detected_store": "image_store", "confidence": 0.5,
        "reason_for_store_selection": "Figure", "content_type": "figure",
    }
    with patch.object(orch, "upload_file", return_value="images/d1/0.png"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "")), \
         patch("app.services.image_analysis_service.analyze_image",
               return_value=analysis_result) as mock_analyze:
        records, texts = orch._build_image_records(_doc_with_one_image(), "d1", "rag-documents")

    assert len(records) == 1
    mock_analyze.assert_called_once_with(b"\x89PNG", "")
    assert records[0]["ocr_text"] == ""
    assert records[0]["structured_content"] == "Chart showing revenue"


def test_build_image_records_embed_texts_fallback_to_ocr():
    """When structured_content is empty, embed_texts use raw_ocr."""
    analysis_result = {
        "structured_content": "", "vlm_ocr_text": "",
        "detected_store": "image_store", "confidence": 0.0,
        "reason_for_store_selection": "VLM failed", "content_type": "figure",
    }
    with patch.object(orch, "upload_file", return_value="images/d1/0.png"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "raw ocr fallback")), \
         patch("app.services.image_analysis_service.analyze_image",
               return_value=analysis_result):
        records, texts = orch._build_image_records(_doc_with_one_image(), "d1", "rag-documents")

    assert len(records) == 1
    assert texts == ["raw ocr fallback"]
