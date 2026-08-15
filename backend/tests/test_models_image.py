from app.models.document import ExtractedImage, ParsedDocument, ExtractedTable, BoundingBox


def test_extracted_image_fields():
    img = ExtractedImage(
        image_index=0, page_number=2,
        bbox=BoundingBox(x1=1, y1=2, x2=3, y2=4),
        png_bytes=b"\x89PNG", width=100, height=50,
    )
    assert img.image_index == 0
    assert img.page_number == 2
    assert img.png_bytes == b"\x89PNG"
    assert img.width == 100 and img.height == 50


def test_parsed_document_images_default_empty():
    pd = ParsedDocument(
        doc_id="d", filename="f.pdf", raw_text="", text_blocks=[], tables=[],
        page_count=1, word_count=0, has_tables=False, has_images=False,
    )
    assert pd.images == []


def test_extracted_table_image_bytes_default_none():
    t = ExtractedTable(table_index=0, page_number=1, headers=[], rows=[])
    assert t.image_png_bytes is None
