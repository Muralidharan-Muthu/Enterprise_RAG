"""Tests for document_parser.py"""
import pytest
from app.services.document_parser import parse_document
from app.models.document import ParsedDocument, TextBlock


def test_parse_returns_parsed_document(sample_pdf_path):
    result = parse_document(sample_pdf_path, doc_id="test-001")
    assert isinstance(result, ParsedDocument)
    assert result.doc_id == "test-001"


def test_parse_has_text_blocks(sample_pdf_path):
    result = parse_document(sample_pdf_path, doc_id="test-001")
    assert len(result.text_blocks) > 0
    for block in result.text_blocks:
        assert isinstance(block, TextBlock)
        assert len(block.text.strip()) > 0


def test_parse_page_count_positive(sample_pdf_path):
    result = parse_document(sample_pdf_path, doc_id="test-001")
    assert result.page_count >= 1


def test_parse_word_count_positive(sample_pdf_path):
    result = parse_document(sample_pdf_path, doc_id="test-001")
    assert result.word_count > 0


def test_parse_raw_text_not_empty(sample_pdf_path):
    result = parse_document(sample_pdf_path, doc_id="test-001")
    assert len(result.raw_text.strip()) > 0


def test_parse_nonexistent_file_raises():
    from app.core.exceptions import ParsingError
    with pytest.raises(ParsingError):
        parse_document("/nonexistent/file.pdf", doc_id="x")
