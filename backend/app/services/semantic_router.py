"""
Semantic query router — classifies which stores a query likely belongs to by
cosine similarity against a small set of precomputed per-store prototype
embeddings, instead of a keyword list or an LLM round-trip.

Why this exists (tier 2 of intent_service.classify_intent's escalation ladder):
- _rule_based_intent() is instant but brittle — it only catches queries that
  contain one of its literal keywords, so paraphrases ("what did the firm earn
  last quarter" with no "revenue"/"fiscal"/"earnings" keyword) fall through to
  the recall-safe all-stores case even though they are clearly about numbers.
- Groq (intent_service's LLM path) is accurate but adds a full network round
  trip on the query's critical path, so it's gated off by default.
- This module sits between the two: it reuses the query embedding retrieve()
  already computes for the vector search itself (embedding_service.embed_query),
  so classification costs a handful of dot products — no extra model call, no
  network round trip.

The prototype set intentionally mirrors ALL_STORES from intent_service.py and
the keyword categories in intent_service's rule-based classifier, so the two
signals can be combined/compared meaningfully by the caller.
"""
import logging
import threading

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

ALL_STORES = ["vector", "clause", "research", "table"]

# A handful of short, representative example queries per store. Multiple
# phrasings per store average out into a single centroid so the match isn't
# overly sensitive to any one phrase's wording.
_STORE_PROTOTYPES: dict[str, list[str]] = {
    "table": [
        "show me the revenue table",
        "what is the value in this row and column",
        "give me the numbers from the spreadsheet",
        "how much did earnings change quarter over quarter",
        "what was the profit margin last fiscal year",
        "show the chart or figure with the data",
    ],
    "clause": [
        "what does this contract clause say",
        "who is liable under this agreement",
        "what are the termination conditions",
        "explain the confidentiality obligations",
        "what happens if either party breaches the contract",
        "what is the governing law of this agreement",
    ],
    "research": [
        "summarize the study methodology",
        "what was the hypothesis of this experiment",
        "what did the paper find",
        "what dataset was used in this research",
        "what is the citation for this finding",
    ],
    "vector": [
        "what is the company policy on this topic",
        "explain the standard operating procedure",
        "what are the guidelines for this process",
        "summarize this document",
        "what does this policy require employees to do",
    ],
}

# Cosine similarity is a dot product here because embed_query()/embed_passages()
# both L2-normalize their output (see embedding_service.py).
#
# Below this, the query doesn't resemble any store's prototypes closely enough
# to trust a narrowed set — ambiguous, fall back to all stores (recall-safe).
MIN_SIMILARITY = 0.30
# Gap between the best- and second-best-matching store's similarity needed to
# call it an unambiguous single-store match. Below this gap, several stores
# are plausibly relevant, so the caller gets all of them within the margin.
MARGIN = 0.05

_centroids: dict[str, np.ndarray] | None = None
_centroids_lock = threading.Lock()


def _build_centroids() -> dict[str, np.ndarray]:
    """Embed every prototype phrase and average per store into one centroid
    vector each. Computed once per process — prototypes are a fixed constant,
    so there is nothing to invalidate."""
    from app.services import embedding_service

    centroids: dict[str, np.ndarray] = {}
    for store, phrases in _STORE_PROTOTYPES.items():
        vectors = embedding_service.embed_passages(phrases)
        centroid = vectors.mean(axis=0)
        centroid = centroid / (np.linalg.norm(centroid) or 1.0)
        centroids[store] = centroid
    return centroids


def _get_centroids() -> dict[str, np.ndarray]:
    global _centroids
    if _centroids is None:
        with _centroids_lock:
            if _centroids is None:
                logger.info("Building semantic router store centroids (one-time)")
                _centroids = _build_centroids()
    return _centroids


def classify(query_embedding: np.ndarray) -> dict:
    """Classify a pre-computed, L2-normalized query embedding against the
    per-store centroids.

    Returns {"stores": [...], "doc_types": None, "confidence": float,
    "used_fallback": False, "scores": {store: similarity}}. Recall-safe:
    an off-topic/ambiguous query gets every store back with low confidence
    rather than an empty or wrong result.
    """
    centroids = _get_centroids()
    scores = {store: float(np.dot(query_embedding, centroid)) for store, centroid in centroids.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_store, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else -1.0
    margin = top_score - second_score

    if top_score < settings.INTENT_SEMANTIC_MIN_SIMILARITY:
        return {
            "stores": list(ALL_STORES),
            "doc_types": None,
            "confidence": 0.35,
            "used_fallback": False,
            "scores": scores,
        }

    if margin >= settings.INTENT_SEMANTIC_MARGIN:
        confidence = min(0.95, 0.6 + margin * 2)
        return {
            "stores": [top_store],
            "doc_types": None,
            "confidence": confidence,
            "used_fallback": False,
            "scores": scores,
        }

    # Close cluster of plausible stores — return all within MARGIN of the top
    # score rather than arbitrarily picking one.
    close_stores = sorted(store for store, score in scores.items()
                           if score >= top_score - settings.INTENT_SEMANTIC_MARGIN)
    return {
        "stores": close_stores,
        "doc_types": None,
        "confidence": 0.55,
        "used_fallback": False,
        "scores": scores,
    }


def reset_centroids_cache() -> None:
    """Test-only utility to force centroid recomputation (e.g. after
    monkeypatching embedding_service)."""
    global _centroids
    with _centroids_lock:
        _centroids = None
