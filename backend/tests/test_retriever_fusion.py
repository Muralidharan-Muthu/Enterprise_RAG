from app.services.retriever_service import RetrievedChunk, balanced_pool, rrf_merge


def _c(cid, store, dist):
    return RetrievedChunk(chunk_id=cid, document_id="d", text=cid,
                          store_type=store, distance=dist)


def test_balanced_pool_caps_per_store_and_keeps_minority_store():
    results = [
        _c("v1", "vector", 0.10), _c("v2", "vector", 0.20), _c("v3", "vector", 0.30),
        _c("t1", "table", 0.50),
    ]
    pool = balanced_pool(results, per_store_cap=2)
    ids = {c.chunk_id for c in pool}
    # vector capped to its 2 closest; the lone (far) table is NOT starved
    assert ids == {"v1", "v2", "t1"}


def test_balanced_pool_dedups_by_chunk_id():
    results = [_c("v1", "vector", 0.1), _c("v1", "vector", 0.1)]
    pool = balanced_pool(results, per_store_cap=8)
    assert len(pool) == 1


def test_rrf_merge_makes_top_of_each_store_comparable():
    results = [
        _c("v1", "vector", 0.10), _c("v2", "vector", 0.20),
        _c("t1", "table", 0.50),
    ]
    merged = rrf_merge(results, k=60)
    # rank-1 of each store (v1, t1) share the top RRF score; v2 (rank 2) ranks below
    top_two = {merged[0].chunk_id, merged[1].chunk_id}
    assert top_two == {"v1", "t1"}
    assert merged[2].chunk_id == "v2"
    assert merged[0].relevance_score == merged[1].relevance_score
    assert merged[2].relevance_score < merged[0].relevance_score
