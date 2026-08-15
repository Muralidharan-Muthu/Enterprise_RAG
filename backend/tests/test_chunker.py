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


def test_chunk_research_type():
    doc = _make_doc([
        ("Abstract: This paper investigates the effects of RAG on LLM accuracy.", "paragraph"),
        ("Methodology: We conducted experiments on 100 documents from enterprise domains.", "paragraph"),
        ("Results: RAG improved accuracy by 23% compared to baseline models.", "paragraph"),
    ])
    chunks = chunk_document(doc, "research")
    assert len(chunks) >= 1
