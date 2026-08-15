"""
Image pre-filter — decides SKIP / OCR_ONLY / VLM_PROCESSED for each extracted
image BEFORE the expensive VLM (and, for technical rejects, before OCR too).

Goal: stop spending GPU/VLM time on non-informative images (logos, decorative
icons, blanks, separator lines, duplicates) while never dropping an image that
carries real information (tables, charts, diagrams, screenshots, photos).

Pipeline (matches the design):
    Stage 1  technical_filter   tiny / blank / separator / duplicate / corrupted
    Stage 2  OCR                (run by the caller via ocr_fn, only if Stage 1 passes)
    Stage 3  rule_engine        logo / decorative icon / watermark / very-little-info
    Stage 4  classify           logo|icon|text|table|chart|diagram|screenshot|photo
    Stage 5  decision_engine    SKIP | OCR_ONLY | VLM_PROCESSED

Design principles
-----------------
* Cheap: pure Pillow + numpy, no GPU, no extra dependency, no model load.
* Fail-OPEN: any unexpected error -> VLM_PROCESSED. A filter bug must never cause
  an informative image to be silently dropped.
* Conservative SKIP: an image is skipped only on high-confidence junk signals
  (multiple conditions agree). Anything ambiguous goes to the VLM.
* Auditable: every decision carries processing_status / skip_reason / filter_stage
  / image_type, plus the raw computed features (stored in image_metadata) for
  later threshold tuning.
* Stateful per document: an ImagePrefilter instance remembers perceptual hashes
  so the Nth copy of the same image (repeated header logo, etc.) is a duplicate.

Thresholds live in app.config (PREFILTER_*) so they can be tuned without code
changes.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# Processing-status values recorded in image_store.processing_status
STATUS_SKIPPED = "SKIPPED"
STATUS_OCR_ONLY = "OCR_ONLY"
STATUS_VLM = "VLM_PROCESSED"

# filter_stage values
STAGE_TECHNICAL = "technical_filter"
STAGE_RULE = "rule_engine"
STAGE_DECISION = "decision_engine"

# image_type values that warrant the VLM (informative content).
_VLM_TYPES = {"table", "chart", "diagram", "screenshot", "photo"}


@dataclass
class PrefilterDecision:
    processing_status: str            # STATUS_*
    image_type: str                   # logo|icon|blank|separator|duplicate|corrupted|
                                      # watermark|text|table|chart|diagram|screenshot|photo|unknown
    run_vlm: bool
    skip_reason: str | None = None
    filter_stage: str | None = None
    features: dict = field(default_factory=dict)


def _cfg(name: str, default):
    from app.config import settings
    return getattr(settings, name, default)


# ─────────────────────────────────────────────────────────────────────────────
# Perceptual hash (average hash) for duplicate detection — no extra dependency.
# ─────────────────────────────────────────────────────────────────────────────

def _ahash(gray_small: np.ndarray) -> int:
    """64-bit average hash from an 8x8 grayscale array."""
    mean = gray_small.mean()
    bits = (gray_small >= mean).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ─────────────────────────────────────────────────────────────────────────────
# Feature extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_features(png_bytes: bytes) -> dict:
    """Compute cheap visual features from PNG bytes. Raises on a corrupt image."""
    from PIL import Image

    im = Image.open(io.BytesIO(png_bytes))
    im.load()  # force decode now so corruption raises here
    im = im.convert("RGB")
    w, h = im.size

    gray = np.asarray(im.convert("L"), dtype=np.float32)
    std = float(gray.std())

    # Gradient-based edge density (fraction of strong-gradient pixels).
    if gray.shape[0] > 1 and gray.shape[1] > 1:
        gx = np.abs(np.diff(gray, axis=1))
        gy = np.abs(np.diff(gray, axis=0))
        edge_density = float(((gx > 25).mean() + (gy > 25).mean()) / 2.0)
    else:
        edge_density = 0.0

    # Distinct-colour count on a downsampled, depth-reduced copy (logos/icons have
    # very few; photos/charts have many). NEAREST resampling avoids introducing
    # blended edge colours that would inflate the count for flat-colour icons.
    from PIL import Image as _Image
    small_rgb = np.asarray(im.resize((32, 32), _Image.NEAREST), dtype=np.uint8) >> 4  # 16 levels/channel
    n_colors = int(np.unique(small_rgb.reshape(-1, 3), axis=0).shape[0])

    # 8x8 grayscale for the perceptual hash.
    gray_8 = np.asarray(im.resize((8, 8)).convert("L"), dtype=np.float32)
    ahash = _ahash(gray_8)

    # Grid/line score: fraction of full rows/cols that are near-uniform AND dark
    # (table gridlines, chart axes, filled bars). Must be dark so a mostly-white
    # text block (many uniform but BRIGHT rows) does not look like a grid.
    row_dark_uniform = ((gray.std(axis=1) < 12) & (gray.mean(axis=1) < 128)).mean() if gray.shape[0] else 0.0
    col_dark_uniform = ((gray.std(axis=0) < 12) & (gray.mean(axis=0) < 128)).mean() if gray.shape[1] else 0.0
    line_score = float(max(row_dark_uniform, col_dark_uniform))

    aspect = (max(w, h) / min(w, h)) if min(w, h) > 0 else 999.0

    return {
        "width": w, "height": h, "area": w * h, "aspect": round(aspect, 2),
        "gray_std": round(std, 2), "edge_density": round(edge_density, 4),
        "n_colors": n_colors, "line_score": round(line_score, 3), "ahash": ahash,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — technical filter
# ─────────────────────────────────────────────────────────────────────────────

def technical_filter(feats: dict, seen_hashes: list[int]) -> tuple[bool, str, str | None]:
    """Return (skip, image_type, reason). Pure; does not mutate seen_hashes."""
    w, h, area, aspect = feats["width"], feats["height"], feats["area"], feats["aspect"]

    if w < _cfg("PREFILTER_MIN_DIM", 24) or h < _cfg("PREFILTER_MIN_DIM", 24) \
            or area < _cfg("PREFILTER_MIN_AREA", 1600):
        return True, "tiny", f"Tiny image ({w}x{h})"

    if feats["gray_std"] < _cfg("PREFILTER_BLANK_STD", 6.0):
        return True, "blank", "Blank/near-uniform image"

    if aspect >= _cfg("PREFILTER_SEPARATOR_ASPECT", 12.0) \
            and min(w, h) <= _cfg("PREFILTER_SEPARATOR_THIN_DIM", 12):
        return True, "separator", f"Separator line (aspect {aspect})"

    dup_t = _cfg("PREFILTER_DUP_HAMMING", 5)
    for prev in seen_hashes:
        if _hamming(prev, feats["ahash"]) <= dup_t:
            return True, "duplicate", "Duplicate of an earlier image"

    # Obvious icon candidate — VERY small area. Caught here (pre-OCR) so decorative
    # icons cost neither OCR nor VLM. Conservative: real charts/tables are an order
    # of magnitude larger, so this never fires on informative content.
    if area <= _cfg("PREFILTER_VERY_SMALL_AREA", 6000):
        return True, "icon", f"Obvious icon candidate (very small area {w}x{h}, pre-OCR)"

    # Near-flat / trivial graphic: almost no edges AND very few colours (a simple
    # decorative shape). Distinct from 'blank' (which is std-based).
    if feats["edge_density"] <= _cfg("PREFILTER_LOWCOMPLEXITY_EDGE", 0.01) \
            and feats["n_colors"] <= _cfg("PREFILTER_LOWCOMPLEXITY_COLORS", 6):
        return True, "icon", "Very low visual complexity (pre-OCR)"

    return False, "unknown", None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — rule engine (after OCR)
# ─────────────────────────────────────────────────────────────────────────────

def rule_engine(feats: dict, ocr_text: str) -> tuple[bool, str, str | None]:
    """Return (skip, image_type, reason). Conservative: needs several signals."""
    ocr_len = len((ocr_text or "").strip())

    # Decorative icon / logo: SMALL AND (almost) no text. Colour count is NOT a
    # signal — real rasterised icons are antialiased/gradient-filled with many
    # colours. Real charts/tables are far larger AND carry axis/cell text, so
    # they never match this rule.
    if feats["area"] <= _cfg("PREFILTER_ICON_MAX_AREA", 10000) \
            and ocr_len <= _cfg("PREFILTER_ICON_MAX_OCR_CHARS", 6):
        kind = "logo" if feats["area"] <= 2500 else "icon"
        return True, kind, "Decorative icon/logo (small, no text)"

    # Very-little-information: almost no text AND almost no structure AND few colours.
    if ocr_len <= _cfg("PREFILTER_LOWINFO_MAX_OCR_CHARS", 8) \
            and feats["edge_density"] <= _cfg("PREFILTER_LOWINFO_MAX_EDGE", 0.02) \
            and feats["n_colors"] <= _cfg("PREFILTER_LOWINFO_MAX_COLORS", 16):
        return True, "watermark", "Low information (no text, no structure)"

    return False, "unknown", None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4 — lightweight classification (heuristic)
# ─────────────────────────────────────────────────────────────────────────────

def classify(feats: dict, ocr_text: str) -> str:
    """Coarse type label. Used for audit + to pick OCR_ONLY vs VLM. When unsure,
    returns a VLM-bound type so the image is not under-processed."""
    ocr_len = len((ocr_text or "").strip())
    n_colors, edge, line_score, area = feats["n_colors"], feats["edge_density"], feats["line_score"], feats["area"]

    # Strong gridlines + some text -> table.
    if line_score >= 0.15 and ocr_len >= 10:
        return "table"
    # Large + lots of text + horizontal structure -> screenshot.
    if area >= 400_000 and ocr_len >= 200 and line_score >= 0.05:
        return "screenshot"
    # Many colours + edges -> chart (bars/lines/legend) or photo.
    if n_colors >= 40 and edge >= 0.03:
        # Photos are continuous-tone (very high colour count, little flat area);
        # charts have flat fills + axis text.
        return "photo" if (n_colors >= 200 and ocr_len < 10) else "chart"
    # Real text with no chart/grid/diagram structure -> a plain text label (OCR is
    # enough). Checked BEFORE 'diagram'. Conservative: low colour count, no dark
    # gridlines, and low edge density (diagrams/flowcharts have more edges) so we
    # never down-grade a structured graphic to OCR-only.
    if (ocr_len >= _cfg("PREFILTER_TEXT_MIN_OCR_CHARS", 12)
            and n_colors <= 24 and line_score < 0.1 and edge < 0.06):
        return "text"
    # Moderate structure + few colours + some text -> diagram.
    if edge >= 0.02 and ocr_len >= 5:
        return "diagram"
    # Small + few colours -> icon (rule engine usually caught this already).
    if area <= _cfg("PREFILTER_ICON_MAX_AREA", 65536) and n_colors <= 16:
        return "icon"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5 — decision engine
# ─────────────────────────────────────────────────────────────────────────────

def decide(image_type: str) -> tuple[str, bool]:
    """Map an image_type to (processing_status, run_vlm)."""
    if image_type in _VLM_TYPES or image_type == "unknown":
        return STATUS_VLM, True       # unknown -> fail-safe to VLM
    if image_type == "text":
        return STATUS_OCR_ONLY, False
    return STATUS_SKIPPED, False      # logo / icon


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrating class (stateful per document for duplicate detection)
# ─────────────────────────────────────────────────────────────────────────────

class ImagePrefilter:
    """One instance per document. Call evaluate() per extracted image."""

    def __init__(self):
        self._hashes: list[int] = []

    def evaluate(self, png_bytes: bytes, ocr_fn: Callable[[], str]) -> tuple[PrefilterDecision, str]:
        """Run the 5-stage pipeline.

        Args:
            png_bytes: the cropped image bytes.
            ocr_fn:    zero-arg callable returning OCR text. Invoked ONLY if Stage 1
                       passes (so junk images never pay the OCR cost).

        Returns (decision, ocr_text). ocr_text is "" when OCR was not run.
        Never raises — on any error it fails open to VLM_PROCESSED.
        """
        if not _cfg("PREFILTER_ENABLED", True):
            ocr_text = _safe(ocr_fn)
            return PrefilterDecision(STATUS_VLM, "unknown", True, filter_stage=None), ocr_text

        # ── feature extraction (corruption check) ──
        try:
            feats = extract_features(png_bytes)
        except Exception as exc:
            logger.warning("prefilter: corrupt/undecodable image (%s) — skipping", exc)
            return PrefilterDecision(STATUS_SKIPPED, "corrupted", False,
                                     skip_reason=f"Corrupted image ({type(exc).__name__})",
                                     filter_stage=STAGE_TECHNICAL), ""

        try:
            # ── Stage 1: technical filter (no OCR yet) ──
            skip, itype, reason = technical_filter(feats, self._hashes)
            self._hashes.append(feats["ahash"])
            if skip:
                return PrefilterDecision(STATUS_SKIPPED, itype, False, reason,
                                         STAGE_TECHNICAL, feats), ""

            # ── Stage 2: OCR ──
            ocr_text = _safe(ocr_fn)

            # ── Stage 3: rule engine ──
            skip, itype, reason = rule_engine(feats, ocr_text)
            if skip:
                return PrefilterDecision(STATUS_SKIPPED, itype, False, reason,
                                         STAGE_RULE, feats), ocr_text

            # ── Stage 4 + 5: classify + decide ──
            itype = classify(feats, ocr_text)
            status, run_vlm = decide(itype)
            reason = None
            if status == STATUS_SKIPPED:
                reason = f"Classified as {itype}"
            elif status == STATUS_OCR_ONLY:
                reason = "OCR sufficient (text label, no visual structure)"
            return PrefilterDecision(status, itype, run_vlm, reason, STAGE_DECISION, feats), ocr_text

        except Exception as exc:
            # Fail open: never drop an image because of a filter bug.
            logger.warning("prefilter error (%s) — failing open to VLM", exc)
            ocr_text = _safe(ocr_fn)
            return PrefilterDecision(STATUS_VLM, "unknown", True,
                                     filter_stage=None, features=feats), ocr_text


def _safe(ocr_fn: Callable[[], str]) -> str:
    try:
        return ocr_fn() or ""
    except Exception as exc:
        logger.warning("prefilter: ocr_fn failed (%s)", exc)
        return ""
