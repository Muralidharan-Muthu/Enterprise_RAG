"""
BGE embedding service.
Model: BAAI/bge-large-en-v1.5 (1024-dim, requires instruction prefix).

IMPORTANT: Initialize at module level so the Celery worker loads the model once
on startup (~1.3GB download on first run). Never instantiate inside a task function.
"""
import logging

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Passage prefix required by bge-large-en-v1.5 for document embedding
PASSAGE_PREFIX = "Represent this sentence: "
# Query prefix for retrieval (Phase 2)
QUERY_PREFIX = "Represent this question for searching relevant passages: "

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading BGE embedding model: %s (device=%s)", settings.BGE_MODEL_NAME, settings.EMBEDDING_DEVICE)
        _model = SentenceTransformer(settings.BGE_MODEL_NAME, device=settings.EMBEDDING_DEVICE)
        logger.info("BGE model loaded.")
    return _model


def embed_passages(texts: list[str]) -> np.ndarray:
    """
    Embed a list of passage texts for storage.
    Returns float32 array of shape (len(texts), 1024), L2-normalized.
    """
    if not texts:
        return np.empty((0, 1024), dtype=np.float32)

    model = _get_model()
    prefixed = [PASSAGE_PREFIX + t for t in texts]

    embeddings = model.encode(
        prefixed,
        batch_size=settings.EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return embeddings.astype(np.float32)


def embed_query(query: str) -> np.ndarray:
    """
    Embed a single query string for similarity search (Phase 2).
    Returns float32 array of shape (1024,).

    Optionally cached (RETRIEVAL_CACHE_ENABLED + RETRIEVAL_CACHE_EMBEDDINGS_ENABLED):
    embeddings are a pure function of the query string, so a cache hit is
    numerically identical to a fresh encode.
    """
    _cache_on = settings.RETRIEVAL_CACHE_ENABLED and settings.RETRIEVAL_CACHE_EMBEDDINGS_ENABLED
    if _cache_on:
        from app.services import retrieval_cache
        cached = retrieval_cache.get_cached_embedding(query)
        if cached is not None:
            return cached

    model = _get_model()
    prefixed = QUERY_PREFIX + query
    embedding = model.encode(
        prefixed,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    result = embedding.astype(np.float32)

    if _cache_on:
        from app.services import retrieval_cache
        retrieval_cache.put_cached_embedding(query, result)

    return result


def warmup() -> None:
    """Pre-load the model at worker startup to avoid cold start on first task."""
    _get_model()
    logger.info("Embedding model warmed up.")


def _get_tokenizer():
    """Return the underlying HuggingFace tokenizer from the loaded SentenceTransformer.

    SentenceTransformer exposes the tokenizer either as ``model.tokenizer``
    (most versions) or on the first Transformer module (``model[0].tokenizer``).
    Reuses the already-loaded model — never triggers a second model load.
    """
    model = _get_model()
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is not None:
        return tokenizer
    # Fallback: first module in the SentenceTransformer pipeline (Transformer block)
    try:
        return model[0].tokenizer
    except Exception:
        return None


def count_tokens(text: str) -> int:
    """Count real BGE subword tokens for ``text`` (no special tokens added).

    Lazily reuses the already-loaded model/tokenizer — does not force a second
    model load. Empty/whitespace/None input returns 0.
    """
    if not text or not text.strip():
        return 0

    tokenizer = _get_tokenizer()
    if tokenizer is None:
        # Extremely defensive fallback — should not happen in practice.
        logger.warning("BGE tokenizer unavailable; falling back to whitespace count.")
        return len(text.split())

    encoded = tokenizer(text, add_special_tokens=False)
    input_ids = encoded["input_ids"] if isinstance(encoded, dict) else encoded.input_ids
    return len(input_ids)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate ``text`` (on whitespace boundaries) so its BGE token count <= max_tokens.

    Simple greedy approach: binary-search-free, just trims whole whitespace
    tokens from the end until the real token count fits. Adequate for the
    occasional oversized-sentence edge case — not a hot path.
    """
    if not text or not text.strip() or max_tokens <= 0:
        return ""

    words = text.split()
    if count_tokens(text) <= max_tokens:
        return text

    lo, hi = 0, len(words)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = " ".join(words[:mid])
        if count_tokens(candidate) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return " ".join(words[:lo])
