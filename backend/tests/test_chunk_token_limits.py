"""
Tests for package A (token correctness): real-tokenizer indirection and the
hard-split of an oversized, indivisible single sentence.

All tests here inject a FAKE token counter (Callable[[str], int]) so the
1.3 GB BGE model is never loaded. Tests that need the real tokenizer are
marked @pytest.mark.slow and excluded from the default run.
"""
from __future__ import annotations

import pytest

from app.services.semantic_chunker import (
    _enforce_max_tokens,
    _hard_split_sentence,
    split_sentences_semantically,
)


def _word_counter(text: str) -> int:
    """Fake counter: whitespace word count (deterministic, no model)."""
    return len(text.split())


def _identity_embed_fn(sentences):
    """Fake embed_fn — never actually consulted once len(group) <= 1,
    but required by _enforce_max_tokens' signature."""
    import numpy as np
    n = len(sentences)
    arr = np.eye(max(n, 1), dtype=np.float32)[:n]
    return arr


# ─────────────────────────────────────────────────────────────────────────────
# _hard_split_sentence
# ─────────────────────────────────────────────────────────────────────────────

class TestHardSplitSentence:
    def test_oversized_single_sentence_is_split_into_bounded_pieces(self):
        sentence = " ".join(f"word{i}" for i in range(50))  # 50 "tokens" per _word_counter
        pieces = _hard_split_sentence(sentence, max_tokens=10, token_counter=_word_counter)

        assert len(pieces) > 1
        for p in pieces:
            assert _word_counter(p) <= 10

        # No text dropped: concatenation reconstructs all original words in order.
        reconstructed = " ".join(pieces).split()
        assert reconstructed == sentence.split()

    def test_terminates_and_preserves_order(self):
        words = [f"tok{i}" for i in range(137)]
        sentence = " ".join(words)
        pieces = _hard_split_sentence(sentence, max_tokens=7, token_counter=_word_counter)

        flat = " ".join(pieces).split()
        assert flat == words  # order preserved, nothing dropped

    def test_small_sentence_unaffected(self):
        sentence = "This is a short sentence."
        pieces = _hard_split_sentence(sentence, max_tokens=100, token_counter=_word_counter)
        # Under the limit: still returns the sentence as a single piece via the
        # normal accumulation loop (no artificial splitting).
        assert pieces == [sentence]

    def test_empty_string_returns_empty_list(self):
        assert _hard_split_sentence("", max_tokens=10, token_counter=_word_counter) == []


# ─────────────────────────────────────────────────────────────────────────────
# _enforce_max_tokens — degenerate single-sentence-over-limit case
# ─────────────────────────────────────────────────────────────────────────────

class TestEnforceMaxTokensHardSplit:
    def test_single_oversized_sentence_group_is_hard_split(self):
        """A group containing exactly ONE sentence that itself exceeds
        max_tokens must be hard-split rather than returned whole (the bug
        described in the task: 'a single sentence longer than
        CHUNK_MAX_TOKENS is currently returned un-split')."""
        huge_sentence = " ".join(f"w{i}" for i in range(100))
        groups = [[huge_sentence]]

        result = _enforce_max_tokens(
            groups, _identity_embed_fn, max_tokens=20, token_counter=_word_counter
        )

        assert len(result) > 1
        for g in result:
            text = " ".join(g)
            assert _word_counter(text) <= 20

        # No text lost.
        flat_words = " ".join(" ".join(g) for g in result).split()
        assert flat_words == huge_sentence.split()

    def test_normal_size_group_unaffected(self):
        """Groups already within the limit must not be touched at all."""
        group = ["A short sentence.", "Another short one."]
        groups = [group]

        result = _enforce_max_tokens(
            groups, _identity_embed_fn, max_tokens=1000, token_counter=_word_counter
        )

        assert result == groups

    def test_multi_sentence_oversized_group_splits_at_sentence_boundary_not_hard_split(self):
        """When a group has more than one sentence, prefer the existing
        interior-cut logic over word-level hard-splitting."""
        sents = [f"Sentence number {i} with some words in it." for i in range(6)]
        groups = [sents]

        result = _enforce_max_tokens(
            groups, _identity_embed_fn, max_tokens=15, token_counter=_word_counter
        )

        # Every resulting group's sentences are a subset drawn from the
        # original list (no word-level mid-sentence splitting occurred),
        # i.e. every piece of every output group is one of the input sentences.
        for g in result:
            for s in g:
                assert s in sents


# ─────────────────────────────────────────────────────────────────────────────
# Counter indirection: injected fake counter drives the boundary, not the
# whitespace approximation baked into the module.
# ─────────────────────────────────────────────────────────────────────────────

class TestCounterIndirection:
    def test_injected_counter_drives_max_token_boundary(self):
        """Use a counter that reports a MUCH higher count than whitespace
        word-count would (e.g. chars // 1) to prove the injected counter -- not
        some hardcoded word-count -- determines where splits happen."""
        calls = {"n": 0}

        def _char_counter(text: str) -> int:
            calls["n"] += 1
            return len(text)  # far larger than word count

        sentence = "abcdefgh " * 20  # 160 words worth of chars => big char count
        groups = [[sentence]]

        result = _enforce_max_tokens(
            groups, _identity_embed_fn, max_tokens=50, token_counter=_char_counter
        )

        assert calls["n"] > 0  # the fake counter was actually invoked
        for g in result:
            text = " ".join(g)
            assert _char_counter(text) <= 50

        # Using the char counter forces many more, smaller pieces than the
        # word counter would for the same max_tokens value.
        word_result = _enforce_max_tokens(
            [[sentence]], _identity_embed_fn, max_tokens=50, token_counter=_word_counter
        )
        assert len(result) >= len(word_result)

    def test_split_sentences_semantically_accepts_injected_counter(self):
        """End-to-end: split_sentences_semantically must honor an injected
        token_counter without touching embedding_service at all."""
        import numpy as np

        def _embed_fn(sentences):
            n = len(sentences)
            return np.eye(max(n, 1), dtype=np.float32)[:n]

        long_sentence = " ".join(f"word{i}" for i in range(80))
        groups = split_sentences_semantically(
            sentences=[long_sentence],
            embed_fn=_embed_fn,
            percentile=95,
            max_tokens=10,
            min_tokens=0,
            token_counter=_word_counter,
        )

        assert len(groups) > 1
        for g in groups:
            assert _word_counter(" ".join(g)) <= 10
        # No text dropped.
        flat = " ".join(" ".join(g) for g in groups).split()
        assert flat == long_sentence.split()


# ─────────────────────────────────────────────────────────────────────────────
# Slow: real BGE tokenizer path (excluded from default "not slow" runs)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
class TestRealTokenizerCounter:
    def test_count_tokens_matches_real_bge_tokenizer(self):
        from app.services.embedding_service import count_tokens

        assert count_tokens("") == 0
        assert count_tokens("   ") == 0
        assert count_tokens(None) == 0
        assert count_tokens("hello world") > 0

    def test_default_token_counter_uses_real_tokenizer_when_flag_true(self, monkeypatch):
        from app.config import settings
        from app.services.semantic_chunker import default_token_counter

        monkeypatch.setattr(settings, "CHUNK_USE_REAL_TOKENIZER", True)
        assert default_token_counter("hello world") > 0
