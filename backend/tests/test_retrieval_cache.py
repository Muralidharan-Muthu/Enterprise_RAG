"""Unit tests for the in-process retrieval cache (retrieval_cache.py).

No models, no DB: embedding-service model calls and retrieve()'s internals
are monkeypatched. Follows test_signed_url_cache.py's structure (autouse
state-clearing fixture, counting fakes, monkeypatched monotonic clock).
"""
import numpy as np
import pytest

from app.config import settings
from app.services import retrieval_cache as rc
from app.services import embedding_service as es
from app.services.retriever_service import RetrievedChunk, TableFilters


@pytest.fixture(autouse=True)
def _clear_cache():
    rc.clear_all()
    yield
    rc.clear_all()


def _fake_model(monkeypatch):
    """Replace the BGE model with a counting fake whose output is derived from
    the input text (so distinct queries produce distinct embeddings)."""
    calls = {"n": 0}

    class _FakeModel:
        def encode(self, text, **kwargs):
            calls["n"] += 1
            vec = np.zeros(1024, dtype=np.float32)
            vec[0] = float(len(text))
            return vec

    monkeypatch.setattr(es, "_get_model", lambda: _FakeModel())
    return calls


def _chunk(chunk_id="c1", score=0.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id="d1", text="some text",
        store_type="vector", distance=0.3, relevance_score=score,
    )


# ── Embedding cache ──────────────────────────────────────────────────────────

def test_embedding_cache_hit(monkeypatch):
    calls = _fake_model(monkeypatch)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_EMBEDDINGS_ENABLED", True)

    e1 = es.embed_query("what is the revenue")
    e2 = es.embed_query("what is the revenue")
    assert calls["n"] == 1                      # model ran once
    assert np.array_equal(e1, e2)               # identical output


def test_embedding_cache_distinct_queries(monkeypatch):
    calls = _fake_model(monkeypatch)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_EMBEDDINGS_ENABLED", True)

    es.embed_query("query one")
    es.embed_query("query two")
    assert calls["n"] == 2


def test_embedding_cache_off_by_default(monkeypatch):
    """Shipped default (master flag False): the model runs on every call and
    the cache module's dicts stay empty — proves the change is inert."""
    calls = _fake_model(monkeypatch)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_ENABLED", False)

    es.embed_query("same query")
    es.embed_query("same query")
    assert calls["n"] == 2
    assert len(rc._embedding_cache) == 0


def test_embedding_cache_returns_copy(monkeypatch):
    _fake_model(monkeypatch)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_EMBEDDINGS_ENABLED", True)

    e1 = es.embed_query("q")
    e1[0] = -999.0                              # mutate caller's copy
    e2 = es.embed_query("q")
    assert e2[0] != -999.0                      # cached value unaffected


# ── Result cache ─────────────────────────────────────────────────────────────

def test_result_cache_roundtrip_and_ttl(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(rc.time, "monotonic", lambda: clock["t"])

    args = ("q", ["financial"], None, 10, True, None, None)
    rc.put_cached_retrieval(*args, results=[_chunk()])

    assert rc.get_cached_retrieval(*args) is not None       # hit before expiry
    clock["t"] += settings.RETRIEVAL_CACHE_RESULT_TTL_SECONDS - 1
    assert rc.get_cached_retrieval(*args) is not None       # still valid
    clock["t"] += 2                                          # past expiry
    assert rc.get_cached_retrieval(*args) is None            # expired


def test_result_cache_key_differentiation():
    rc.put_cached_retrieval("q", None, "doc-A", 10, True, None, None,
                            results=[_chunk("a")])
    # Different document_id → distinct key, no false hit
    assert rc.get_cached_retrieval("q", None, "doc-B", 10, True, None, None) is None
    # Different table_filters → distinct key
    tf = TableFilters(currency="USD")
    assert rc.get_cached_retrieval("q", None, "doc-A", 10, True, None, tf) is None
    # Exact same key → hit
    assert rc.get_cached_retrieval("q", None, "doc-A", 10, True, None, None) is not None


def test_result_cache_hit_returns_independent_copies():
    """Downstream reranking mutates relevance_score in place — two hits must
    not share objects, or one request's rerank corrupts the other's."""
    args = ("q", None, None, 10, True, None, None)
    rc.put_cached_retrieval(*args, results=[_chunk(score=0.0)])

    first = rc.get_cached_retrieval(*args)
    first[0].relevance_score = 0.99             # simulate reranker mutation
    second = rc.get_cached_retrieval(*args)
    assert second[0].relevance_score == 0.0     # unaffected by first's mutation


def test_result_cache_size_bound(monkeypatch):
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_RESULT_MAX_ENTRIES", 3)
    for i in range(5):
        rc.put_cached_retrieval(f"q{i}", None, None, 10, True, None, None,
                                results=[_chunk(str(i))])
    assert len(rc._result_cache) <= 3
