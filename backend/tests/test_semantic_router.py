"""Unit tests for the embedding-centroid store router (semantic_router.py).

Centroids are monkeypatched to fixed orthonormal vectors so classify()'s
similarity math is exercised deterministically, without loading the real BGE
model. _get_centroids()'s lazy-build path is tested separately with a fake
embed_passages().
"""
import numpy as np
import pytest

from app.services import semantic_router as sr


@pytest.fixture(autouse=True)
def _fixed_centroids(monkeypatch):
    centroids = {
        "table": np.array([1.0, 0.0, 0.0], dtype=np.float32),
        "clause": np.array([0.0, 1.0, 0.0], dtype=np.float32),
        "vector": np.array([0.0, 0.0, 1.0], dtype=np.float32),
    }
    monkeypatch.setattr(sr, "_centroids", centroids)
    yield
    sr.reset_centroids_cache()


def test_classify_confident_single_store():
    query_embedding = np.array([0.9, 0.1, 0.05], dtype=np.float32)
    out = sr.classify(query_embedding)
    assert out["stores"] == ["table"]
    assert out["confidence"] > 0.6
    assert out["used_fallback"] is False


def test_classify_ambiguous_low_similarity_searches_all_stores():
    query_embedding = np.array([0.1, 0.1, 0.1], dtype=np.float32)
    out = sr.classify(query_embedding)
    assert set(out["stores"]) == set(sr.ALL_STORES)
    assert out["confidence"] == 0.35


def test_classify_close_cluster_returns_multiple_stores():
    # table and clause nearly tied and both well above MIN_SIMILARITY —
    # too close to call a single winner, so both come back together.
    query_embedding = np.array([0.5, 0.48, 0.1], dtype=np.float32)
    out = sr.classify(query_embedding)
    assert "table" in out["stores"]
    assert "clause" in out["stores"]
    assert "vector" not in out["stores"]


def test_get_centroids_lazily_builds_once_and_caches(monkeypatch):
    sr.reset_centroids_cache()
    calls = {"n": 0}

    def _fake_embed_passages(phrases):
        calls["n"] += 1
        return np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), (len(phrases), 1))

    monkeypatch.setattr("app.services.embedding_service.embed_passages", _fake_embed_passages)

    first = sr._get_centroids()
    second = sr._get_centroids()

    assert calls["n"] == len(sr._STORE_PROTOTYPES)  # one embed_passages call per store
    assert first is second  # cached on the second call, not rebuilt
