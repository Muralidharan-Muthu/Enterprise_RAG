from unittest.mock import patch
from app.services.retriever_service import RetrievedChunk
import app.api.routes.query as q


def _c(cid, store, dist):
    return RetrievedChunk(chunk_id=cid, document_id="d", text=cid,
                          store_type=store, distance=dist)


def _retrieved():
    return [
        _c("v1", "vector", 0.10), _c("v2", "vector", 0.20), _c("v3", "vector", 0.30),
        _c("t1", "table", 0.50),
    ]


def test_rank_chunks_no_reranker_uses_rrf_keeps_minority_store():
    final, pool_size = q._rank_chunks("revenue", _retrieved(), top_k=2, use_reranker=False)
    ids = {c.chunk_id for c in final}
    # RRF gives the lone table the same rank-1 score as the closest vector — not starved
    assert "t1" in ids
    assert pool_size == 4


def test_rank_chunks_reranker_receives_balanced_pool():
    captured = {}

    def fake_rerank(query, pool, top_k):
        captured["pool_ids"] = [c.chunk_id for c in pool]
        return pool[:top_k]

    with patch.object(q.reranker_service, "rerank", side_effect=fake_rerank):
        final, pool_size = q._rank_chunks("q", _retrieved(), top_k=2, use_reranker=True)
    # the table chunk made it into the pool handed to the reranker
    assert "t1" in captured["pool_ids"]
    assert pool_size == 4
    assert len(final) == 2


def test_rank_chunks_reranker_error_falls_back_to_rrf():
    with patch.object(q.reranker_service, "rerank", side_effect=RuntimeError("boom")):
        final, pool_size = q._rank_chunks("q", _retrieved(), top_k=3, use_reranker=True)
    # fell back to RRF (relevance_score set by RRF, not zero), table present
    assert {c.chunk_id for c in final} >= {"v1", "t1"}
    assert all(c.relevance_score > 0 for c in final)
