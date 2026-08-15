from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.services.graph_service as gs


@pytest.fixture(autouse=True)
def _reset_driver_singleton():
    """Each test starts with a clean driver cache."""
    gs._driver = None
    gs._driver_failed = False
    gs._last_check_at = 0.0
    gs._last_check_result = False
    yield
    gs._driver = None
    gs._driver_failed = False
    gs._last_check_at = 0.0
    gs._last_check_result = False


def _mock_driver(run_return=None):
    """A driver whose .session() is a context manager exposing .run()."""
    session = MagicMock()
    session.run.return_value = run_return if run_return is not None else []
    driver = MagicMock()

    @contextmanager
    def _session(*args, **kwargs):
        # Accept database=... (passed by graph_service._session when
        # NEO4J_DATABASE is set) and any other session kwargs.
        yield session

    driver.session.side_effect = _session
    return driver, session


# ── gating / availability ────────────────────────────────────────────

def test_get_driver_none_when_disabled():
    with patch.object(gs.settings, "NEO4J_ENABLED", False):
        assert gs._get_driver() is None


def test_is_available_false_when_disabled():
    with patch.object(gs.settings, "NEO4J_ENABLED", False):
        assert gs.is_available() is False


def test_is_available_true_when_connectivity_ok():
    driver, _ = _mock_driver()
    with patch.object(gs, "_get_driver", return_value=driver):
        assert gs.is_available() is True
    driver.verify_connectivity.assert_called_once()


def test_is_available_false_when_connectivity_raises():
    driver, _ = _mock_driver()
    driver.verify_connectivity.side_effect = RuntimeError("down")
    with patch.object(gs, "_get_driver", return_value=driver):
        assert gs.is_available() is False


# ── writes: Cypher + params ──────────────────────────────────────────

def test_upsert_document_runs_merge_with_params():
    driver, session = _mock_driver()
    with patch.object(gs, "_get_driver", return_value=driver):
        gs.upsert_document("doc1", "Acme.pdf", "legal")
    cypher, kwargs = session.run.call_args[0][0], session.run.call_args.kwargs
    assert "MERGE (d:Document {id: $id})" in cypher
    assert kwargs == {"id": "doc1", "filename": "Acme.pdf", "doc_type": "legal"}


def test_upsert_entities_canonicalizes_keys_and_merges_edges():
    driver, session = _mock_driver()
    with patch.object(gs, "_get_driver", return_value=driver):
        gs.upsert_entities("doc1", [{"name": "Acme Corp", "type": "org"},
                                    {"name": "  ", "type": "x"}])
    cypher = session.run.call_args[0][0]
    kwargs = session.run.call_args.kwargs
    assert "MERGE (e:Entity {key: row.key})" in cypher
    assert "MENTIONED_IN" in cypher
    assert kwargs["doc_id"] == "doc1"
    # blank name dropped; key canonicalized
    assert kwargs["rows"] == [{"key": "acme corp", "name": "Acme Corp", "type": "org"}]


def test_upsert_entities_noop_when_all_blank():
    driver, session = _mock_driver()
    with patch.object(gs, "_get_driver", return_value=driver):
        gs.upsert_entities("doc1", [{"name": "   ", "type": "x"}])
    session.run.assert_not_called()


# ── reads ────────────────────────────────────────────────────────────

def test_related_documents_returns_ids_and_passes_canonical_keys():
    driver, session = _mock_driver(run_return=[{"id": "docA"}, {"id": "docB"}])
    with patch.object(gs, "_get_driver", return_value=driver):
        out = gs.related_documents(["Acme Corp", "ACME CORP"], exclude_doc_id="doc1", limit=3)
    assert out == ["docA", "docB"]
    kwargs = session.run.call_args.kwargs
    assert kwargs["keys"] == ["acme corp"]   # deduped + canonical
    assert kwargs["exclude"] == "doc1"
    assert kwargs["limit"] == 3


def test_related_documents_empty_input_returns_empty():
    driver, session = _mock_driver()
    with patch.object(gs, "_get_driver", return_value=driver):
        assert gs.related_documents([]) == []
    session.run.assert_not_called()


# ── degradation: a raising driver never propagates ───────────────────

def test_raising_driver_yields_noop_and_empty():
    driver = MagicMock()
    driver.session.side_effect = RuntimeError("neo4j down")
    with patch.object(gs, "_get_driver", return_value=driver):
        # writes swallow
        gs.upsert_document("doc1", "f.pdf", "legal")
        gs.upsert_entities("doc1", [{"name": "Acme", "type": "org"}])
        # read returns [] not raise
        assert gs.related_documents(["Acme"]) == []


def test_all_calls_noop_when_driver_none():
    with patch.object(gs, "_get_driver", return_value=None):
        gs.upsert_document("doc1", "f.pdf", "legal")
        gs.upsert_entities("doc1", [{"name": "Acme", "type": "org"}])
        assert gs.related_documents(["Acme"]) == []
        assert gs.is_available() is False
