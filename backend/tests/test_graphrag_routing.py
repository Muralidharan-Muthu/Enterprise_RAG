"""route_graphrag() — Groq-gated entity-intent routing.

The graph (local mode) must only fire for genuine entity/relationship questions,
NOT for factual value lookups that merely mention an entity (e.g. a fiscal
period or a metric name). The decision is delegated to Groq via
_is_entity_query(); these tests mock that classifier and the graph so no network
or Neo4j is needed.
"""
from unittest.mock import patch

import app.services.graphrag_retriever as gr


def _patch_common(enabled=True, available=True):
    """Patch the two hard gates so we can focus on the routing logic."""
    return [
        patch.object(gr.settings, "GRAPHRAG_ENABLED", enabled),
        patch("app.services.graph_service.is_available", return_value=available),
    ]


def _run(query, is_entity, entities=None, graph_hits=1):
    """Drive route_graphrag with the classifier + graph mocked.

    is_entity   → what the Groq classifier returns
    entities    → what extract_entities returns (defaults to one entity)
    graph_hits  → count returned by the Neo4j entity-match query
    """
    entities = [{"name": "Kelvin He", "type": "person"}] if entities is None else entities

    class _Session:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, *a, **k):
            class _R:
                def single(self_inner): return {"cnt": graph_hits}
            return _R()

    with patch.object(gr.settings, "GRAPHRAG_ENABLED", True), \
         patch("app.services.graph_service.is_available", return_value=True), \
         patch.object(gr, "_is_entity_query", return_value=is_entity), \
         patch("app.services.entity_service.extract_entities", return_value=entities), \
         patch("app.services.entity_service.canonicalize", side_effect=lambda s: (s or "").lower().strip()), \
         patch("app.services.graph_service._get_driver", return_value=object()), \
         patch("app.services.graph_service._session", return_value=_Session()):
        return gr.route_graphrag(query)


def test_factual_value_lookup_does_not_route_local():
    # "What is the revenue of FY 2023-24 - Planned?" — Groq says NOT an entity
    # question, so even though NER would extract "FY 2023-24"/"revenue" and they
    # exist in the graph, we must not route local.
    mode = _run("What is the revenue of FY 2023-24 - Planned?", is_entity=False)
    assert mode == "none"


def test_entity_relationship_question_routes_local():
    # "Who is Kelvin He at Lenovo?" — Groq says entity question AND the entity
    # exists in the graph → local.
    mode = _run("Who is Kelvin He and what is his role at Lenovo?", is_entity=True, graph_hits=1)
    assert mode == "local"


def test_entity_question_without_graph_hit_is_not_local():
    # Classifier says entity, but no matching node in the graph → not local.
    mode = _run("Who is Nonexistent Person?", is_entity=True, graph_hits=0)
    assert mode == "none"


def test_non_entity_but_aggregation_cue_routes_global():
    # Not an entity question, but has a global cue word ("compare") → global.
    mode = _run("Compare the overall themes across all documents", is_entity=False)
    assert mode == "global"


def test_disabled_flag_short_circuits_to_none():
    with patch.object(gr.settings, "GRAPHRAG_ENABLED", False):
        assert gr.route_graphrag("Who is Kelvin He?") == "none"


def test_graph_unavailable_short_circuits_to_none():
    with patch.object(gr.settings, "GRAPHRAG_ENABLED", True), \
         patch("app.services.graph_service.is_available", return_value=False):
        assert gr.route_graphrag("Who is Kelvin He?") == "none"


def test_classifier_returns_false_when_Groq_unconfigured():
    with patch.object(gr.settings, "GROQ_BASE_URL", ""):
        assert gr._is_entity_query("Who is Kelvin He?") is False


def test_classifier_parses_graph_verdict():
    with patch.object(gr.settings, "GROQ_BASE_URL", "http://x"), \
         patch("app.services.groq_client.chat", return_value='{"mode": "graph"}'):
        assert gr._is_entity_query("Who is Kelvin He?") is True


def test_classifier_parses_search_verdict():
    with patch.object(gr.settings, "GROQ_BASE_URL", "http://x"), \
         patch("app.services.groq_client.chat", return_value='{"mode": "search"}'):
        assert gr._is_entity_query("What is the revenue of FY 2023-24?") is False


def test_classifier_defaults_false_on_Groq_error():
    with patch.object(gr.settings, "GROQ_BASE_URL", "http://x"), \
         patch("app.services.groq_client.chat", side_effect=RuntimeError("boom")):
        assert gr._is_entity_query("Who is Kelvin He?") is False
