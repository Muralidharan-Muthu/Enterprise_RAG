import io
from PIL import Image
from app.services.document_parser import _pil_to_png_bytes


def test_pil_to_png_bytes_roundtrip():
    src = Image.new("RGB", (40, 20), color=(10, 20, 30))
    png_bytes, w, h = _pil_to_png_bytes(src)
    assert w == 40 and h == 20
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    # bytes are decodable back to the same size
    back = Image.open(io.BytesIO(png_bytes))
    assert back.size == (40, 20)


import os
import pytest


@pytest.mark.slow
@pytest.mark.skipif(not os.path.exists("tests/fixtures/with_figure.pdf"),
                    reason="needs a sample PDF with an embedded figure")
def test_docling_extracts_images():
    from app.services.document_parser import parse_document
    pd = parse_document("tests/fixtures/with_figure.pdf", "test-doc")
    assert pd.has_images
    assert len(pd.images) >= 1
    first = pd.images[0]
    assert first.png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert first.page_number >= 1
