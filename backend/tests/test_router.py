"""Tests for router_service.py — rule-based fallback (no CDAC required)."""
import pytest
from app.services.router_service import _rule_based_classify, RouterResult
from app.models.document import ParsedDocument, TextBlock


def _make_doc(filename: str, text: str) -> ParsedDocument:
    return ParsedDocument(
        doc_id="test",
        filename=filename,
        raw_text=text,
        text_blocks=[TextBlock(text=text, page_number=1, block_type="paragraph", token_count=len(text.split()))],
        tables=[],
        page_count=1,
        word_count=len(text.split()),
        has_tables=False,
        has_images=False,
    )


def test_rule_based_financial():
    doc = _make_doc(
        "annual_report_2024.pdf",
        "Revenue increased by 15% year-over-year. The balance sheet shows total assets of USD 5 billion. "
        "EBITDA margin improved to 22%. Cash flow from operations was positive for Q3 fiscal year."
    )
    result = _rule_based_classify(doc)
    assert result.document_type == "financial"
    assert isinstance(result.confidence, float)
    assert result.used_fallback is True


def test_rule_based_legal():
    doc = _make_doc(
        "nda_agreement.pdf",
        "This Non-Disclosure Agreement (NDA) is entered into by and between the Parties. "
        "The Contractor shall indemnify and hold harmless the Company. "
        "Governing law shall be the laws of the State of California. Arbitration clause applies."
    )
    result = _rule_based_classify(doc)
    assert result.document_type == "legal"


def test_rule_based_policy():
    doc = _make_doc(
        "hr_policy.pdf",
        "This policy outlines the standard operating procedure for all employees. "
        "Compliance with these guidelines is mandatory. The process for handling complaints is described herein."
    )
    result = _rule_based_classify(doc)
    assert result.document_type == "policy"


def test_rule_based_research():
    doc = _make_doc(
        "research_paper.pdf",
        "Abstract: This study investigates the methodology behind large language models. "
        "Our hypothesis is that retrieval augmented generation improves factual accuracy. "
        "The findings show a 23% improvement. Bibliography and citation list are included."
    )
    result = _rule_based_classify(doc)
    assert result.document_type == "research"


def test_rule_based_returns_router_result():
    doc = _make_doc("unknown.pdf", "Some generic document text without clear category signals.")
    result = _rule_based_classify(doc)
    assert isinstance(result, RouterResult)
    assert result.document_type in ("policy", "financial", "legal", "entity", "research")
    assert 0.0 <= result.confidence <= 1.0


def test_rule_based_financial_with_tables():
    from app.models.document import ExtractedTable
    doc = _make_doc("report.pdf", "Quarterly data overview.")
    doc.tables = [
        ExtractedTable(table_index=i, page_number=1, headers=["A", "B"], rows=[["1", "2"]])
        for i in range(4)
    ]
    result = _rule_based_classify(doc)
    # 4 tables → +2 to financial score
    assert result.document_type == "financial"
