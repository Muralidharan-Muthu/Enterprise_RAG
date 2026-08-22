"""Tests for chunker.py"""
import pytest
from app.services.chunker import chunk_document, extract_legal_clauses
from app.models.document import ParsedDocument, TextBlock


def _make_doc(blocks: list[tuple[str, str]], n_tables: int = 0) -> ParsedDocument:
    text_blocks = [
        TextBlock(text=t, page_number=i + 1, block_type=bt, token_count=len(t.split()))
        for i, (t, bt) in enumerate(blocks)
    ]
    return ParsedDocument(
        doc_id="test",
        filename="test.pdf",
        raw_text=" ".join(t for t, _ in blocks),
        text_blocks=text_blocks,
        tables=[],
        page_count=1,
        word_count=sum(len(t.split()) for t, _ in blocks),
        has_tables=n_tables > 0,
        has_images=False,
    )


def test_chunk_policy_basic():
    doc = _make_doc([
        ("This is a policy document about data governance and privacy requirements.", "paragraph"),
        ("All employees must comply with these guidelines as per company standards.", "paragraph"),
        ("Section 2 outlines the process for handling customer data securely.", "paragraph"),
    ])
    chunks = chunk_document(doc, "policy")
    assert len(chunks) >= 1
    for c in chunks:
        assert len(c.chunk_text.strip()) > 0
        assert c.chunk_index >= 0
        assert c.page_number >= 1


def test_chunk_count_reasonable():
    # 20 blocks should produce at least 1 chunk and not 20 separate chunks
    blocks = [(f"Paragraph {i}: " + "word " * 30, "paragraph") for i in range(20)]
    doc = _make_doc(blocks)
    chunks = chunk_document(doc, "policy")
    assert 1 <= len(chunks) <= 20


def test_chunk_index_sequential():
    blocks = [(f"Para {i}: " + "word " * 50, "paragraph") for i in range(10)]
    doc = _make_doc(blocks)
    chunks = chunk_document(doc, "policy")
    indices = [c.chunk_index for c in chunks]
    assert indices == sorted(indices)


def test_legal_clause_extraction():
    doc = _make_doc([
        ("1. DEFINITIONS", "header"),
        ("1.1 Agreement means this contract between the parties.", "paragraph"),
        ("1.2 Party means any signatory to this agreement.", "paragraph"),
        ("2. OBLIGATIONS", "header"),
        ("2.1 The Contractor shall deliver work within 30 days.", "paragraph"),
    ])
    clauses = extract_legal_clauses(doc)
    assert len(clauses) >= 2
    for clause in clauses:
        assert len(clause.clause_text.strip()) > 0
        assert clause.clause_index >= 0


def test_chunk_entity_type():
    doc = _make_doc([
        ("Overview: This report outlines the corporate hierarchy and subsidiaries.", "paragraph"),
        ("Structure: The parent organization owns 100% of the active retail entities.", "paragraph"),
        ("Operations: Key directors and branch offices are distributed regionally.", "paragraph"),
    ])
    chunks = chunk_document(doc, "entity")
    assert len(chunks) >= 1


def test_detect_clause_nature_numbered_clauses():
    from app.services.chunker import detect_clause_nature

    # Test 1: Termination for cause
    text1 = (
        "1. TERMINATION FOR CAUSE (IMMEDIATE ACTION)\n"
        "RIL's contracts contain unilateral, immediate termination rights in the event of default."
    )
    is_c1, num1, title1, type1 = detect_clause_nature(text1, "Key Contractual Clauses & Legal Framework")
    assert is_c1 is True
    assert num1 == "1"
    assert "TERMINATION FOR CAUSE" in title1
    assert type1 == "termination"

    # Test 2: Termination without cause
    text2 = (
        "2. TERMINATION WITHOUT CAUSE (NOTICE PROVISIONS)\n"
        "Standard operational contracts feature balanced exit options. Termination without cause is strictly governed by written notice."
    )
    is_c2, num2, title2, type2 = detect_clause_nature(text2, "Key Contractual Clauses & Legal Framework")
    assert is_c2 is True
    assert num2 == "2"
    assert "TERMINATION WITHOUT CAUSE" in title2
    assert type2 == "termination"

    # Test 3: Dispute resolution & jurisdiction
    text3 = (
        "3. DISPUTE RESOLUTION & EXCLUSIVE JURISDICTION\n"
        "All commercial agreements feature standard binding dispute resolution clauses. Designated exclusive legal jurisdiction is Mumbai, India."
    )
    is_c3, num3, title3, type3 = detect_clause_nature(text3, "Key Contractual Clauses & Legal Framework")
    assert is_c3 is True
    assert num3 == "3"
    assert "DISPUTE RESOLUTION" in title3
    assert type3 == "dispute_resolution"


def test_convert_chunk_to_legal_clause():
    from app.models.document import Chunk
    from app.services.chunker import convert_chunk_to_legal_clause

    c = Chunk(
        chunk_index=0,
        chunk_text="1. TERMINATION FOR CAUSE\nImmediate termination rights upon bankruptcy or material breach.",
        page_number=7,
        page_numbers=[7],
        section_title="Key Contractual Clauses",
        section_level=1,
        semantic_type="legal_clause",
        keywords=["termination", "breach"],
        token_count=15,
        chunk_metadata={
            "clause_number": "1",
            "clause_title": "TERMINATION FOR CAUSE",
            "clause_type": "termination",
            "risk_level": "high",
            "risk_rationale": "Immediate termination risk without notice",
            "parties_mentioned": ["RIL", "Vendor"],
        }
    )
    lc = convert_chunk_to_legal_clause(c, 0)
    assert lc.clause_index == 0
    assert lc.clause_number == "1"
    assert lc.clause_title == "TERMINATION FOR CAUSE"
    assert lc.clause_type == "termination"
    assert lc.risk_level == "high"
    assert lc.page_number == 7
    assert "RIL" in lc.parties_mentioned
