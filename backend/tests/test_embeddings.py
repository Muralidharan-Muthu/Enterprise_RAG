"""Tests for embedding_service.py — require BGE model download on first run."""
import pytest
import numpy as np


@pytest.mark.slow
def test_embed_passages_shape():
    from app.services.embedding_service import embed_passages
    texts = ["This is a test sentence.", "Another document chunk for embedding."]
    embeddings = embed_passages(texts)
    assert embeddings.shape == (2, 1024), f"Expected (2, 1024), got {embeddings.shape}"


@pytest.mark.slow
def test_embed_passages_normalized():
    from app.services.embedding_service import embed_passages
    texts = ["Normalized embedding test sentence for BGE model verification."]
    embeddings = embed_passages(texts)
    norm = np.linalg.norm(embeddings[0])
    assert abs(norm - 1.0) < 1e-5, f"Expected unit norm, got {norm}"


@pytest.mark.slow
def test_embed_passages_float32():
    from app.services.embedding_service import embed_passages
    embeddings = embed_passages(["dtype check"])
    assert embeddings.dtype == np.float32


@pytest.mark.slow
def test_similar_sentences_higher_similarity():
    from app.services.embedding_service import embed_passages
    texts = [
        "The company reported strong quarterly revenue growth.",
        "Quarterly earnings showed significant revenue increase.",
        "The cat sat on the mat in the garden.",
    ]
    embs = embed_passages(texts)
    sim_related = float(np.dot(embs[0], embs[1]))
    sim_unrelated = float(np.dot(embs[0], embs[2]))
    assert sim_related > sim_unrelated, (
        f"Related sentences ({sim_related:.3f}) should be more similar than "
        f"unrelated ({sim_unrelated:.3f})"
    )


@pytest.mark.slow
def test_embed_query_shape():
    from app.services.embedding_service import embed_query
    emb = embed_query("What is the revenue for Q3 2024?")
    assert emb.shape == (1024,)
    assert emb.dtype == np.float32


def test_embed_empty_list():
    from app.services.embedding_service import embed_passages
    result = embed_passages([])
    assert result.shape == (0, 1024)
