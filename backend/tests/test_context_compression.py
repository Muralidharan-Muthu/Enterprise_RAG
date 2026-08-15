"""Unit tests for context_compression_service (extractive, cross-encoder based).

The cross-encoder is monkeypatched with a deterministic keyword scorer so these
run fast (no model load) and are not marked slow.
"""
import pytest

from app.config import settings
from app.services import context_compression_service as ccs
from app.services import reranker_service
from app.services.retriever_service import RetrievedChunk


# ── Helpers ──────────────────────────────────────────────────────────────────

def _chunk(text, store_type="vector", **kw):
    return RetrievedChunk(
        chunk_id=kw.pop("chunk_id", "c1"),
        document_id="d1",
        text=text,
        store_type=store_type,
        distance=0.2,
        relevance_score=0.9,
        **kw,
    )


@pytest.fixture
def keyword_scorer(monkeypatch):
    """Patch reranker_service.score_pairs: +6 logit when the sentence contains
    the query word 'apple', else -6. sigmoid(+6)≈0.9975 (kept), sigmoid(-6)≈
    0.0025 (dropped, below the 0.30 default keep threshold)."""
    def fake_score_pairs(pairs):
        out = []
        for query, sentence in pairs:
            out.append(6.0 if "apple" in sentence.lower() else -6.0)
        return out

    monkeypatch.setattr(reranker_service, "score_pairs", fake_score_pairs)
    return fake_score_pairs


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_ENABLED", True)
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_MIN_SENTENCES", 4)
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_KEEP_SCORE", 0.30)
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_MAX_SENTENCES", 6)
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_MIN_KEEP", 1)
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_MAX_PAIRS", 120)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_drops_irrelevant_sentences_keeps_relevant(keyword_scorer):
    text = (
        "Apple reported strong revenue. The weather was nice yesterday. "
        "Apple also grew its services segment. A cat sat on the mat."
    )
    c = _chunk(text)
    ccs.compress_chunks("apple financials", [c])

    assert c.compressed_text is not None
    assert "Apple reported strong revenue." in c.compressed_text
    assert "Apple also grew its services segment." in c.compressed_text
    assert "weather" not in c.compressed_text
    assert "cat sat" not in c.compressed_text


def test_never_mutates_original_text(keyword_scorer):
    text = (
        "Apple reported strong revenue. The weather was nice yesterday. "
        "Apple also grew its services segment. A cat sat on the mat."
    )
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])
    # citation fidelity: original text untouched
    assert c.text == text


def test_preserves_original_sentence_order(keyword_scorer):
    text = (
        "Apple one. Filler two here. Apple three. Filler four here. Apple five."
    )
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])
    idx1 = c.compressed_text.index("Apple one")
    idx3 = c.compressed_text.index("Apple three")
    idx5 = c.compressed_text.index("Apple five")
    assert idx1 < idx3 < idx5


def test_never_empties_a_chunk(keyword_scorer):
    # No sentence contains 'apple' → all score low, but min_keep guarantees >=1.
    text = "First filler line. Second filler line. Third filler. Fourth filler here."
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])
    # Either left uncompressed (None) or compressed to a non-empty subset —
    # never blanked. With all-low scores and min_keep=1 it keeps the top sentence,
    # which is shorter than the original, so compressed_text is set & non-empty.
    if c.compressed_text is not None:
        assert c.compressed_text.strip()


def test_tables_are_exempt(keyword_scorer):
    text = "Apple row. Filler row. Apple row two. Filler row two here."
    c = _chunk(text, store_type="table", table_markdown="| Apple | 1 |")
    ccs.compress_chunks("apple", [c])
    assert c.compressed_text is None  # tables kept verbatim


def test_graph_chunks_are_exempt(keyword_scorer):
    text = "Apple sentence. Filler sentence. Apple two. Filler two here."
    c = _chunk(text, from_graph=True)
    ccs.compress_chunks("apple", [c])
    assert c.compressed_text is None  # graph chunks deliberately low-similarity


def test_short_chunks_skipped(keyword_scorer):
    # Only 2 sentences (< MIN_SENTENCES=4) → not compressed.
    c = _chunk("Apple one. Filler two here.")
    ccs.compress_chunks("apple", [c])
    assert c.compressed_text is None


def test_disabled_flag_is_noop(monkeypatch, keyword_scorer):
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_ENABLED", False)
    text = "Apple one. Filler two. Apple three. Filler four here."
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])
    assert c.compressed_text is None


def test_scoring_failure_is_best_effort(monkeypatch):
    def boom(pairs):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(reranker_service, "score_pairs", boom)

    text = "Apple one. Filler two. Apple three. Filler four here."
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])  # must not raise
    assert c.compressed_text is None


def test_max_sentences_cap(monkeypatch, keyword_scorer):
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_MAX_SENTENCES", 2)
    text = "Apple a. Apple b. Apple c. Apple d. Apple e."  # 5 relevant sentences
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])
    # All score high, but cap keeps only 2.
    kept = ccs.split_sentences(c.compressed_text)
    assert len(kept) == 2


def test_split_sentences_handles_abbreviations():
    out = ccs.split_sentences("Dr. Smith met Mr. Jones. They discussed the deal.")
    assert out == ["Dr. Smith met Mr. Jones.", "They discussed the deal."]


def test_pair_budget_bounds_scoring(monkeypatch):
    monkeypatch.setattr(settings, "CONTEXT_COMPRESSION_MAX_PAIRS", 5)
    seen = {}

    def counting_scorer(pairs):
        seen["n"] = len(pairs)
        return [6.0] * len(pairs)

    monkeypatch.setattr(reranker_service, "score_pairs", counting_scorer)

    # 8 sentences in one chunk, budget is 5 → at most 5 pairs scored.
    text = " ".join(f"Apple sentence number {i}." for i in range(8))
    c = _chunk(text)
    ccs.compress_chunks("apple", [c])
    assert seen["n"] <= 5
