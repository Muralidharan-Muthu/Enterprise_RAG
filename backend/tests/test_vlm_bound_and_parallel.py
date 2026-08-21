"""Tests for package A (bounded OCR in the VLM prompt) and package B
(bounded-concurrency VLM calls during ingestion), without any real network,
DB, or model.

- OCR truncation: image_analysis_service._bound_ocr_text / analyze_image.
- Parallelism correctness + dedup preservation:
  ingestion_orchestrator._build_image_records_parallel.
"""
import io
import threading
import time
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from app.models.document import ParsedDocument, ExtractedImage, BoundingBox
import app.services.ingestion_orchestrator as orch
from app.services.image_prefilter import PrefilterDecision, STATUS_VLM, STATUS_SKIPPED
import app.services.image_analysis_service as ias


def _real_png(seed: int, w=120, h=120) -> bytes:
    """A real, decodable noise PNG (distinct per seed) — needed for tests that
    exercise the actual ImagePrefilter duplicate-detection logic."""
    rng = np.random.RandomState(seed)
    arr = rng.randint(0, 256, size=(h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# A. OCR truncation
# ─────────────────────────────────────────────────────────────────────────────

def test_bound_ocr_text_under_limit_unchanged():
    text = "short ocr text"
    assert ias._bound_ocr_text(text, 8000) == text


def test_bound_ocr_text_truncates_and_marks():
    text = "x" * 20000
    bounded = ias._bound_ocr_text(text, 8000)
    assert len(bounded) < len(text)
    assert bounded.startswith("x" * 8000)
    assert bounded.endswith("[truncated]")


def test_analyze_image_sends_bounded_ocr_to_model(monkeypatch):
    """Feed OCR longer than VLM_OCR_MAX_CHARS and capture the actual payload
    sent to the HTTP client — assert the OCR portion of the prompt text is
    capped at the configured limit."""
    from app.config import settings

    monkeypatch.setattr(settings, "GROQ_BASE_URL", "http://fake-Groq4")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "VLM_OCR_MAX_CHARS", 100)

    long_ocr = "A" * 5000
    captured_payload = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{
                    "message": {"content": '{"detected_store": "Image Store", '
                                            '"structured_content": "ok", "ocr_text": "", '
                                            '"confidence": 0.5, "reason_for_store_selection": "x"}'}
                }]
            }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured_payload.update(json)
            return _FakeResponse()

    monkeypatch.setattr(ias.httpx, "Client", _FakeClient)

    ias.analyze_image(b"\x89PNG", long_ocr)

    sent_text = captured_payload["messages"][0]["content"][0]["text"]
    # Extract only the OCR section appended after the fixed prompt marker.
    marker = "--- RAW OCR OUTPUT (supporting evidence, may contain errors) ---\n"
    idx = sent_text.index(marker) + len(marker)
    ocr_in_prompt = sent_text[idx:]
    assert "A" * 100 in ocr_in_prompt
    assert "A" * 101 not in ocr_in_prompt
    assert "[truncated]" in ocr_in_prompt


# ─────────────────────────────────────────────────────────────────────────────
# B. Parallelism correctness + dedup preservation
# ─────────────────────────────────────────────────────────────────────────────

def _make_doc(n: int, duplicate_pair=False) -> ParsedDocument:
    images = []
    for i in range(n):
        png = b"\x89PNGDATA" + bytes([i % 256])
        if duplicate_pair and i == 1:
            png = b"\x89PNGDATA" + bytes([0])   # identical to image 0
        images.append(ExtractedImage(
            image_index=i, page_number=1, bbox=BoundingBox(0, 0, 1, 1),
            png_bytes=png, width=10, height=10,
        ))
    return ParsedDocument(
        doc_id="d1", filename="f.pdf", raw_text="", text_blocks=[], tables=[],
        page_count=1, word_count=0, has_tables=False, has_images=True,
        images=images,
    )


def _vlm_decision():
    return PrefilterDecision(STATUS_VLM, "table", True, None, "decision_engine", {})


def _dup_decision():
    return PrefilterDecision(STATUS_SKIPPED, "duplicate", False, "duplicate", "technical_filter", {})


class _ConcurrencyTrackingAnalyze:
    """Stub for analyze_image that sleeps briefly and records the observed
    concurrency (how many calls were in-flight simultaneously)."""

    def __init__(self, sleep_s=0.05):
        self.sleep_s = sleep_s
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = []

    def __call__(self, png_bytes, raw_ocr_text):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.sleep_s)
            self.calls.append(png_bytes)
            return {
                "structured_content": f"content-for-{png_bytes[-1]}",
                "vlm_ocr_text": "", "detected_store": "table_store",
                "confidence": 0.9, "reason_for_store_selection": "ok",
                "content_type": "table",
            }
        finally:
            with self.lock:
                self.in_flight -= 1


def test_all_images_stored_exactly_once_and_correct(monkeypatch):
    n = 6
    doc = _make_doc(n)
    tracker = _ConcurrencyTrackingAnalyze(sleep_s=0.02)

    with patch.object(orch, "upload_file", return_value="ok"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "some ocr")), \
         patch("app.services.image_analysis_service.analyze_image", side_effect=tracker):
        records, texts = orch._build_image_records_parallel(doc, "d1", "bucket", max_workers=4)

    assert len(records) == n
    assert len(texts) == n
    image_indices = sorted(r["image_index"] for r in records)
    assert image_indices == list(range(n))
    for r in records:
        expected_suffix = r["image_index"] % 256
        assert r["structured_content"] == f"content-for-{expected_suffix}"
    assert tracker.max_in_flight > 1
    assert tracker.max_in_flight <= 4


def test_sequential_when_max_workers_is_one(monkeypatch):
    n = 5
    doc = _make_doc(n)
    tracker = _ConcurrencyTrackingAnalyze(sleep_s=0.02)

    with patch.object(orch, "upload_file", return_value="ok"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "some ocr")), \
         patch("app.services.image_analysis_service.analyze_image", side_effect=tracker):
        records, texts = orch._build_image_records_parallel(doc, "d1", "bucket", max_workers=1)

    assert len(records) == n
    assert tracker.max_in_flight == 1
    # sequential path preserves original image order
    assert [r["image_index"] for r in records] == list(range(n))


def test_parallel_exceeds_cap_bound(monkeypatch):
    n = 10
    doc = _make_doc(n)
    tracker = _ConcurrencyTrackingAnalyze(sleep_s=0.03)
    cap = 3

    with patch.object(orch, "upload_file", return_value="ok"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "some ocr")), \
         patch("app.services.image_analysis_service.analyze_image", side_effect=tracker):
        records, _ = orch._build_image_records_parallel(doc, "d1", "bucket", max_workers=cap)

    assert len(records) == n
    assert tracker.max_in_flight > 1
    assert tracker.max_in_flight <= cap


def test_vlm_exception_falls_back_without_aborting_others(monkeypatch):
    n = 4

    def _flaky_analyze(png_bytes, raw_ocr_text):
        if png_bytes[-1] == 1:
            raise RuntimeError("boom")
        return {
            "structured_content": "ok", "vlm_ocr_text": "", "detected_store": "table_store",
            "confidence": 0.5, "reason_for_store_selection": "ok", "content_type": "table",
        }

    doc = _make_doc(n)
    with patch.object(orch, "upload_file", return_value="ok"), \
         patch("app.services.image_prefilter.ImagePrefilter.evaluate",
               return_value=(_vlm_decision(), "raw-ocr")), \
         patch("app.services.image_analysis_service.analyze_image", side_effect=_flaky_analyze):
        records, _ = orch._build_image_records_parallel(doc, "d1", "bucket", max_workers=4)

    assert len(records) == n
    failed = [r for r in records if r["image_index"] == 1]
    assert len(failed) == 1
    assert failed[0]["structured_content"] == "raw-ocr"   # fallback to raw OCR
    # VLM crashed and the short OCR ("raw-ocr", < searchable threshold) isn't worth
    # indexing -> stays a repository asset in image_store (content-driven, not forced).
    assert failed[0]["detected_store"] == "image_store"
    assert "analyze failed" in failed[0]["image_metadata"]["reason_for_store_selection"]


def test_dedup_preserved_with_sequential_prefilter(monkeypatch):
    """Two identical images must still yield exactly one 'duplicate' decision
    because the prefilter (Phase 1) always runs sequentially, never in
    parallel, regardless of max_workers used for the VLM phase."""
    from app.services.image_prefilter import ImagePrefilter

    same_png = _real_png(seed=42)
    images = [
        ExtractedImage(image_index=0, page_number=1, bbox=BoundingBox(0, 0, 1, 1),
                       png_bytes=same_png, width=120, height=120),
        ExtractedImage(image_index=1, page_number=1, bbox=BoundingBox(0, 0, 1, 1),
                       png_bytes=same_png, width=120, height=120),
    ]
    doc = ParsedDocument(
        doc_id="d1", filename="f.pdf", raw_text="", text_blocks=[], tables=[],
        page_count=1, word_count=0, has_tables=False, has_images=True,
        images=images,
    )

    call_log = []
    orig_evaluate = ImagePrefilter.evaluate

    def _tracking_evaluate(self, png_bytes, ocr_fn):
        decision, ocr = orig_evaluate(self, png_bytes, ocr_fn)
        call_log.append(decision.image_type)
        return decision, ocr

    tracker = _ConcurrencyTrackingAnalyze(sleep_s=0.01)

    with patch.object(orch, "upload_file", return_value="ok"), \
         patch.object(ImagePrefilter, "evaluate", _tracking_evaluate), \
         patch("app.services.image_analysis_service.analyze_image", side_effect=tracker):
        records, _ = orch._build_image_records_parallel(doc, "d1", "bucket", max_workers=4)

    # The real ImagePrefilter's aHash duplicate detection ran sequentially in
    # Phase 1, so the second identical image is flagged 'duplicate' and only
    # one VLM call happens (for the first, non-duplicate image).
    assert call_log.count("duplicate") == 1
    duplicate_records = [r for r in records if r["image_type"] == "duplicate"]
    assert len(duplicate_records) == 1
    assert duplicate_records[0]["processing_status"] == STATUS_SKIPPED
    assert len(tracker.calls) == 1
