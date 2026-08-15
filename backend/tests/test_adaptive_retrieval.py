from unittest.mock import patch

import app.api.routes.query as q


# ── _is_complex_query ──────────────────────────────────────────────────────

def test_short_query_is_simple():
    assert q._is_complex_query("What is the termination clause?") is False


def test_long_query_is_complex_by_word_count():
    long_query = " ".join(["word"] * q.settings.ADAPTIVE_COMPLEXITY_WORD_THRESHOLD)
    assert q._is_complex_query(long_query) is True


def test_query_with_comparison_cue_is_complex():
    assert q._is_complex_query("Compare the two contracts") is True
    assert q._is_complex_query("What is the difference between plan A and plan B?") is True


def test_query_with_list_all_cue_is_complex():
    assert q._is_complex_query("List all termination clauses") is True


def test_short_non_cue_query_is_simple():
    assert q._is_complex_query("revenue 2023") is False


# ── _resolve_retrieval_params ───────────────────────────────────────────────

def test_resolve_params_simple_query():
    top_k, rerank_cap = q._resolve_retrieval_params("What is the termination clause?")
    assert top_k == q.settings.ADAPTIVE_TOP_K_SIMPLE
    assert rerank_cap == q.settings.ADAPTIVE_RERANK_CAP_SIMPLE


def test_resolve_params_complex_query():
    top_k, rerank_cap = q._resolve_retrieval_params("Compare all clauses across every contract")
    assert top_k == q.settings.ADAPTIVE_TOP_K_COMPLEX
    assert rerank_cap == q.settings.ADAPTIVE_RERANK_CAP_COMPLEX


def test_disabled_adaptive_retrieval_reproduces_old_constants():
    with patch.object(q.settings, "ADAPTIVE_RETRIEVAL_ENABLED", False):
        top_k, rerank_cap = q._resolve_retrieval_params(
            "Compare all clauses across every contract"
        )
        assert top_k == 10
        assert rerank_cap == q.RERANK_PER_STORE_CAP
