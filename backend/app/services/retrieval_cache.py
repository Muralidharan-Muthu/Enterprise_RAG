"""
Retrieval cache — additive, opt-in (RETRIEVAL_CACHE_ENABLED, default False).

Two independent in-process cache layers, both following the established
_signed_url_cache pattern in supabase_storage.py (dict + threading.Lock +
time.monotonic() TTL + prune-then-clear-fallback eviction):

  1. Embedding cache (RETRIEVAL_CACHE_EMBEDDINGS_ENABLED, default True once
     the master flag is on): caches embed_query() output keyed on the RAW,
     unnormalized query string. Safe by construction — the BGE model is static
     and deterministic, so a cached embedding is never stale. The TTL is pure
     memory housekeeping, not a correctness mechanism.

  2. Full-result cache (RETRIEVAL_CACHE_RESULTS_ENABLED, default False even
     when the master flag is on): caches retrieve() output. Results depend on
     live DB state, so this layer accepts BOUNDED STALENESS — a document
     ingested after a cache write stays invisible for up to
     RETRIEVAL_CACHE_RESULT_TTL_SECONDS (default 90s). A short TTL was chosen
     over cross-process invalidation (documents-version counter / DB freshness
     check) because the API process and the Celery ingestion worker do not
     share memory — any true invalidation signal would need shared state or a
     DB round-trip on the hit path, defeating the cache's purpose.

Design notes:
- Cache-hit results are returned as per-object shallow COPIES: downstream
  ranking (rrf_merge, reranker) mutates chunk.relevance_score in place, and
  shared instances would let one request's rerank corrupt another's.
- Query strings are NOT normalized (no lower/strip) — the raw string is the
  true model input; normalizing would silently map distinct inputs together.
- An intent cache was considered and rejected: the full-result cache already
  subsumes repeated intent classification for identical retrieve() calls.
- In-process (per API worker), not Redis: a Redis round-trip plus numpy
  serialization would eat most of the embedding cache's latency win. Known
  limitation: no cross-worker sharing of warm entries.
"""
import copy
import logging
import threading
import time
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# ── Embedding cache ──────────────────────────────────────────────────────────
_embedding_cache: dict[str, tuple[np.ndarray, float]] = {}
_embedding_lock = threading.Lock()

# ── Full-retrieval-result cache ──────────────────────────────────────────────
_result_cache: dict[tuple, tuple[list, float]] = {}
_result_lock = threading.Lock()

# Hit/miss counters (process lifetime; logged periodically, see _maybe_log_stats)
_stats = {"embed_hits": 0, "embed_misses": 0, "result_hits": 0, "result_misses": 0}
_stats_call_counter = 0
_STATS_LOG_EVERY = 100


def _prune_expired(cache: dict, now: float) -> None:
    """Remove expired entries. Caller must hold the cache's lock."""
    for key in [k for k, (_, exp) in cache.items() if exp <= now]:
        cache.pop(key, None)


def _bounded_put(cache: dict, lock: threading.Lock, key, value, ttl: float, max_entries: int) -> None:
    """Insert under lock with prune-then-clear-fallback size bounding
    (mirrors _prune_signed_url_cache in supabase_storage.py)."""
    now = time.monotonic()
    with lock:
        if len(cache) >= max_entries:
            _prune_expired(cache, now)
            if len(cache) >= max_entries:
                cache.clear()
        cache[key] = (value, now + ttl)


# ── Embedding cache API ──────────────────────────────────────────────────────

def get_cached_embedding(query: str) -> Optional[np.ndarray]:
    """Return a COPY of the cached embedding for this exact query string, or None."""
    now = time.monotonic()
    with _embedding_lock:
        entry = _embedding_cache.get(query)
        if entry is not None and entry[1] > now:
            _stats["embed_hits"] += 1
            return entry[0].copy()
        if entry is not None:
            _embedding_cache.pop(query, None)
        _stats["embed_misses"] += 1
    return None


def put_cached_embedding(query: str, embedding: np.ndarray) -> None:
    # Store a copy: the caller receives (and may mutate) the original array,
    # and a shared reference would let that mutation corrupt the cached value.
    _bounded_put(
        _embedding_cache, _embedding_lock, query, embedding.copy(),
        ttl=settings.RETRIEVAL_CACHE_EMBEDDING_TTL_SECONDS,
        max_entries=settings.RETRIEVAL_CACHE_EMBEDDING_MAX_ENTRIES,
    )


# ── Full-result cache API ────────────────────────────────────────────────────

def _intent_key(intent: Optional[dict]):
    """Stable, hashable projection of an intent dict — only the two fields
    _select_stores() actually reads."""
    if intent is None:
        return None
    return (
        tuple(sorted(intent.get("stores") or [])),
        round(float(intent.get("confidence", 0.0)), 3),
    )


def _table_filters_key(table_filters):
    """Stable, hashable projection of a TableFilters dataclass (or None)."""
    if not table_filters or table_filters.is_empty():
        return None
    return (
        table_filters.currency,
        table_filters.fiscal_year,
        table_filters.table_category,
        table_filters.numeric_only,
        table_filters.min_quality,
    )


def _result_key(query, document_types, document_id, top_k_per_store, use_intent,
                intent, table_filters) -> tuple:
    return (
        query,
        tuple(sorted(document_types)) if document_types else None,
        document_id,
        top_k_per_store,
        use_intent,
        _intent_key(intent),
        _table_filters_key(table_filters),
    )


def get_cached_retrieval(query, document_types, document_id, top_k_per_store,
                         use_intent, intent, table_filters) -> Optional[list]:
    """Return per-object shallow copies of a cached retrieve() result, or None.

    Copies are mandatory: downstream reranking mutates relevance_score in
    place; sharing instances across requests would corrupt concurrent views.
    """
    key = _result_key(query, document_types, document_id, top_k_per_store,
                      use_intent, intent, table_filters)
    now = time.monotonic()
    with _result_lock:
        entry = _result_cache.get(key)
        if entry is not None and entry[1] > now:
            _stats["result_hits"] += 1
            return [copy.copy(c) for c in entry[0]]
        if entry is not None:
            _result_cache.pop(key, None)
        _stats["result_misses"] += 1
    _maybe_log_stats()
    return None


def put_cached_retrieval(query, document_types, document_id, top_k_per_store,
                         use_intent, intent, table_filters, results: list) -> None:
    key = _result_key(query, document_types, document_id, top_k_per_store,
                      use_intent, intent, table_filters)
    # Store copies too, so the caller's subsequent in-place mutations
    # (reranking) don't leak into the cached snapshot.
    _bounded_put(
        _result_cache, _result_lock, key, [copy.copy(c) for c in results],
        ttl=settings.RETRIEVAL_CACHE_RESULT_TTL_SECONDS,
        max_entries=settings.RETRIEVAL_CACHE_RESULT_MAX_ENTRIES,
    )


# ── Stats / ops ──────────────────────────────────────────────────────────────

def _maybe_log_stats() -> None:
    """Log hit rates every _STATS_LOG_EVERY result-cache lookups (avoids per-request spam)."""
    global _stats_call_counter
    _stats_call_counter += 1
    if _stats_call_counter % _STATS_LOG_EVERY != 0:
        return
    eh, em = _stats["embed_hits"], _stats["embed_misses"]
    rh, rm = _stats["result_hits"], _stats["result_misses"]
    logger.info(
        "STAGE retrieval_cache: embed hit_rate=%.0f%% (%d/%d) result hit_rate=%.0f%% (%d/%d) sizes=(%d,%d)",
        100.0 * eh / max(eh + em, 1), eh, eh + em,
        100.0 * rh / max(rh + rm, 1), rh, rh + rm,
        len(_embedding_cache), len(_result_cache),
    )


def cache_stats() -> dict:
    """Snapshot of counters and sizes (for logging/health/debug use)."""
    return {
        **_stats,
        "embedding_entries": len(_embedding_cache),
        "result_entries": len(_result_cache),
    }


def clear_all() -> None:
    """Test/ops utility — not called from any production code path."""
    with _embedding_lock:
        _embedding_cache.clear()
    with _result_lock:
        _result_cache.clear()
    for k in _stats:
        _stats[k] = 0
