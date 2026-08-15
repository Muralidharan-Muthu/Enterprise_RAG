import logging
import math
import os

from app.config import settings

logger = logging.getLogger(__name__)

# BGE-reranker-large supports up to 512 tokens per pair; use the full window
# for better accuracy. Cap candidates at 40 to keep CPU latency reasonable.
MAX_RERANK_CANDIDATES = 40
RERANK_MAX_LENGTH = 512

# Log the configured model immediately at import so startup logs show which
# reranker will be used — catches stale env reads before the first query.
logger.info("Reranker configured: %s", settings.RERANKER_NAME)

_reranker = None


def _get_reranker():
    global _reranker
    if _reranker is None:
        import torch
        from sentence_transformers import CrossEncoder

        cpu_count = os.cpu_count() or 4
        torch.set_num_threads(cpu_count)

        logger.info("Loading reranker: %s (threads=%d)", settings.RERANKER_NAME, cpu_count)
        _reranker = CrossEncoder(settings.RERANKER_NAME, max_length=RERANK_MAX_LENGTH)
        logger.info("Reranker ready")
    return _reranker


def warmup() -> None:
    """Pre-load the reranker so the first query doesn't pay the load cost."""
    _get_reranker()
    logger.info("Reranker warmed up.")


def _sigmoid(x: float) -> float:
    """Map raw CrossEncoder logit to [0, 1]."""
    return 1.0 / (1.0 + math.exp(-x))


def score_pairs(pairs: list) -> list:
    """Score (query, text) pairs with the cross-encoder, returning raw logits.

    Shared entry point for callers that need relevance scores at a granularity
    other than whole-chunk reranking (e.g. sentence-level context compression).
    Loads/reuses the same CrossEncoder as rerank(). Raises on model failure so
    the caller can decide how to degrade (compression treats it as best-effort).
    """
    if not pairs:
        return []
    model = _get_reranker()
    raw = model.predict(pairs, show_progress_bar=False)
    return [float(s) for s in raw]


def _minmax_from_distances(chunks: list) -> None:
    """Set relevance_score on chunks using batch-relative min-max on cosine distance.

    Root cause of the 18% bug: raw `1.0 - distance` gives ~0.18 when cosine
    distance is ~0.82 (typical for relevant domain text). This is NOT a low
    relevance score — it is just how the embedding space is calibrated. The
    absolute distance value is not meaningful as a confidence signal; only the
    RELATIVE ordering within a batch is meaningful.

    Fix: min-max normalize within the batch so the closest chunk always scores
    near 1.0 and the farthest scores near 0.0.
    """
    if not chunks:
        return
    distances = [c.distance for c in chunks]
    lo, hi = min(distances), max(distances)
    span = hi - lo if hi != lo else 1.0
    for c in chunks:
        # Lower distance = more similar = higher score; invert after normalizing
        c.relevance_score = round(1.0 - (c.distance - lo) / span, 4)


def _normalize_logits(raw_scores: list) -> list:
    """Convert BGE-reranker-large raw logits to meaningful [0, 1] scores.

    Problem with plain sigmoid: BGE logits for correct matches on domain-specific
    text are typically in the range [-3, +2]. sigmoid(-1.5) = 0.18, which
    incorrectly shows 18 % confidence even when the answer is perfectly correct.

    Fix: batch-relative min-max normalization so the BEST chunk in this specific
    query always maps to a high score (reflecting that it IS the best match we
    found). A soft absolute floor via sigmoid prevents artificially high scores
    when ALL chunks are truly irrelevant (every logit << 0).

    Formula:
        sigmoid_score  = sigmoid(logit)          # absolute quality floor
        minmax_score   = (logit - min) / (range) # relative rank within batch
        final = 0.3 * sigmoid_score + 0.7 * minmax_score
    """
    if not raw_scores:
        return []

    floats = [float(s) for s in raw_scores]
    lo, hi = min(floats), max(floats)
    span = hi - lo if hi != lo else 1.0

    normalized = []
    for s in floats:
        sig = _sigmoid(s)
        minmax = (s - lo) / span          # 0.0 for worst, 1.0 for best
        combined = 0.3 * sig + 0.7 * minmax
        normalized.append(round(min(max(combined, 0.0), 1.0), 4))
    return normalized


def rerank(query: str, candidates: list, top_k: int = 5) -> list:
    """Re-rank candidates using BGE-reranker-large CrossEncoder.

    Falls back to batch-relative distance normalization on error so that the
    best-matching chunk always shows a meaningful (high) confidence score even
    when the reranker model is unavailable.
    """
    if not candidates:
        return []

    if len(candidates) <= top_k:
        # Small pool — skip the model, use normalized distances directly
        _minmax_from_distances(candidates)
        return sorted(candidates, key=lambda c: c.relevance_score, reverse=True)

    # Only re-rank the closest candidates by vector distance. The remaining
    # tail is already low-similarity; scoring it on CPU just adds latency.
    # Candidates arrive sorted by distance from the retriever.
    pool = candidates[:MAX_RERANK_CANDIDATES]

    try:
        model = _get_reranker()
        pairs = [(query, chunk.text) for chunk in pool]
        raw_scores = model.predict(pairs, show_progress_bar=False)

        # Normalize logits batch-relative so the best match in this query always
        # reflects high relevance, regardless of absolute logit magnitude.
        norm_scores = _normalize_logits(list(raw_scores))
        for chunk, score in zip(pool, norm_scores):
            # A verified table-row match (table_chunk_store hit, not just the
            # parent table's generic summary) carries the actual queried figures.
            # The cross-encoder scores it on lexical/semantic similarity alone,
            # which favors narrative prose describing a table over the table's
            # bare numeric rows — so give confirmed row matches a small nudge to
            # keep them from losing purely on wording. Bounded at +0.05 so a weak
            # row match still can't outrank a strongly relevant text chunk.
            if getattr(chunk, "is_child_match", False):
                score = min(1.0, score + 0.05)
            chunk.relevance_score = score

        return sorted(pool, key=lambda c: c.relevance_score, reverse=True)[:top_k]

    except Exception as e:
        logger.warning("Reranker failed, using distance order: %s", e)
        # Use batch-relative distance normalization — NOT raw 1.0-distance
        # which gives ~0.18 for typical relevant chunks (distance ~0.82).
        _minmax_from_distances(pool)
        return sorted(pool, key=lambda c: c.relevance_score, reverse=True)[:top_k]
