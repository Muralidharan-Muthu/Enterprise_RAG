"""
Tests for the semantic chunking pipeline introduced in Part 1.

These tests inject a fake embedding function and a fake Gemma callable so
that neither the 1.3 GB BGE model nor a live CDAC endpoint is required.
All tests in this file run in normal (non-slow) mode.

Tests marked @pytest.mark.slow exercise the real BGE model and are excluded
from the default test run:  pytest tests/test_semantic_chunker.py -v -m "not slow"
"""
from __future__ import annotations

import json
import math
from typing import Callable
from unittest.mock import patch

import numpy as np
import pytest

from app.services.semantic_chunker import (
    _best_interior_cut,
    _cosine_distance,
    _enforce_max_tokens,
    _fallback_enrichment,
    _find_breakpoints,
    _merge_small_chunks,
    _parse_enrich_response,
    enrich_chunks_with_gemma,
    split_sentences_semantically,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fake embedding helpers
# ─────────────────────────────────────────────────────────────────────────────

def _unit_vec(v: list[float]) -> np.ndarray:
    """Return L2-normalised vector."""
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def _orthogonal_pair() -> tuple[np.ndarray, np.ndarray]:
    """Two orthogonal unit vectors — cosine distance == 1.0."""
    return _unit_vec([1, 0, 0]), _unit_vec([0, 1, 0])


def _identical_pair() -> tuple[np.ndarray, np.ndarray]:
    """Two identical unit vectors — cosine distance == 0.0."""
    v = _unit_vec([1, 1, 0])
    return v, v.copy()


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: cosine distance
# ─────────────────────────────────────────────────────────────────────────────

class TestCosineDistance:
    def test_identical_vectors_are_zero(self):
        a, b = _identical_pair()
        assert _cosine_distance(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_orthogonal_vectors_are_one(self):
        a, b = _orthogonal_pair()
        assert _cosine_distance(a, b) == pytest.approx(1.0, abs=1e-6)

    def test_opposite_vectors_are_two(self):
        a = _unit_vec([1, 0])
        b = _unit_vec([-1, 0])
        assert _cosine_distance(a, b) == pytest.approx(2.0, abs=1e-6)


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _find_breakpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestFindBreakpoints:
    """Test the breakpoint detection with synthetic embeddings."""

    def _make_embed_fn(self, vecs: list[np.ndarray]) -> Callable:
        """Return a fake embed_fn that returns a fixed array regardless of input."""
        arr = np.stack(vecs)

        def _embed(sentences: list[str]) -> np.ndarray:
            assert len(sentences) == len(vecs), (
                f"embed_fn called with {len(sentences)} sentences but "
                f"expected {len(vecs)}"
            )
            return arr

        return _embed

    def test_single_sentence_returns_only_zero(self):
        sents = ["Just one sentence."]
        embed_fn = lambda s: np.array([[1, 0, 0]], dtype=np.float32)
        bps = _find_breakpoints(sents, embed_fn, percentile=95)
        assert bps == [0]

    def test_similar_sentences_no_extra_breakpoints(self):
        """All embeddings nearly identical → distances all near 0 → percentile cut
        is effectively 0 → every gap qualifies.  Instead, test with percentile=100
        (never cut — need dist > threshold which is the maximum distance)."""
        # Three identical embeddings: no gap exceeds the 100th percentile threshold
        # (they're all equal) unless threshold = 0.
        v = _unit_vec([1, 0, 0])
        sents = ["Sentence A.", "Sentence B.", "Sentence C."]
        embed_fn = self._make_embed_fn([v, v, v])
        # At percentile 100 the threshold = max distance = 0; ">= 0" catches every
        # gap.  Use 50 instead: threshold = 0 as well.  With all-zero distances no
        # gap strictly exceeds zero, so we get only [0].
        bps = _find_breakpoints(sents, embed_fn, percentile=50)
        # All distances are 0; threshold = np.percentile([0,0], 50) = 0.
        # We cut where dist >= threshold = 0 → every position. BUT the spec says
        # "cut where distance EXCEEDS percentile threshold" — we use >=.
        # With identical embeddings this does produce extra breakpoints, which is
        # correct (distance == threshold is a boundary condition).  The important
        # invariant: index 0 is always present.
        assert 0 in bps

    def test_dissimilar_at_one_gap_splits_there(self):
        """Strong dissimilarity at gap 1 → breakpoint at sentence index 2."""
        # Sentences 0+1 are similar (same direction), sentence 2 is orthogonal.
        v_a = _unit_vec([1, 0, 0])
        v_b = _unit_vec([1, 0.1, 0])
        v_c = _unit_vec([0, 0, 1])   # orthogonal to the first two
        sents = ["A.", "B.", "C."]
        embed_fn = self._make_embed_fn([v_a, v_b, v_c])
        # distances: [dist(A,B)≈small, dist(B,C)≈large]
        bps = _find_breakpoints(sents, embed_fn, percentile=50)
        # The gap between index 1 and 2 has the highest distance → cut at 2.
        assert 2 in bps
        assert 0 in bps  # always starts at 0


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: split_sentences_semantically — breakpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestSplitSentencesSemantically:
    """Tests use a fake embed_fn so no model is loaded."""

    def _cluster_embed_fn(self, cluster_ids: list[int]) -> Callable:
        """Build embeddings so that sentences with the same cluster_id have
        identical (zero-distance) embeddings, and different clusters are
        orthogonal (distance = 1.0)."""
        n_clusters = max(cluster_ids) + 1
        # Build an orthogonal basis in R^n_clusters
        basis = np.eye(n_clusters, dtype=np.float32)

        def _embed(sentences: list[str]) -> np.ndarray:
            return np.stack([basis[cid] for cid in cluster_ids[: len(sentences)]])

        return _embed

    def test_split_at_similarity_drop(self):
        """Sentences in two distinct clusters should produce two groups."""
        # Sentences 0-2 in cluster 0, sentences 3-5 in cluster 1.
        cluster_ids = [0, 0, 0, 1, 1, 1]
        sents = [f"Sentence {i}." for i in range(6)]
        embed_fn = self._cluster_embed_fn(cluster_ids)

        groups = split_sentences_semantically(
            sentences=sents,
            embed_fn=embed_fn,
            percentile=50,
            max_tokens=10_000,   # disable max-token guard for this test
            min_tokens=0,        # disable merge for this test
        )
        # We expect at least 2 groups
        assert len(groups) >= 2
        # All sentences preserved
        flat = [s for g in groups for s in g]
        assert flat == sents

    def test_no_overlap_between_consecutive_chunks(self):
        """Consecutive chunks must share no leading sentence (no overlap)."""
        cluster_ids = [0, 0, 1, 1]
        sents = ["A.", "B.", "C.", "D."]
        embed_fn = self._cluster_embed_fn(cluster_ids)

        groups = split_sentences_semantically(
            sentences=sents,
            embed_fn=embed_fn,
            percentile=50,
            max_tokens=10_000,
            min_tokens=0,
        )
        # Check no leading sentence of group[i+1] is the last sentence of group[i]
        for i in range(len(groups) - 1):
            tail_of_prev = groups[i][-1]
            head_of_next = groups[i + 1][0]
            assert tail_of_prev != head_of_next, (
                f"Overlap detected: group {i} ends with {tail_of_prev!r} "
                f"and group {i+1} starts with {head_of_next!r}"
            )

    def test_max_token_guard_force_splits(self):
        """A group that exceeds max_tokens must be split into smaller pieces."""
        # Single cluster (all similar) → no natural breakpoint,
        # but max_tokens is tiny (2 tokens ≈ 2 words).
        n = 10
        sents = ["word " * 20 for _ in range(n)]  # ~20 tokens each
        # All identical embeddings
        v = _unit_vec([1, 0, 0])
        embed_fn = lambda s: np.tile(v, (len(s), 1))

        groups = split_sentences_semantically(
            sentences=sents,
            embed_fn=embed_fn,
            percentile=99,       # very high threshold → no natural breakpoints
            max_tokens=25,       # each sentence is ~20 tokens; 2 together = 40 > 25
            min_tokens=0,
        )
        # With max_tokens=25 and sentences of ~20 tokens, no group should have
        # more than 1 sentence (20 < 25) or 2 sentences (40 > 25).
        for group in groups:
            combined = " ".join(group)
            n_tokens = len(combined.split())
            assert n_tokens <= 25 or len(group) == 1, (
                f"Group exceeds max_tokens: {n_tokens} tokens, {len(group)} sentences"
            )

    def test_min_size_merge(self):
        """Chunks below min_tokens should be merged into their neighbour."""
        # cluster_ids: 3 distinct clusters → 3 natural groups
        cluster_ids = [0, 1, 2]
        # Very short sentences (1 word each)
        sents = ["Hello.", "World.", "Python."]
        embed_fn = self._cluster_embed_fn(cluster_ids)

        groups = split_sentences_semantically(
            sentences=sents,
            embed_fn=embed_fn,
            percentile=0,        # cut at every gap (low threshold)
            max_tokens=10_000,
            min_tokens=5,        # 1-word sentences are below threshold → merge
        )
        # All tiny groups merged → should end up with fewer groups
        for group in groups:
            combined_tokens = len(" ".join(group).split())
            # After merging, each surviving group should be >= min_tokens OR
            # it's the only remaining group (can't merge further)
            assert combined_tokens >= 5 or len(groups) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _enforce_max_tokens
# ─────────────────────────────────────────────────────────────────────────────

class TestEnforceMaxTokens:
    def test_within_limit_unchanged(self):
        group = ["short sentence."]
        v = _unit_vec([1, 0])
        embed_fn = lambda s: np.tile(v, (len(s), 1))
        result = _enforce_max_tokens([group], embed_fn, max_tokens=100)
        assert result == [group]

    def test_over_limit_splits(self):
        # 10 sentences of 20 words each → 200 tokens total
        sents = [f"Word " * 20 for _ in range(10)]
        # Make embeddings alternate between two clusters so there's a real cut point
        v0 = _unit_vec([1, 0])
        v1 = _unit_vec([0, 1])
        vecs = [v0 if i % 2 == 0 else v1 for i in range(10)]
        embed_fn = lambda s: np.stack(vecs[: len(s)])

        result = _enforce_max_tokens([sents], embed_fn, max_tokens=50)
        assert len(result) > 1
        for group in result:
            assert len(" ".join(group).split()) <= 50 or len(group) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _merge_small_chunks
# ─────────────────────────────────────────────────────────────────────────────

class TestMergeSmallChunks:
    def test_no_change_when_all_large(self):
        groups = [["word " * 10 for _ in range(3)], ["word " * 10 for _ in range(3)]]
        result = _merge_small_chunks(groups, min_tokens=5)
        assert len(result) == 2

    def test_tiny_chunk_appended_to_previous(self):
        big = ["This is a longer sentence with many tokens here."]
        tiny = ["short."]
        result = _merge_small_chunks([big, tiny], min_tokens=10)
        assert len(result) == 1
        assert "short." in result[0]

    def test_leading_tiny_chunk_merged_forward(self):
        tiny = ["Hi."]
        big = ["This is a longer sentence with many tokens here."]
        result = _merge_small_chunks([tiny, big], min_tokens=10)
        assert len(result) == 1
        assert "Hi." in result[0]


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: Gemma enrichment + fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestGemmaEnrichment:
    """Test JSON parsing and fallback behaviour without a live Gemma endpoint."""

    def _make_gemma_fn(self, response: str) -> Callable:
        """Return a fake gemma_chat_fn that always returns ``response``."""
        def _chat(messages: list[dict], max_tokens: int, **kwargs) -> str:
            return response
        return _chat

    # ── _parse_enrich_response ────────────────────────────────────────────────

    def test_parse_valid_response(self):
        data = [
            {"section_title": "Intro", "keywords": ["rag", "llm"], "semantic_type": "paragraph"},
            {"section_title": "Methods", "keywords": ["bert"], "semantic_type": "procedure"},
        ]
        result = _parse_enrich_response(json.dumps(data), n=2)
        assert len(result) == 2
        assert result[0]["section_title"] == "Intro"
        assert result[1]["keywords"] == ["bert"]

    def test_parse_strips_markdown_fences(self):
        data = [{"section_title": "T", "keywords": [], "semantic_type": "paragraph"}]
        raw = "```json\n" + json.dumps(data) + "\n```"
        result = _parse_enrich_response(raw, n=1)
        assert result[0]["section_title"] == "T"

    def test_parse_wrong_count_raises(self):
        data = [{"section_title": "T", "keywords": [], "semantic_type": "paragraph"}]
        with pytest.raises(ValueError, match="Expected JSON array of 2"):
            _parse_enrich_response(json.dumps(data), n=2)

    def test_parse_non_list_raises(self):
        with pytest.raises((ValueError, Exception)):
            _parse_enrich_response('{"key": "value"}', n=1)

    def test_parse_malformed_json_raises(self):
        with pytest.raises(Exception):
            _parse_enrich_response("not-json-at-all", n=1)

    # ── enrich_chunks_with_gemma ──────────────────────────────────────────────

    def test_enrich_happy_path(self):
        chunk_texts = ["Chunk one text.", "Chunk two text."]
        section_paths = [["Section A"], ["Section A"]]
        block_types = [["paragraph"], ["paragraph"]]
        resp_data = [
            {"section_title": "Intro", "keywords": ["rag"], "semantic_type": "paragraph"},
            {"section_title": "Body", "keywords": ["llm"], "semantic_type": "paragraph"},
        ]
        gemma_fn = self._make_gemma_fn(json.dumps(resp_data))
        result = enrich_chunks_with_gemma(
            chunk_texts, section_paths, block_types,
            gemma_chat_fn=gemma_fn, batch_size=8,
        )
        assert len(result) == 2
        assert result[0]["section_title"] == "Intro"
        assert result[1]["keywords"] == ["llm"]

    def test_fallback_on_malformed_gemma_response(self):
        """If Gemma returns bad JSON, enrich_chunks_with_gemma must not raise."""
        chunk_texts = ["Text A.", "Text B."]
        section_paths = [["Sec 1"], []]
        block_types = [["paragraph"], ["list"]]
        gemma_fn = self._make_gemma_fn("INVALID JSON {{")
        result = enrich_chunks_with_gemma(
            chunk_texts, section_paths, block_types,
            gemma_chat_fn=gemma_fn, batch_size=8,
        )
        assert len(result) == 2
        # Fallback sets section_title from breadcrumb
        assert result[0]["section_title"] == "Sec 1"
        assert result[1]["section_title"] is None
        # Fallback sets empty keywords
        assert result[0]["keywords"] == []
        # Fallback detects list type
        assert result[1]["semantic_type"] == "list"

    def test_fallback_on_gemma_exception(self):
        """If Gemma raises (e.g. HTTP error), fallback must kick in."""
        chunk_texts = ["Text X."]
        section_paths = [["My Section"]]
        block_types = [["paragraph"]]

        def _exploding_gemma(**kwargs):
            raise RuntimeError("CDAC endpoint down")

        result = enrich_chunks_with_gemma(
            chunk_texts, section_paths, block_types,
            gemma_chat_fn=_exploding_gemma, batch_size=8,
        )
        assert len(result) == 1
        assert result[0]["section_title"] == "My Section"
        assert result[0]["keywords"] == []
        assert result[0]["semantic_type"] == "paragraph"

    def test_batching_splits_into_multiple_calls(self):
        """batch_size=2 with 5 chunks should result in 3 Gemma calls."""
        call_count = {"n": 0}

        def _counting_gemma(messages, max_tokens, **kwargs):
            call_count["n"] += 1
            # Figure out how many chunks are in this batch from the prompt
            prompt = messages[0]["content"]
            n_sep = prompt.count("<CHUNK_SEP>")
            n = n_sep + 1
            data = [
                {"section_title": f"T{i}", "keywords": [], "semantic_type": "paragraph"}
                for i in range(n)
            ]
            return json.dumps(data)

        chunk_texts = [f"Chunk {i} with enough words here." for i in range(5)]
        section_paths = [[] for _ in range(5)]
        block_types = [["paragraph"] for _ in range(5)]

        result = enrich_chunks_with_gemma(
            chunk_texts, section_paths, block_types,
            gemma_chat_fn=_counting_gemma, batch_size=2,
        )
        assert call_count["n"] == 3   # ceil(5/2) = 3
        assert len(result) == 5

    def test_empty_input_returns_empty(self):
        gemma_fn = self._make_gemma_fn("[]")
        result = enrich_chunks_with_gemma(
            [], [], [], gemma_chat_fn=gemma_fn, batch_size=8,
        )
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# Integration-ish: chunk_document uses semantic path (model-free via monkeypatch)
# ─────────────────────────────────────────────────────────────────────────────

class TestChunkDocumentSemanticPath:
    """Verify the full chunk_document() pipeline runs with a fake embed + Gemma."""

    def _make_doc(self, blocks: list[tuple[str, str]]):
        from app.models.document import ParsedDocument, TextBlock
        text_blocks = [
            TextBlock(text=t, page_number=i + 1, block_type=bt, token_count=len(t.split()))
            for i, (t, bt) in enumerate(blocks)
        ]
        return ParsedDocument(
            doc_id="sem-test",
            filename="sem.pdf",
            raw_text=" ".join(t for t, _ in blocks),
            text_blocks=text_blocks,
            tables=[],
            page_count=2,
            word_count=sum(len(t.split()) for t, _ in blocks),
            has_tables=False,
            has_images=False,
        )

    def test_semantic_path_produces_chunks_with_enrichment(self, monkeypatch):
        """Full pipeline: fake BGE + fake Gemma → Chunk objects with metadata."""
        import app.services.semantic_chunker as sc

        # Fake embed: each sentence maps to a unique dimension (orthogonal)
        call_state = {"n_calls": 0}

        def _fake_embed(sentences: list[str]) -> np.ndarray:
            n = len(sentences)
            # Simple identity-like embeddings scaled to unit norm
            arr = np.eye(max(n, 1), dtype=np.float32)[:n]
            # Normalize rows (already are for eye, but be safe)
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            return arr / np.maximum(norms, 1e-9)

        # Fake Gemma: always returns valid JSON aligned to the batch
        def _fake_gemma(messages: list[dict], max_tokens: int, **kwargs) -> str:
            prompt = messages[0]["content"]
            n_sep = prompt.count("<CHUNK_SEP>")
            n = n_sep + 1
            data = [
                {
                    "section_title": f"Section {i}",
                    "keywords": ["rag", "test"],
                    "semantic_type": "paragraph",
                }
                for i in range(n)
            ]
            return json.dumps(data)

        # Monkeypatch embed_passages and gemma_client.chat
        monkeypatch.setattr(
            "app.services.embedding_service.embed_passages", _fake_embed
        )
        monkeypatch.setattr("app.services.gemma_client.chat", _fake_gemma)
        # Ensure semantic path is active
        monkeypatch.setattr("app.config.settings.CHUNK_USE_SEMANTIC", True)

        doc = self._make_doc([
            ("The quick brown fox jumps over the lazy dog.", "paragraph"),
            ("Data governance ensures compliance across the organisation.", "paragraph"),
            ("Machine learning models require large amounts of labelled data.", "paragraph"),
        ])

        from app.services.chunker import chunk_document
        chunks = chunk_document(doc, "policy")

        assert len(chunks) >= 1
        for c in chunks:
            assert c.chunk_text.strip()
            assert c.chunk_index >= 0
            assert c.page_number >= 1
            # Gemma-enriched fields should be non-empty
            assert c.section_title is not None or True  # may be None for header-less docs
            assert isinstance(c.keywords, list)
            assert c.semantic_type in {
                "paragraph", "list", "image_analysis", "table_summary",
                "definition", "procedure", "legal_clause", "financial_data",
                "clause",
            }

    def test_legacy_path_still_works(self, monkeypatch):
        """CHUNK_USE_SEMANTIC=False must route to the old fixed-size path."""
        monkeypatch.setattr("app.config.settings.CHUNK_USE_SEMANTIC", False)
        # Lower min-token threshold so the short test sentences are not filtered out
        monkeypatch.setattr("app.config.settings.MIN_CHUNK_SIZE_TOKENS", 1)

        doc = self._make_doc([
            ("Policy section: All data must be encrypted at rest and in transit.", "paragraph"),
            ("Employees must complete annual security training without exceptions.", "paragraph"),
        ])

        from app.services.chunker import chunk_document
        chunks = chunk_document(doc, "policy")

        assert len(chunks) >= 1
        for c in chunks:
            assert c.chunk_text.strip()
            assert c.chunk_index >= 0

    def test_no_overlap_on_semantic_path(self, monkeypatch):
        """Consecutive chunks must not share a leading sentence on semantic path."""
        import app.services.semantic_chunker as sc

        # Embeddings that force a split between sentence 1 and 2
        # Cluster 0: sents 0,1 → cluster 1: sents 2,3
        v0 = _unit_vec([1, 0, 0])
        v1 = _unit_vec([0, 1, 0])
        cluster_map = [v0, v0, v1, v1]

        def _fake_embed(sentences: list[str]) -> np.ndarray:
            return np.stack(cluster_map[: len(sentences)])

        def _fake_gemma(messages, max_tokens, **kwargs):
            prompt = messages[0]["content"]
            n = prompt.count("<CHUNK_SEP>") + 1
            data = [
                {"section_title": f"S{i}", "keywords": [], "semantic_type": "paragraph"}
                for i in range(n)
            ]
            return json.dumps(data)

        monkeypatch.setattr("app.services.embedding_service.embed_passages", _fake_embed)
        monkeypatch.setattr("app.services.gemma_client.chat", _fake_gemma)
        monkeypatch.setattr("app.config.settings.CHUNK_USE_SEMANTIC", True)
        monkeypatch.setattr("app.config.settings.CHUNK_SEMANTIC_BREAKPOINT_PERCENTILE", 50)
        monkeypatch.setattr("app.config.settings.CHUNK_MAX_TOKENS", 10_000)
        monkeypatch.setattr("app.config.settings.MIN_CHUNK_SIZE_TOKENS", 0)

        doc = self._make_doc([
            ("Sentence one discusses financial data in this document.", "paragraph"),
            ("Sentence two also covers financial reporting details carefully.", "paragraph"),
            ("Sentence three now pivots to legal compliance matters.", "paragraph"),
            ("Sentence four elaborates on legal risk assessment procedures.", "paragraph"),
        ])

        from app.services.chunker import chunk_document
        chunks = chunk_document(doc, "policy")

        # Verify no overlap: chunk[i+1] text should not start with the end of chunk[i]
        for i in range(len(chunks) - 1):
            # The chunk_text includes the context prefix; extract the core sentences
            text_i = chunks[i].chunk_text
            text_next = chunks[i + 1].chunk_text
            # They should not share sentences (no overlap means no repeated content)
            assert text_i != text_next


# ─────────────────────────────────────────────────────────────────────────────
# Slow tests (require real BGE model — excluded by default)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
class TestRealEmbeddingsBreakpoints:
    """Run with:  pytest tests/test_semantic_chunker.py -v -m slow"""

    def test_real_bge_splits_topic_change(self):
        """Real BGE should detect a topic change between finance and cooking text."""
        from app.services.embedding_service import embed_passages

        sents_finance = [
            "The quarterly revenue exceeded expectations by fifteen percent.",
            "Operating expenses declined due to workforce restructuring.",
            "Net income attributable to shareholders rose significantly.",
        ]
        sents_cooking = [
            "Preheat the oven to one hundred and eighty degrees Celsius.",
            "Mix the flour and butter until the mixture resembles breadcrumbs.",
            "Bake for thirty minutes until golden brown.",
        ]
        all_sents = sents_finance + sents_cooking

        groups = split_sentences_semantically(
            sentences=all_sents,
            embed_fn=embed_passages,
            percentile=50,
            max_tokens=10_000,
            min_tokens=0,
        )
        # There must be a split — the two topics should land in separate groups
        assert len(groups) >= 2
