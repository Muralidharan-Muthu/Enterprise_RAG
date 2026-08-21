"""
Context Compression Service — extractive, accuracy-first.

Position in the pipeline: AFTER reranking, BEFORE synthesis. The reranker picks
the top-k most relevant *chunks*; this stage then trims each text chunk down to
its query-relevant *sentences*, so the Groq prompt spends its character budget
on signal instead of lead-biased filler.

Design principles (why this shape):
- Extractive, never generative. We SELECT existing sentences; we never
  summarize or rewrite. A chunk's surviving text is always a verbatim substring
  set of the original, so numbers/quotes stay exact and citations stay faithful.
- Relevance signal = the cross-encoder reranker (reranker_service.score_pairs),
  the same model that ranked the chunks — the highest-accuracy query/text
  relevance signal already resident in this process. No new model, no LLM call.
- Citation-safe: results land in chunk.compressed_text; chunk.text (what the
  citation panel renders) is never mutated.
- Exemptions (accuracy over compression):
    * tables  — the synthesis system prompt demands verbatim figures/rows;
    * images  — caption + OCR are already terse and non-sentential;
    * from_graph chunks — included precisely because their embedding similarity
      to the query is poor; sentence-scoring would wrongly prune them.
- Never-empty guarantee: every compressed chunk keeps at least
  CONTEXT_COMPRESSION_MIN_KEEP of its highest-scoring sentences, so compression
  can only ever concentrate a chunk, never blank it out.
- Best-effort: any failure (model unavailable, odd text) leaves compressed_text
  as None and the caller falls back to the full chunk text. Compression can
  never make an answer worse than the uncompressed baseline.

Public API:
    compress_chunks(query, chunks) -> None   # mutates chunk.compressed_text in place
"""
import logging
import re

from app.config import settings
from app.services import reranker_service

logger = logging.getLogger(__name__)

# Store types whose `text` is prose worth sentence-splitting. Tables and images
# are handled verbatim by synthesis and are intentionally excluded.
_COMPRESSIBLE_STORES = frozenset({"vector", "clause", "research"})

# Sentence boundary: end punctuation + whitespace, guarded against splitting on
# a few common abbreviations and decimal points. Deliberately simple and
# dependency-free — over-splitting is harmless here (each fragment just gets
# scored independently), under-splitting only means coarser compression.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg", "ie",
    "fig", "no", "inc", "ltd", "co", "corp", "u.s", "u.k", "e.g", "i.e",
}
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")


def _sigmoid(x: float) -> float:
    # Local copy so this module doesn't depend on reranker internals.
    import math
    return 1.0 / (1.0 + math.exp(-x))


def split_sentences(text: str) -> list:
    """Split prose into sentences. Merges fragments that end on a known
    abbreviation back into the following fragment so 'Dr. Smith' stays whole."""
    text = (text or "").strip()
    if not text:
        return []

    raw = _SENT_SPLIT.split(text)
    merged: list = []
    for frag in raw:
        frag = frag.strip()
        if not frag:
            continue
        if merged:
            # Look at the last word of the previous fragment — if it's a known
            # abbreviation, this split was spurious; re-join.
            prev = merged[-1]
            last_word = re.sub(r"[^\w.]", "", prev.split()[-1]).rstrip(".").lower()
            if last_word in _ABBREVIATIONS:
                merged[-1] = f"{prev} {frag}"
                continue
        merged.append(frag)
    return merged


def _should_compress(chunk) -> bool:
    if getattr(chunk, "store_type", None) not in _COMPRESSIBLE_STORES:
        return False
    if getattr(chunk, "from_graph", False):
        return False
    if getattr(chunk, "is_child_match", False):
        # A table row-window match rendered as a table — leave verbatim.
        return False
    return bool(getattr(chunk, "text", None))


def compress_chunks(query: str, chunks: list) -> None:
    """Populate chunk.compressed_text for the compressible chunks in `chunks`.

    Mutates in place; returns None. Never raises — on any error the affected
    chunks keep compressed_text=None and synthesis falls back to full text.

    All sentences across all eligible chunks are scored in a SINGLE cross-encoder
    batch (bounded by CONTEXT_COMPRESSION_MAX_PAIRS) to amortize model overhead.
    """
    if not settings.CONTEXT_COMPRESSION_ENABLED or not chunks or not query:
        return

    min_sentences = settings.CONTEXT_COMPRESSION_MIN_SENTENCES
    keep_score = settings.CONTEXT_COMPRESSION_KEEP_SCORE
    max_sentences = settings.CONTEXT_COMPRESSION_MAX_SENTENCES
    min_keep = max(1, settings.CONTEXT_COMPRESSION_MIN_KEEP)
    max_pairs = settings.CONTEXT_COMPRESSION_MAX_PAIRS

    # ── 1. Collect sentences to score, tracking which chunk each belongs to ──
    #   plan[i] = (chunk, [sentences]) for chunks we will actually compress.
    plan: list = []
    pairs: list = []           # flat (query, sentence) list for one predict() call
    owners: list = []          # owners[j] = index into `plan` for pairs[j]

    for chunk in chunks:
        if not _should_compress(chunk):
            continue
        sentences = split_sentences(chunk.text)
        if len(sentences) < min_sentences:
            # Already short/dense — nothing to gain, and pruning risks fact loss.
            continue
        if len(pairs) >= max_pairs:
            logger.debug("Compression pair budget (%d) reached — remaining chunks left uncompressed", max_pairs)
            break

        # Respect the global pair budget: if this chunk would overflow it, take
        # as many leading sentences as fit (they retain original order).
        room = max_pairs - len(pairs)
        if len(sentences) > room:
            sentences = sentences[:room]
            if len(sentences) < min_sentences:
                continue

        plan_idx = len(plan)
        plan.append((chunk, sentences))
        for s in sentences:
            pairs.append((query, s))
            owners.append(plan_idx)

    if not pairs:
        return

    # ── 2. Score every sentence against the query in one batch ───────────────
    try:
        logits = reranker_service.score_pairs(pairs)
    except Exception as exc:
        logger.warning("Context compression scoring failed (leaving chunks uncompressed): %s", exc)
        return
    if len(logits) != len(pairs):
        logger.warning("Compression score count mismatch (%d != %d) — skipping", len(logits), len(pairs))
        return

    scores = [_sigmoid(x) for x in logits]

    # ── 3. Regroup scores per chunk and select sentences ─────────────────────
    per_chunk_scores: list = [[] for _ in plan]
    for j, owner in enumerate(owners):
        per_chunk_scores[owner].append(scores[j])

    compressed_count = 0
    for plan_idx, (chunk, sentences) in enumerate(plan):
        s_scores = per_chunk_scores[plan_idx]
        kept = _select_sentences(sentences, s_scores, keep_score, max_sentences, min_keep)
        if not kept:
            continue
        compressed = " ".join(kept)
        # Only record a win when we actually dropped something — otherwise leave
        # compressed_text None so synthesis uses the original untouched.
        if len(compressed) < len(chunk.text.strip()):
            chunk.compressed_text = compressed
            compressed_count += 1

    if compressed_count:
        logger.info(
            "Context compression: compressed %d/%d eligible chunk(s), scored %d sentence(s)",
            compressed_count, len(plan), len(pairs),
        )


def _select_sentences(
    sentences: list,
    scores: list,
    keep_score: float,
    max_sentences: int,
    min_keep: int,
) -> list:
    """Pick sentences to keep, preserving their ORIGINAL order.

    Selection: keep every sentence scoring >= keep_score; if fewer than min_keep
    qualify, top up with the next highest-scoring sentences; cap at
    max_sentences (highest scores win the cap). Order of the kept set always
    matches the source so the passage still reads naturally.
    """
    n = len(sentences)
    if n == 0:
        return []

    order_by_score = sorted(range(n), key=lambda i: scores[i], reverse=True)

    keep_idx = {i for i in range(n) if scores[i] >= keep_score}

    # Guarantee a floor of min_keep, filling from the top scorers.
    for i in order_by_score:
        if len(keep_idx) >= min_keep:
            break
        keep_idx.add(i)

    # Enforce the ceiling: if too many survive, keep only the top max_sentences.
    if len(keep_idx) > max_sentences:
        keep_idx = set(order_by_score[:max_sentences])

    return [sentences[i] for i in range(n) if i in keep_idx]
