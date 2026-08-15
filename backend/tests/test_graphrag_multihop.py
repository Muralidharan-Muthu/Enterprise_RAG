"""Multi-hop, cross-document GraphRAG traversal.

Covers the whole depth chain:
  graphrag_local_chunks (retriever_service)
    → local_search (graphrag_retriever)     ← honors GRAPHRAG_LOCAL_HOPS
      → local_neighborhood (graph_service)  ← walks N entity→entity hops

Regression: local_search used to default hops=1 with a dead `hops or config`
fallback (1 or 2 == 1), so every live query silently ran a single hop and the
configured multi-hop depth was never reached.
"""
from unittest.mock import patch

import app.services.graph_service as gs
import app.services.graphrag_retriever as gr
import app.services.retriever_service as rs


def _rec(pg_id, doc, store="vector_store"):
    return {"pg_id": pg_id, "document_id": doc, "store": store}


class _FakeSession:
    """Returns a distinct row set per session.run() call, in call order —
    mirroring local_neighborhood's one-query-per-hop structure."""
    def __init__(self, per_call_rows):
        self._rows = list(per_call_rows)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, *args, **kwargs):
        rows = self._rows[self.calls] if self.calls < len(self._rows) else []
        self.calls += 1
        return list(rows)


# ── local_neighborhood: the traversal engine ─────────────────────────────────

def test_local_neighborhood_multihop_crosses_documents():
    # hop 0 → docA (direct mention), hop 1 → docB, hop 2 → docC.
    # Distinct documents at each hop proves the traversal crosses document
    # boundaries through shared entities, not just within one file.
    fake = _FakeSession([
        [_rec("c0", "docA")],
        [_rec("c1", "docB")],
        [_rec("c2", "docC")],
    ])
    with patch.object(gs, "_get_driver", return_value=object()), \
         patch.object(gs, "_session", return_value=fake):
        out = gs.local_neighborhood(["acme"], hops=2, limit=40)

    assert {r["document_id"] for r in out} == {"docA", "docB", "docC"}
    by = {r["pg_id"]: r for r in out}
    assert by["c0"]["hop_distance"] == 0
    assert by["c1"]["hop_distance"] == 1
    assert by["c2"]["hop_distance"] == 2
    # Closer hops score higher (1.0 > 0.5 > 0.333…).
    assert by["c0"]["score"] > by["c1"]["score"] > by["c2"]["score"]
    assert fake.calls == 3  # hop 0 + 2 traversal hops


def test_local_neighborhood_respects_hop_limit():
    # With hops=1, only hop 0 + hop 1 queries run — the hop-2 rows must never
    # be fetched.
    fake = _FakeSession([
        [_rec("c0", "docA")],
        [_rec("c1", "docB")],
        [_rec("c2", "docC")],  # hop 2 — should not be queried
    ])
    with patch.object(gs, "_get_driver", return_value=object()), \
         patch.object(gs, "_session", return_value=fake):
        out = gs.local_neighborhood(["acme"], hops=1, limit=40)

    assert fake.calls == 2  # hop 0 + 1 traversal hop only
    assert {r["document_id"] for r in out} == {"docA", "docB"}
    assert "c2" not in {r["pg_id"] for r in out}


def test_local_neighborhood_dedups_keeping_closest_hop():
    # A chunk reachable at both hop 0 and hop 1 is kept once, at the closer hop.
    fake = _FakeSession([
        [_rec("c0", "docA")],
        [_rec("c0", "docA"), _rec("c1", "docB")],  # c0 repeats
    ])
    with patch.object(gs, "_get_driver", return_value=object()), \
         patch.object(gs, "_session", return_value=fake):
        out = gs.local_neighborhood(["acme"], hops=1, limit=40)

    by = {r["pg_id"]: r for r in out}
    assert len(out) == 2
    assert by["c0"]["hop_distance"] == 0


def test_local_neighborhood_caps_hops_at_three():
    # min(hops, 3): even hops=9 runs at most hop 0 + hops 1..3 = 4 queries.
    fake = _FakeSession([[_rec(f"c{i}", f"doc{i}")] for i in range(6)])
    with patch.object(gs, "_get_driver", return_value=object()), \
         patch.object(gs, "_session", return_value=fake):
        gs.local_neighborhood(["acme"], hops=9, limit=40)
    assert fake.calls == 4


# ── local_search: honors configured hop depth ────────────────────────────────

def _patch_local_search_env(hops_cfg=2):
    return patch.multiple(
        gr.settings,
        GRAPHRAG_ENABLED=True,
        GRAPHRAG_LOCAL_HOPS=hops_cfg,
        GRAPHRAG_LOCAL_TOP_ENTITIES=10,
    )


def test_local_search_uses_configured_hops_by_default():
    with _patch_local_search_env(hops_cfg=2), \
         patch("app.services.graph_service.is_available", return_value=True), \
         patch("app.services.entity_service.extract_entities",
               return_value=[{"name": "Acme", "type": "org"}]), \
         patch("app.services.entity_service.canonicalize",
               side_effect=lambda s: (s or "").lower().strip()), \
         patch("app.services.graph_service.local_neighborhood", return_value=[]) as ln:
        gr.local_search("who is acme connected to")

    # The whole point of the fix: config depth reaches the traversal, not 1.
    assert ln.call_args.kwargs["hops"] == 2


def test_local_search_honors_explicit_hops_override():
    with _patch_local_search_env(hops_cfg=2), \
         patch("app.services.graph_service.is_available", return_value=True), \
         patch("app.services.entity_service.extract_entities",
               return_value=[{"name": "Acme", "type": "org"}]), \
         patch("app.services.entity_service.canonicalize",
               side_effect=lambda s: (s or "").lower().strip()), \
         patch("app.services.graph_service.local_neighborhood", return_value=[]) as ln:
        gr.local_search("who is acme connected to", hops=3)

    assert ln.call_args.kwargs["hops"] == 3


# ── graphrag_local_chunks: threads hops down to local_search ──────────────────

def test_graphrag_local_chunks_defaults_hops_to_none():
    # None → local_search resolves GRAPHRAG_LOCAL_HOPS (multi-hop by config).
    with patch("app.services.graphrag_retriever.local_search", return_value=[]) as ls:
        rs.graphrag_local_chunks("q", ["research"])
    assert ls.call_args.kwargs.get("hops") is None


def test_graphrag_local_chunks_threads_explicit_hops():
    with patch("app.services.graphrag_retriever.local_search", return_value=[]) as ls:
        rs.graphrag_local_chunks("q", ["research"], hops=3)
    assert ls.call_args.kwargs.get("hops") == 3
