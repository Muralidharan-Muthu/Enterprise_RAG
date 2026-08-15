import io
import logging

logger = logging.getLogger(__name__)


def extract_pages(content: bytes, max_pages: int) -> bytes:
    """Return a new PDF containing only the first max_pages pages.

    Uses pypdfium2 (bundled with docling). Falls back to original bytes if
    extraction fails or the PDF already fits within the limit.
    """
    try:
        import pypdfium2 as pdfium

        src = pdfium.PdfDocument(content)
        total = len(src)
        if total <= max_pages:
            src.close()
            return content

        logger.info("PDF has %d pages — truncating to first %d", total, max_pages)
        dst = pdfium.PdfDocument.new()
        dst.import_pages(src, list(range(max_pages)))

        buf = io.BytesIO()
        dst.save(buf)
        result = buf.getvalue()

        dst.close()
        src.close()
        return result
    except Exception as exc:
        logger.warning("PDF page extraction failed (%s) — using original bytes", exc)
        return content
