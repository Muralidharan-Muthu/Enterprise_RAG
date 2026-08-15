from unittest.mock import MagicMock, patch
import app.services.reranker_service as rr
from app.services.retriever_service import RetrievedChunk


def _c(cid, dist):
    return RetrievedChunk(chunk_id=cid, document_id="d", text=cid,
                          store_type="vector", distance=dist)


def test_cap_raised_to_40():
    assert rr.MAX_RERANK_CANDIDATES == 40


def test_rerank_scores_beyond_old_cap_of_12():
    # 20 candidates; mocked model scores by position so the LAST is most relevant.
    cands = [_c(f"c{i}", 0.01 * i) for i in range(20)]
    fake = MagicMock()
    fake.predict.return_value = [float(i) for i in range(20)]  # c19 highest
    with patch.object(rr, "_get_reranker", return_value=fake):
        out = rr.rerank("q", cands, top_k=5)
    ids = [c.chunk_id for c in out]
    # c19 (index 19, beyond the old cap of 12) wins — proves the whole pool is reranked
    assert "c19" in ids
    assert len(out) == 5
    assert ids[0] == "c19"
