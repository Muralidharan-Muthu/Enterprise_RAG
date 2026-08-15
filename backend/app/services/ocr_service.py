"""
Raw OCR engine wrapper: runs EasyOCR on a single cropped image and returns
the detected text UNALTERED (lines joined by newlines). Intended for audit
trails and as input to VLM prompts — no cleaning or correction is applied.
"""
import logging

logger = logging.getLogger(__name__)

# Module-level singleton; built on first call to ocr_image().
_reader = None


def _get_reader():
    """Lazily initialise and return the EasyOCR Reader singleton."""
    global _reader
    if _reader is None:
        import easyocr  # noqa: PLC0415  (deferred import keeps module import cheap)
        logger.info("Initialising EasyOCR Reader (en, cpu) — this loads ~1 GB of model weights")
        # verbose=False suppresses EasyOCR's progress bars, whose block glyphs
        # (█ = █) crash on non-UTF-8 stdout (e.g. Windows cp1252 console),
        # raising UnicodeEncodeError and aborting OCR entirely.
        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        logger.info("EasyOCR Reader ready")
    return _reader


def ocr_image(png_bytes: bytes) -> str:
    """Run raw OCR on a single cropped PNG.

    Returns the OCR engine output joined by newlines, UNALTERED.
    Returns '' on any failure (non-fatal).
    """
    try:
        import io
        import numpy as np
        from PIL import Image

        img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
        reader = _get_reader()
        lines = reader.readtext(img, detail=0, paragraph=True)
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("ocr_image failed (%s: %s)", type(exc).__name__, exc)
        return ""
