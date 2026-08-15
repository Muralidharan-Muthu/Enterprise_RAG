"""
Gated live verification for the Neo4j graph (P4T6).

DANGER: this test writes real nodes/relationships to whatever Neo4j instance
NEO4J_URI points at and then deletes them. NEO4J_ENABLED=true is not enough to
gate this — a developer's .env may legitimately point NEO4J_ENABLED=true at a
real/shared/production Aura instance (this happened once: the test ran during
an unrelated `pytest -k graph` sweep and left junk nodes behind). Running this
test requires BOTH NEO4J_ENABLED=true AND an explicit opt-in:

    docker-compose up -d neo4j
    NEO4J_ENABLED=true NEO4J_LIVE_TEST_OK=true pytest tests/test_graph_live.py -v -m slow

It proves the acceptance criterion: two documents sharing an entity become
connected, and `related_documents` surfaces the neighbour. Cleans up its own
fixture data (docA/docB + their entities) in a finally block regardless of
pass/fail, so it never leaves residue in whatever Neo4j it ran against.
"""
import os

import pytest

from app.config import settings
from app.services import graph_service

pytestmark = pytest.mark.slow

_LIVE_TEST_OPT_IN = os.environ.get("NEO4J_LIVE_TEST_OK", "").lower() in ("1", "true", "yes")


def _neo4j_up() -> bool:
    return _LIVE_TEST_OPT_IN and settings.NEO4J_ENABLED and graph_service.is_available()


@pytest.mark.skipif(
    not _neo4j_up(),
    reason="needs NEO4J_ENABLED=true, NEO4J_LIVE_TEST_OK=true, and a reachable Neo4j",
)
def test_shared_entity_connects_two_documents():
    docA, docB = "live-test-docA", "live-test-docB"
    shared = [{"name": "Acme Holdings", "type": "org"}]

    try:
        graph_service.upsert_document(docA, "A.pdf", "legal")
        graph_service.upsert_document(docB, "B.pdf", "policy")
        graph_service.upsert_entities(docA, shared, filename="A.pdf")
        graph_service.upsert_entities(docB, shared + [{"name": "Globex", "type": "org"}], filename="B.pdf")

        # From docA's perspective, querying the shared entity finds docB (its neighbour).
        related = graph_service.related_documents(["Acme Holdings"], exclude_doc_id=docA, limit=5)
        assert docB in related
        assert docA not in related
    finally:
        # Always clean up — never leave test fixtures in a real Neo4j instance.
        graph_service.clear_document_graph(docA)
        graph_service.clear_document_graph(docB)
        drv = graph_service._get_driver()
        if drv is not None:
            with graph_service._session(drv) as session:
                session.run("MATCH (d:Document) WHERE d.id IN $ids DETACH DELETE d",
                             ids=[docA, docB])
