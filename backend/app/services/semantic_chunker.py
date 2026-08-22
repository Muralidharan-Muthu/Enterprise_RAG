"""
Semantic boundary chunker — LlamaIndex SemanticSplitterNodeParser algorithm.

Given a list of sentences and an embedding function, this module:
  1. Embeds each sentence (batched).
  2. Computes cosine distance between consecutive embeddings.
  3. Cuts where distance exceeds the configured percentile threshold.
  4. Applies hard ceiling (CHUNK_MAX_TOKENS) with force-split at the next-best
     interior breakpoint.
  5. Merges chunks below MIN_CHUNK_SIZE_TOKENS into the same-section neighbour.

All embedding and Gemma calls are synchronous — intended for the Celery worker
context, where embedding_service is already warm.

Gemma enrichment fills three previously-empty vector_store columns:
  section_title  — human-readable heading from the breadcrumb or Gemma
  keywords       — top keyphrases for BM25 / facet filtering
  semantic_type  — coarse type: paragraph | list | table_summary | clause | …

The enrichment step is *best-effort*.  Any parse or HTTP failure falls back to
heuristic values (breadcrumb title, semantic_type="paragraph", keywords=[]).
Chunking never crashes because of a Gemma outage.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional, Sequence

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Type alias for the injected embed function — accepts a list of strings,
# returns a float32 ndarray of shape (N, D).
EmbedFn = Callable[[list[str]], np.ndarray]


# ─────────────────────────────────────────────────────────────────────────────
# Cosine-distance helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance in [0, 2] between two L2-normalised vectors.

    BGE embeddings are already L2-normalised by embed_passages(), so the dot
    product equals cosine similarity directly.  We subtract from 1 to convert
    to distance (0 = identical, 2 = opposite).
    """
    # Clip to avoid -0.0 / floating-point overshoot outside [-1, 1]
    sim = float(np.clip(np.dot(a, b), -1.0, 1.0))
    return 1.0 - sim


def _pairwise_distances(embeddings: np.ndarray) -> list[float]:
    """Return len(embeddings) - 1 cosine distances between consecutive rows."""
    n = len(embeddings)
    if n < 2:
        return []
    return [
        _cosine_distance(embeddings[i], embeddings[i + 1])
        for i in range(n - 1)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Core breakpoint detection
# ─────────────────────────────────────────────────────────────────────────────

def _find_breakpoints(
    sentences: list[str],
    embed_fn: EmbedFn,
    percentile: int,
) -> list[int]:
    """Return 0-based sentence indices where a new chunk should *start*.

    Index 0 is always a start.  A breakpoint is added after sentence i when the
    cosine distance between sentence i and i+1 exceeds the ``percentile``-th
    percentile of all pairwise distances.

    Args:
        sentences:  Non-empty list of sentence strings.
        embed_fn:   Function accepting list[str] → ndarray (N, D), L2-normed.
        percentile: Integer in [0, 100].  95 means: cut only the 5 % most
                    dissimilar transitions.

    Returns:
        Sorted list of start indices (always starts with 0).
    """
    if len(sentences) <= 1:
        return [0]

    embeddings = embed_fn(sentences)          # (N, D)
    distances = _pairwise_distances(embeddings)

    if not distances:
        return [0]

    threshold = float(np.percentile(distances, percentile))

    breakpoints = [0]
    for i, dist in enumerate(distances):
        # dist[i] is the distance between sentence i and sentence i+1
        if dist >= threshold:
            breakpoints.append(i + 1)   # next chunk starts at i+1

    return breakpoints


# ─────────────────────────────────────────────────────────────────────────────
# Token counting
# ─────────────────────────────────────────────────────────────────────────────
#
# Two counters are available:
#   - _word_count_approx: legacy whitespace-word approximation.
#   - default_token_counter: real BGE subword tokenizer (embedding_service),
#     used when settings.CHUNK_USE_REAL_TOKENIZER is True.
#
# All public functions below accept an optional ``token_counter`` callable
# (Callable[[str], int]) so tests can inject a fake counter and avoid loading
# the 1.3 GB BGE model. When not provided, the module picks the counter based
# on the config flag at call time (not at import time).

TokenCounterFn = Callable[[str], int]


def _word_count_approx(text: str) -> int:
    return max(1, len(text.split()))


def default_token_counter(text: str) -> int:
    """Real BGE subword token counter, gated by settings.CHUNK_USE_REAL_TOKENIZER.

    Falls back to the legacy whitespace approximation when the flag is False,
    so existing behaviour is preserved exactly unless explicitly opted in.
    """
    if not settings.CHUNK_USE_REAL_TOKENIZER:
        return _word_count_approx(text)
    from app.services import embedding_service
    return max(1, embedding_service.count_tokens(text))


def _resolve_counter(token_counter: Optional[TokenCounterFn]) -> TokenCounterFn:
    return token_counter if token_counter is not None else default_token_counter


# Backward-compatible alias — legacy internal callers/tests may still reference
# this name; it now honours the injected counter path only when called directly
# with no override, defaulting to the whitespace approximation.
def _token_count(text: str) -> int:
    return _word_count_approx(text)


# ─────────────────────────────────────────────────────────────────────────────
# Semantic split: sentences → list[list[str]]  (one inner list = one chunk)
# ─────────────────────────────────────────────────────────────────────────────

def split_sentences_semantically(
    sentences: list[str],
    embed_fn: EmbedFn,
    percentile: int,
    max_tokens: int,
    min_tokens: int,
    token_counter: Optional[TokenCounterFn] = None,
) -> list[list[str]]:
    """Split ``sentences`` into variable-size groups using embedding distances.

    Algorithm:
    1. Detect semantic breakpoints via cosine-distance percentile cut.
    2. Apply CHUNK_MAX_TOKENS ceiling: if a candidate chunk exceeds max_tokens,
       force-split at the highest-distance interior breakpoint within it.
    3. Merge chunks below min_tokens into the *previous* group (same semantic
       unit — the caller ensures no cross-section merges are attempted).

    No overlap is added — that is intentional (variable-size semantic chunks do
    not need it; context prefix from the section breadcrumb aids retrieval).

    Args:
        sentences:  Ordered sentence strings for one logical unit.
        embed_fn:   Embedding function (injected for testability).
        percentile: Cosine-distance percentile threshold.
        max_tokens: Hard ceiling; chunks exceeding this are force-split.
        min_tokens: Chunks below this are merged into previous.
        token_counter: Optional injected Callable[[str], int]. Defaults to
            ``default_token_counter`` (real BGE tokenizer when
            CHUNK_USE_REAL_TOKENIZER is True, else whitespace word-count).

    Returns:
        List of sentence groups.  Each group is a non-empty list of sentence
        strings.  Groups preserve the original sentence order.
    """
    if not sentences:
        return []

    counter = _resolve_counter(token_counter)

    # ── Step 1: initial semantic breakpoints ──────────────────────────────────
    starts = _find_breakpoints(sentences, embed_fn, percentile)

    # Build candidate groups from breakpoints
    groups: list[list[str]] = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(sentences)
        groups.append(sentences[start:end])

    # ── Step 2: enforce CHUNK_MAX_TOKENS ceiling ──────────────────────────────
    groups = _enforce_max_tokens(groups, embed_fn, max_tokens, token_counter=counter)

    # ── Step 3: merge tiny chunks into neighbour ──────────────────────────────
    groups = _merge_small_chunks(groups, min_tokens, token_counter=counter)

    return [g for g in groups if g]  # safety: drop accidental empties


def _hard_split_sentence(sentence: str, max_tokens: int, token_counter: TokenCounterFn) -> list[str]:
    """Split a single indivisible sentence into whitespace-bounded pieces,
    each with token_counter(piece) <= max_tokens.

    Guarantees termination and never drops text. Used when a "group" cannot
    be split further because it is already a single sentence yet still
    exceeds the max-token ceiling (e.g. one giant run-on sentence/paragraph
    with no punctuation).
    """
    words = sentence.split()
    if not words:
        return [sentence] if sentence else []

    pieces: list[str] = []
    current: list[str] = []

    for word in words:
        candidate = current + [word]
        if current and token_counter(" ".join(candidate)) > max_tokens:
            pieces.append(" ".join(current))
            current = [word]
        else:
            current = candidate

    if current:
        pieces.append(" ".join(current))

    # Safety net: if a single word itself exceeds max_tokens (real tokenizer,
    # long token), or the loop somehow produced nothing, fall back to the
    # original sentence as one piece rather than dropping text or looping.
    if not pieces:
        pieces = [sentence]

    return pieces


def _enforce_max_tokens(
    groups: list[list[str]],
    embed_fn: EmbedFn,
    max_tokens: int,
    token_counter: Optional[TokenCounterFn] = None,
) -> list[list[str]]:
    """Force-split any group that exceeds max_tokens.

    Strategy: within the oversized group, re-embed the sentences and find the
    highest-distance interior boundary; cut there and recurse until all pieces
    fit.  If all interior distances are equal (degenerate), split at the
    midpoint to avoid an infinite loop.

    Degenerate case: if the oversized group is already a single sentence
    (cannot be split at a sentence boundary), hard-split it on whitespace
    into token-bounded pieces so no chunk silently exceeds CHUNK_MAX_TOKENS.
    """
    counter = _resolve_counter(token_counter)
    result: list[list[str]] = []
    for group in groups:
        text = " ".join(group)
        if counter(text) <= max_tokens:
            result.append(group)
            continue

        if len(group) <= 1:
            # Single indivisible sentence still over the limit — hard-split
            # on whitespace so the ceiling is guaranteed, never dropping text.
            sentence = group[0] if group else ""
            pieces = _hard_split_sentence(sentence, max_tokens, counter)
            result.extend([p] for p in pieces)
            continue

        # Find the best interior cut point
        cut = _best_interior_cut(group, embed_fn)
        left = group[:cut]
        right = group[cut:]

        # Recurse on each half
        result.extend(_enforce_max_tokens([left], embed_fn, max_tokens, token_counter=counter))
        result.extend(_enforce_max_tokens([right], embed_fn, max_tokens, token_counter=counter))

    return result


def _best_interior_cut(group: list[str], embed_fn: EmbedFn) -> int:
    """Return the index where group should be split (1 <= cut < len(group)).

    Picks the highest cosine-distance interior boundary.  Falls back to
    midpoint when distances are all zero or there is only one interior position.
    """
    n = len(group)
    if n <= 2:
        return 1   # only one possible cut

    try:
        embeddings = embed_fn(group)
        distances = _pairwise_distances(embeddings)
        # distances[i] = distance between group[i] and group[i+1]
        # A cut at index i+1 means: left = group[:i+1], right = group[i+1:]
        # We want interior cuts (exclude position 0 which would create empty left)
        best_i = int(np.argmax(distances))  # 0-based gap index
        cut = best_i + 1                    # convert gap to sentence index
        return max(1, min(cut, n - 1))
    except Exception:
        return n // 2


def _merge_small_chunks(
    groups: list[list[str]],
    min_tokens: int,
    token_counter: Optional[TokenCounterFn] = None,
) -> list[list[str]]:
    """Merge groups below min_tokens into the immediately preceding group.

    A leading tiny group (no predecessor) is merged into the successor instead.
    """
    if not groups:
        return []

    counter = _resolve_counter(token_counter)

    merged: list[list[str]] = []
    for group in groups:
        text = " ".join(group)
        if counter(text) < min_tokens and merged:
            # Append to previous chunk
            merged[-1].extend(group)
        else:
            merged.append(list(group))

    # Edge case: first chunk was tiny and had no predecessor — check again
    # after the forward merge pass.
    if len(merged) >= 2 and counter(" ".join(merged[0])) < min_tokens:
        merged[1] = merged[0] + merged[1]
        merged = merged[1:]

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Groq enrichment
# ─────────────────────────────────────────────────────────────────────────────

# Strict JSON prompt — Groq must return an array aligned to the input chunks.
_ENRICH_PROMPT_TMPL = (
    "You are an expert document chunk classifier and metadata extractor. You will be given {n} text chunks "
    "from a document, separated by the marker <CHUNK_SEP>.\n"
    "For EACH chunk (in order), output a JSON array of exactly {n} objects.\n"
    "Each object MUST have:\n"
    '  "section_title": concise heading (≤12 words) describing the chunk topic,\n'
    '  "keywords": a JSON array of 3–8 key terms or phrases from the chunk,\n'
    '  "semantic_type": one of "legal_clause", "paragraph", "list", "table_summary", "definition", "procedure", "financial_data".\n'
    "If the chunk is a legal clause, agreement term, statutory provision, or contractual rule (e.g. Termination, Dispute Resolution, Liability, Indemnity, Jurisdiction, Obligations, Confidentiality), set semantic_type to 'legal_clause' and ALSO include:\n"
    '  "clause_number": clause number or section code (e.g. "1.", "2.1", "Article IV", or null),\n'
    '  "clause_title": title of the clause (e.g. "TERMINATION FOR CAUSE", or null),\n'
    '  "clause_type": one of [obligation, prohibition, right, definition, liability, indemnification, termination, confidentiality, dispute_resolution, force_majeure, warranty, penalty, governing_law, general],\n'
    '  "risk_level": "high" | "medium" | "low" | null,\n'
    '  "risk_rationale": brief explanation of risk or legal obligation (or null),\n'
    '  "parties_mentioned": list of named entities or parties mentioned (or []).\n'
    "Output ONLY valid JSON — no markdown fences, no explanation.\n\n"
    "Chunks:\n{chunks_text}"
)


def _build_enrich_prompt(chunk_texts: list[str]) -> str:
    n = len(chunk_texts)
    chunks_text = "\n<CHUNK_SEP>\n".join(chunk_texts)
    return _ENRICH_PROMPT_TMPL.format(n=n, chunks_text=chunks_text)


def _parse_enrich_response(raw: str, n: int) -> list[dict]:
    """Parse Groq's JSON response. Returns a list of dicts or raises ValueError."""
    # Strip markdown code fences if the model wraps in ```json ... ```
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    raw = raw.strip()

    data = json.loads(raw)
    if not isinstance(data, list) or len(data) != n:
        raise ValueError(f"Expected JSON array of {n} items, got: {type(data).__name__} len={len(data) if isinstance(data, list) else '?'}")

    result = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"Array element is not a dict: {item!r}")
        stype = str(item.get("semantic_type", "paragraph")).strip() or "paragraph"
        cnum = str(item.get("clause_number")).strip() if item.get("clause_number") else None
        ctitle = str(item.get("clause_title")).strip() if item.get("clause_title") else None
        ctype = str(item.get("clause_type", "general")).strip() or "general"
        risk = str(item.get("risk_level")).strip() if item.get("risk_level") in ("high", "medium", "low") else None

        result.append({
            "section_title": str(item.get("section_title", "")).strip() or None,
            "keywords": list(item.get("keywords", [])) if isinstance(item.get("keywords"), list) else [],
            "semantic_type": stype,
            "clause_number": cnum,
            "clause_title": ctitle,
            "clause_type": ctype,
            "risk_level": risk,
            "risk_rationale": str(item.get("risk_rationale")).strip() if item.get("risk_rationale") else None,
            "parties_mentioned": list(item.get("parties_mentioned", [])) if isinstance(item.get("parties_mentioned"), list) else [],
            "obligor": str(item.get("obligor")).strip() if item.get("obligor") else None,
            "obligee": str(item.get("obligee")).strip() if item.get("obligee") else None,
        })
    return result


def _fallback_enrichment(
    chunk_texts: list[str],
    section_paths: list[list[str]],
    block_types_list: list[list[str]],
) -> list[dict]:
    """Heuristic enrichment used when Groq is unavailable or returns bad JSON."""
    from app.services.chunker import detect_clause_nature
    result = []
    for i, text in enumerate(chunk_texts):
        path = section_paths[i] if i < len(section_paths) else []
        btypes = block_types_list[i] if i < len(block_types_list) else []
        section_title = path[-1] if path else None

        is_clause, cnum, ctitle, ctype = detect_clause_nature(text, section_title, btypes)

        if is_clause:
            stype = "legal_clause"
        elif "list" in btypes:
            stype = "list"
        elif "image_analysis" in btypes:
            stype = "image_analysis"
        else:
            stype = "paragraph"

        result.append({
            "section_title": ctitle or section_title,
            "keywords": [],
            "semantic_type": stype,
            "clause_number": cnum,
            "clause_title": ctitle,
            "clause_type": ctype,
            "risk_level": "medium" if is_clause else None,
            "risk_rationale": None,
            "parties_mentioned": [],
            "obligor": None,
            "obligee": None,
        })
    return result


def enrich_chunks_with_groq(
    chunk_texts: list[str],
    section_paths: list[list[str]],
    block_types_list: list[list[str]],
    groq_chat_fn: Callable,
    batch_size: int,
    max_tokens: int = 1200,
) -> list[dict]:
    """Batch-enrich chunks via Groq; falls back gracefully on any failure.

    Args:
        chunk_texts:      List of chunk content strings (core text, no prefix).
        section_paths:    Parallel list of section path lists (for fallback titles).
        block_types_list: Parallel list of block-type lists (for fallback types).
        groq_chat_fn:     Callable matching groq_client.chat() signature:
                          ``(messages: list[dict], max_tokens: int) -> str``.
        batch_size:       Maximum number of chunks per Groq call.
        max_tokens:       Max tokens in Groq's JSON response.

    Returns:
        List of dicts with keys ``section_title``, ``keywords``, ``semantic_type``.
        Length == len(chunk_texts).  Never raises.
    """
    if not chunk_texts:
        return []

    all_enriched: list[dict] = []
    n_total = len(chunk_texts)

    for batch_start in range(0, n_total, batch_size):
        batch_end = min(batch_start + batch_size, n_total)
        batch_texts = chunk_texts[batch_start:batch_end]
        batch_paths = section_paths[batch_start:batch_end]
        batch_btypes = block_types_list[batch_start:batch_end]
        n_batch = len(batch_texts)

        try:
            prompt = _build_enrich_prompt(batch_texts)
            messages = [{"role": "user", "content": prompt}]
            raw = groq_chat_fn(messages=messages, max_tokens=max_tokens)
            enriched = _parse_enrich_response(raw, n_batch)
            all_enriched.extend(enriched)
            logger.debug(
                "Groq enriched batch [%d:%d] (%d chunks)",
                batch_start, batch_end, n_batch,
            )
        except Exception as exc:
            logger.warning(
                "Groq enrichment failed for batch [%d:%d] — using heuristics: %s",
                batch_start, batch_end, exc,
            )
            all_enriched.extend(
                _fallback_enrichment(batch_texts, batch_paths, batch_btypes)
            )

    return all_enriched


# Backward-compat alias
enrich_chunks_with_gemma = enrich_chunks_with_groq
