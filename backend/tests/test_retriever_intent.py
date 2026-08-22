from app.services.retriever_service import _select_stores, ALL_STORE_KEYS


def test_select_stores_explicit_doc_types_override():
    flags = _select_stores(["legal"], use_intent=True, intent={"stores": ["table"]})
    # explicit document_types win over intent
    assert flags["clause"] is True
    assert flags["vector"] is False
    assert flags["table"] is False


def test_select_stores_from_intent():
    # A confident intent narrows to its store set. image_store is not a searchable
    # store (migration 008) — visual content routes to table/vector instead.
    flags = _select_stores(None, use_intent=True, intent={"stores": ["table"], "confidence": 0.9})
    assert flags["table"] is True
    assert flags["vector"] is False and flags["clause"] is False
    assert "image" not in flags


def test_select_stores_all_when_intent_disabled():
    flags = _select_stores(None, use_intent=False, intent=None)
    assert all(flags[s] for s in ALL_STORE_KEYS)


def test_select_stores_all_when_no_intent_available():
    flags = _select_stores(None, use_intent=True, intent=None)
    assert all(flags[s] for s in ALL_STORE_KEYS)
