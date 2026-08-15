"""image_store is a PURE extraction repository (migration 008): it has no
embedding column and is NOT a searchable semantic store. These tests lock that
in so image search can't silently creep back.

Image-derived content that should be searchable is cross-stored into
vector/table/clause/document by store_image_derived_chunks and retrieved there.
"""
import app.services.retriever_service as rs
import app.services.intent_service as intsvc


def test_image_store_not_in_retriever_store_keys():
    assert "image" not in rs.ALL_STORE_KEYS


def test_retriever_has_no_image_query_function():
    # The former _query_image_store / _rows_to_image_chunks were removed.
    assert not hasattr(rs, "_query_image_store")
    assert not hasattr(rs, "_rows_to_image_chunks")


def test_intent_does_not_route_to_image():
    assert "image" not in intsvc.ALL_STORES


def test_visual_query_routes_to_searchable_stores_not_image():
    # "show me the chart" must hit real searchable stores, never a dead image set.
    intent = intsvc._rule_based_intent("show me the revenue chart")
    assert intent["stores"]                      # non-empty
    assert "image" not in intent["stores"]
    assert set(intent["stores"]) <= set(intsvc.ALL_STORES)
