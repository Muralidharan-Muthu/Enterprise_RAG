from unittest.mock import patch

import app.services.retriever_service as rs
from app.services.retriever_service import RetrievedChunk


def _chunk(chunk_id, doc_id, dist=0.1):
    return RetrievedChunk(chunk_id=chunk_id, document_id=doc_id, text="t",
                          store_type="vector", distance=dist)


def test_falls_back_to_entity_search_when_graph_unavailable():
    # When Neo4j is down, graph_expanded_chunks no longer just gives up — it
    # searches the discovered entity names directly via retrieve() so
    # cross-document connection still works without graph infra.
    with patch("app.services.graph_service.is_available", return_value=False), \
         patch("app.services.entity_service.extract_entities",
               return_value=[{"name": "Acme", "type": "org"}]), \
         patch.object(rs, "retrieve", return_value=[_chunk("c9", "docB")]) as ret:
        out = rs.graph_expanded_chunks("q about Acme", [_chunk("c1", "docA")])
    assert [c.chunk_id for c in out] == ["c9"]
    ret.assert_called_once()
    assert ret.call_args.kwargs["use_intent"] is False
    assert ret.call_args.args[0] == "Acme"


def test_no_op_when_no_query_entities():
    with patch("app.services.graph_service.is_available", return_value=True), \
         patch("app.services.entity_service.extract_entities", return_value=[]):
        out = rs.graph_expanded_chunks("vague query", [_chunk("c1", "docA")])
    assert out == []


def test_expands_to_related_docs_and_dedupes():
    primary = [_chunk("c1", "docA")]
    with patch("app.services.graph_service.is_available", return_value=True), \
         patch("app.services.entity_service.extract_entities",
               return_value=[{"name": "Acme", "type": "org"}]), \
         patch("app.services.graph_service.related_documents",
               return_value=["docA", "docB"]) as rel, \
         patch.object(rs, "retrieve",
                      return_value=[_chunk("c1", "docB"), _chunk("c2", "docB")]) as ret:
        out = rs.graph_expanded_chunks("who is Acme", primary)

    # docA filtered out (already present) → only docB fetched
    ret.assert_called_once()
    assert ret.call_args.kwargs["document_id"] == "docB"
    assert ret.call_args.kwargs["use_intent"] is False
    # c1 is already in primary → deduped; only c2 returned
    assert [c.chunk_id for c in out] == ["c2"]
    # graph queried with the extracted entity name
    assert rel.call_args[0][0] == ["Acme"]


def test_no_related_docs_returns_empty():
    with patch("app.services.graph_service.is_available", return_value=True), \
         patch("app.services.entity_service.extract_entities",
               return_value=[{"name": "Acme", "type": "org"}]), \
         patch("app.services.graph_service.related_documents", return_value=[]):
        out = rs.graph_expanded_chunks("q", [_chunk("c1", "docA")])
    assert out == []


def test_errors_are_swallowed():
    with patch("app.services.graph_service.is_available", return_value=True), \
         patch("app.services.entity_service.extract_entities",
               side_effect=RuntimeError("boom")):
        out = rs.graph_expanded_chunks("q", [_chunk("c1", "docA")])
    assert out == []
