"""
Tests for app.api.routes.documents._image_metrics — aggregation of the pre-VLM
image filter tracking fields exposed by GET /documents/{document_id}/images.

Pure Python only: no DB, no VLM, no network. Exercises the aggregation helper
directly with a fake list of image dicts covering VLM_PROCESSED / OCR_ONLY /
SKIPPED processing_status values and a mix of filter_stage / image_type values.
"""

from app.api.routes.documents import _image_metrics


def _item(processing_status, filter_stage=None, image_type=None):
    return {
        "processing_status": processing_status,
        "skip_reason": None,
        "filter_stage": filter_stage,
        "image_type": image_type,
    }


def test_image_metrics_empty():
    metrics = _image_metrics([])
    assert metrics == {
        "total": 0,
        "vlm_processed": 0,
        "ocr_only": 0,
        "skipped": 0,
        "by_stage": {},
        "by_type": {},
        "vlm_avoided_pct": 0.0,
    }


def test_image_metrics_counts_and_percentages():
    items = [
        _item("VLM_PROCESSED", image_type="chart"),
        _item("VLM_PROCESSED", image_type="photo"),
        _item("OCR_ONLY", filter_stage="rule_engine", image_type="text"),
        _item("SKIPPED", filter_stage="technical_filter", image_type="blank"),
        _item("SKIPPED", filter_stage="decision_engine", image_type="logo"),
        _item("SKIPPED", filter_stage="decision_engine", image_type="icon"),
        _item("VLM_PROCESSED", image_type=None),
    ]

    metrics = _image_metrics(items)

    assert metrics["total"] == 7
    assert metrics["vlm_processed"] == 3
    assert metrics["ocr_only"] == 1
    assert metrics["skipped"] == 3
    assert metrics["by_stage"] == {
        "rule_engine": 1,
        "technical_filter": 1,
        "decision_engine": 2,
    }
    assert metrics["by_type"] == {
        "chart": 1,
        "photo": 1,
        "text": 1,
        "blank": 1,
        "logo": 1,
        "icon": 1,
    }
    # avoided = skipped(3) + ocr_only(1) = 4 of 7 => 57.1%
    assert metrics["vlm_avoided_pct"] == round(4 / 7 * 100, 1)


def test_image_metrics_skips_none_stage_and_type():
    items = [
        _item("VLM_PROCESSED", filter_stage=None, image_type=None),
        _item("SKIPPED", filter_stage=None, image_type=None),
    ]

    metrics = _image_metrics(items)

    assert metrics["by_stage"] == {}
    assert metrics["by_type"] == {}
    assert metrics["vlm_processed"] == 1
    assert metrics["skipped"] == 1
    assert metrics["vlm_avoided_pct"] == 50.0
