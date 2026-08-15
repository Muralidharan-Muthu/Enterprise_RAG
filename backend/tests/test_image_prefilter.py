"""Unit tests for the pre-VLM image filter (app.services.image_prefilter).

Pure-Python: synthetic PNGs (PIL/numpy) for the technical + end-to-end paths,
crafted feature dicts for the heuristic rule-engine / classify / decide logic.
No DB, no VLM, no OCR model (ocr_fn is injected).
"""
import io

import numpy as np
import pytest
from PIL import Image

from app.services import image_prefilter as pf
from app.services.image_prefilter import (
    ImagePrefilter, STATUS_SKIPPED, STATUS_OCR_ONLY, STATUS_VLM,
    STAGE_TECHNICAL, STAGE_RULE, STAGE_DECISION,
    technical_filter, rule_engine, classify, decide,
)


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def _solid(w, h, val=200):
    return _png(np.full((h, w, 3), val, dtype=np.uint8))


def _noise(w, h, seed=0):
    rng = np.random.RandomState(seed)
    return _png(rng.randint(0, 256, size=(h, w, 3), dtype=np.uint8))


def _icon(w=80, h=80):
    a = np.full((h, w, 3), 255, dtype=np.uint8)
    yy, xx = np.ogrid[:h, :w]
    mask = (yy - h // 2) ** 2 + (xx - w // 2) ** 2 <= (min(w, h) // 3) ** 2
    a[mask] = (30, 60, 200)   # blue disc on white = 2 colours
    return _png(a)


def _raises():
    def _f():
        raise AssertionError("ocr_fn must NOT be called for a Stage-1 technical skip")
    return _f


# ── Stage 1: technical filter (end-to-end via evaluate) ──────────────────────

def test_blank_skipped_without_ocr():
    pre = ImagePrefilter()
    dec, ocr = pre.evaluate(_solid(100, 100, 210), _raises())   # ocr must not run
    assert dec.processing_status == STATUS_SKIPPED
    assert dec.image_type == "blank"
    assert dec.filter_stage == STAGE_TECHNICAL
    assert dec.run_vlm is False and ocr == ""


def test_tiny_skipped():
    dec, _ = ImagePrefilter().evaluate(_noise(10, 10), _raises())
    assert dec.processing_status == STATUS_SKIPPED and dec.image_type == "tiny"


def test_separator_skipped():
    # 600x30 vertical gradient: not blank, not tiny, aspect=20, short side 30<=40.
    a = np.tile((np.arange(30, dtype=np.uint8) * 8)[:, None, None], (1, 600, 3))
    dec, _ = ImagePrefilter().evaluate(_png(a), _raises())
    assert dec.processing_status == STATUS_SKIPPED and dec.image_type == "separator"


def test_duplicate_detected_on_second_occurrence():
    pre = ImagePrefilter()
    img = _noise(120, 120, seed=7)
    d1, _ = pre.evaluate(img, lambda: "")
    d2, _ = pre.evaluate(img, lambda: "")
    assert d2.processing_status == STATUS_SKIPPED and d2.image_type == "duplicate"
    assert d1.image_type != "duplicate"   # first occurrence is not a duplicate


def test_small_icon_skipped_pre_ocr():
    # 60x60 = 3600 px^2 <= PREFILTER_VERY_SMALL_AREA -> obvious icon at Stage 1.
    # ocr_fn must NOT run (the whole point of pre-OCR detection).
    dec, ocr = ImagePrefilter().evaluate(_icon(60, 60), _raises())
    assert dec.processing_status == STATUS_SKIPPED
    assert dec.image_type == "icon"
    assert dec.filter_stage == STAGE_TECHNICAL    # caught BEFORE OCR
    assert dec.run_vlm is False and ocr == ""


def test_corrupted_skipped():
    dec, ocr = ImagePrefilter().evaluate(b"not-a-real-png", lambda: "")
    assert dec.processing_status == STATUS_SKIPPED and dec.image_type == "corrupted"
    assert dec.filter_stage == STAGE_TECHNICAL


# ── Stage 3: rule engine (end-to-end + unit) ─────────────────────────────────

def test_icon_skipped_by_rule_engine():
    dec, _ = ImagePrefilter().evaluate(_icon(), lambda: "")   # few colours, no text
    assert dec.processing_status == STATUS_SKIPPED
    assert dec.image_type in ("logo", "icon")
    assert dec.filter_stage == STAGE_RULE
    assert dec.run_vlm is False


def test_rule_engine_keeps_image_with_text():
    feats = {"area": 50000, "n_colors": 8, "edge_density": 0.10, "line_score": 0.2}
    skip, _, _ = rule_engine(feats, "Quarterly revenue grew 23% year over year")
    assert skip is False


def test_rule_engine_lowinfo_skip():
    feats = {"area": 50000, "n_colors": 10, "edge_density": 0.005, "line_score": 0.0}
    skip, itype, _ = rule_engine(feats, "")
    assert skip is True   # caught as icon (small+few colours+no text) or watermark


# ── Stage 4/5: classify + decide ─────────────────────────────────────────────

def test_classify_table_goes_to_vlm():
    feats = {"n_colors": 20, "edge_density": 0.05, "line_score": 0.25, "area": 200000}
    assert classify(feats, "Metric Value Trend Revenue 98 Profit 16") == "table"
    assert decide("table") == (STATUS_VLM, True)


def test_classify_chart_goes_to_vlm():
    feats = {"n_colors": 80, "edge_density": 0.08, "line_score": 0.02, "area": 150000}
    assert classify(feats, "Q1 Q2 Q3 Q4 revenue") == "chart"
    assert decide("chart") == (STATUS_VLM, True)


def test_classify_plain_text_is_ocr_only():
    feats = {"n_colors": 6, "edge_density": 0.03, "line_score": 0.0, "area": 30000}
    assert classify(feats, "This is a simple text caption label under a figure") == "text"
    assert decide("text") == (STATUS_OCR_ONLY, False)


def test_unknown_fails_open_to_vlm():
    # Ambiguous: little text, low structure but enough to pass rule engine upstream.
    assert decide("unknown") == (STATUS_VLM, True)


# ── Toggle ───────────────────────────────────────────────────────────────────

def test_disabled_sends_everything_to_vlm(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "PREFILTER_ENABLED", False, raising=False)
    called = {"ocr": False}

    def ocr():
        called["ocr"] = True
        return "some text"

    dec, ocr_text = ImagePrefilter().evaluate(_icon(), ocr)
    assert dec.processing_status == STATUS_VLM and dec.run_vlm is True
    assert called["ocr"] is True and ocr_text == "some text"
