from unittest.mock import patch
import numpy as np

import app.services.intent_service as intent


def test_rule_based_legal_query():
    out = intent._rule_based_intent("what is the termination clause and indemnification liability")
    assert "clause" in out["stores"]
    assert out["doc_types"] == ["legal"]
    assert out["confidence"] >= 0.6


def test_rule_based_visual_query_routes_to_searchable_stores():
    # image_store is not searchable (migration 008). Visual queries route to the
    # stores where image-derived content actually lives: table + vector.
    out = intent._rule_based_intent("show me the revenue chart and the figures")
    assert "image" not in out["stores"]
    assert "table" in out["stores"]
    assert "vector" in out["stores"]


def test_rule_based_research_query():
    out = intent._rule_based_intent("summarize the study methodology and hypothesis")
    assert "research" in out["stores"]
    assert out["doc_types"] == ["research"]


def test_rule_based_ambiguous_searches_all_stores():
    out = intent._rule_based_intent("tell me something")
    assert set(out["stores"]) == {"vector", "clause", "research", "table"}
    assert out["doc_types"] is None
    assert out["confidence"] < 0.6


def test_classify_intent_falls_back_when_no_endpoint():
    with patch("app.config.settings") as s:
        s.GEMMA4_BASE_URL = ""
        out = intent.classify_intent("what is the termination clause")
    assert out["used_fallback"] is True
    assert "clause" in out["stores"]


def test_classify_intent_rule_short_circuits_before_semantic_router(monkeypatch):
    """An unambiguous single-category keyword hit (confidence >= RULE_HIGH_CONFIDENCE)
    should win outright without ever consulting the semantic router."""
    called = {"n": 0}

    def _fake_semantic_classify(_embedding):
        called["n"] += 1
        return {"stores": ["vector"], "doc_types": None, "confidence": 0.99, "used_fallback": False}

    monkeypatch.setattr("app.services.semantic_router.classify", _fake_semantic_classify)
    out = intent.classify_intent(
        "what is the termination clause and indemnification liability",
        query_embedding=np.zeros(4, dtype=np.float32),
    )
    assert called["n"] == 0
    assert "clause" in out["stores"]


def test_classify_intent_uses_semantic_router_when_more_confident(monkeypatch):
    """A query the keyword rules can't resolve (ambiguous, low confidence) should
    defer to a more-confident semantic router result instead of searching all stores."""
    monkeypatch.setattr(intent.settings, "INTENT_USE_SEMANTIC_ROUTER", True)
    monkeypatch.setattr(intent.settings, "INTENT_USE_LLM", False)

    def _fake_semantic_classify(_embedding):
        return {"stores": ["table"], "doc_types": None, "confidence": 0.9, "used_fallback": False}

    monkeypatch.setattr("app.services.semantic_router.classify", _fake_semantic_classify)
    out = intent.classify_intent("tell me something", query_embedding=np.zeros(4, dtype=np.float32))
    assert out["stores"] == ["table"]
    assert out["confidence"] == 0.9


def test_classify_intent_skips_semantic_router_without_embedding(monkeypatch):
    """No query_embedding available (caller outside the retrieval path) — the
    semantic tier is skipped rather than forcing a second embed call."""
    called = {"n": 0}

    def _fake_semantic_classify(_embedding):
        called["n"] += 1
        return {"stores": ["vector"], "doc_types": None, "confidence": 0.99, "used_fallback": False}

    monkeypatch.setattr("app.services.semantic_router.classify", _fake_semantic_classify)
    monkeypatch.setattr(intent.settings, "INTENT_USE_LLM", False)
    out = intent.classify_intent("tell me something")
    assert called["n"] == 0
    assert out["used_fallback"] is True


def test_classify_intent_semantic_router_disabled_by_flag(monkeypatch):
    called = {"n": 0}

    def _fake_semantic_classify(_embedding):
        called["n"] += 1
        return {"stores": ["vector"], "doc_types": None, "confidence": 0.99, "used_fallback": False}

    monkeypatch.setattr("app.services.semantic_router.classify", _fake_semantic_classify)
    monkeypatch.setattr(intent.settings, "INTENT_USE_SEMANTIC_ROUTER", False)
    monkeypatch.setattr(intent.settings, "INTENT_USE_LLM", False)
    out = intent.classify_intent("tell me something", query_embedding=np.zeros(4, dtype=np.float32))
    assert called["n"] == 0
    assert out["used_fallback"] is True
