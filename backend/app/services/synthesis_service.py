import asyncio
import json
import logging
import re

from app.config import settings
from app.services import gemma_client

logger = logging.getLogger(__name__)


async def _maybe_compress(query: str, chunks: list) -> None:
    """Extractive context compression (best-effort). Runs the CPU-bound
    cross-encoder sentence scoring in a thread so the event loop stays free.
    Populates chunk.compressed_text in place; on any failure the chunks are left
    untouched and _content_for_chunk falls back to full text. No-op when
    CONTEXT_COMPRESSION_ENABLED is off."""
    if not settings.CONTEXT_COMPRESSION_ENABLED or not chunks:
        return
    try:
        from app.services import context_compression_service
        await asyncio.to_thread(context_compression_service.compress_chunks, query, chunks)
    except Exception as e:  # defensive — compress_chunks already swallows its own errors
        logger.warning("Context compression skipped (%s)", e)

_SYSTEM_PROMPT = (
    "You are a document intelligence assistant. "
    "Answer using only the provided context. Cite sources as [1], [2]. "
    "Use markdown: **bold** key facts, - bullet points. Be concise. "
    "When a source is a Table, quote the exact figures/values/rows verbatim from "
    "it — do NOT describe the table's existence, purpose, or methodology in place "
    "of citing its actual numbers. If the query asks for specific data and a Table "
    "source is present, the answer must include those concrete values."
)

_CONVERSATIONAL_SYSTEM_PROMPT = (
    "You are the assistant for a document Q&A tool. "
    "The user just sent a greeting or general small talk — NOT a question about any specific "
    "document. Respond warmly in one or two sentences, and naturally invite them to ask a "
    "question about their uploaded documents. "
    "Do NOT reference, summarize, or describe the CONTENTS of any specific document — you have "
    "no document context for this message."
)

# Relevance score below this threshold → treat query as conversational/off-topic
_MIN_RELEVANCE_THRESHOLD = 0.15

_USER_TEMPLATE = """User Query: {query}

Retrieved Context:
{context_blocks}

Write a clear, complete answer to the query grounded in the context above, with
inline citations like [1], [2] pointing at the numbered sources you used.

Return ONLY a JSON object (no markdown fences, no text before or after it):
{{
  "answer": "<detailed answer with inline citations [1], [2], etc.>",
  "confidence": <A number 0.0-1.0. Use 0.85-1.0 when the context directly and completely answers the query. Use 0.65-0.84 when the answer is mostly covered but some details are inferred. Use 0.40-0.64 when only partial information is available. Use below 0.40 when the context is largely irrelevant.>,
  "sources_used": [<list of citation numbers actually used>],
  "notes": "<brief note about limitations or gaps, or null>"
}}"""

_RATING_SYSTEM_PROMPT = (
    "You are a strict grader. Judge how well the given context supports the "
    "given answer to the given query. Do not answer the query yourself."
)

_RATING_USER_TEMPLATE = """Query: {query}

Context:
{context_blocks}

Answer to grade:
{answer}

Rate 0.0-1.0 how well the context supports this answer. Use 0.85-1.0 when the
context directly and completely supports it. Use 0.65-0.84 when mostly
supported with minor inference. Use 0.40-0.64 when only partially supported.
Use below 0.40 when largely unsupported by the context.

Return ONLY a JSON object (no markdown fences, no text before or after it):
{{"confidence": <number 0.0-1.0>}}"""


async def synthesize_conversational(query: str) -> str:
    """Public entry-point for conversational queries (no document context).
    Returns a plain answer string — callers build their own response envelope."""
    if settings.GEMMA4_BASE_URL:
        try:
            result = await _call_gemma_conversational(query)
            return result["answer"]
        except Exception as e:
            logger.warning("Gemma conversational call failed (%s)", e)
    return "Hello! How can I help you with your documents?"


async def synthesize(query: str, chunks: list) -> dict:
    """Generate synthesized answer with citations. Falls back if Gemma unavailable."""
    if not chunks:
        return {
            "answer": (
                "No relevant documents found for your query. "
                "Please try a different search or upload relevant documents first."
            ),
            "confidence": 0.0,
            "confidence_breakdown": _retrieval_only_breakdown(retrieval_confidence_breakdown([])),
            "sources_used": [],
            "notes": "No matching content found in the knowledge base.",
        }

    # If all retrieved chunks are irrelevant (low reranker scores), the query is
    # conversational or off-topic — answer without injecting document context.
    top_score = max((getattr(c, "relevance_score", 0.0) for c in chunks), default=0.0)
    if top_score < _MIN_RELEVANCE_THRESHOLD:
        if settings.GROQ_BASE_URL or settings.GEMMA4_BASE_URL:
            try:
                return await _call_gemma_conversational(query)
            except Exception as e:
                logger.warning("Groq conversational call failed (%s)", e)
        return {
            "answer": "Hello! How can I help you with your documents?",
            "confidence": 1.0,
            "confidence_breakdown": _retrieval_only_breakdown(retrieval_confidence_breakdown([])),
            "sources_used": [],
            "notes": None,
        }

    await _maybe_compress(query, chunks)
    context_blocks = _build_context(chunks)
    ret_breakdown = retrieval_confidence_breakdown(chunks)

    if not (settings.GROQ_BASE_URL or settings.GEMMA4_BASE_URL):
        logger.info("GROQ_BASE_URL not set — using fallback synthesis")
        return _fallback(chunks, ret_breakdown)

    try:
        return await _call_gemma(query, context_blocks, retrieval_breakdown=ret_breakdown)
    except Exception as e:
        logger.warning("Groq synthesis failed (%s), using fallback", e)
        return _fallback(chunks, ret_breakdown)


_STORE_LABELS = {
    "vector": "Document",
    "clause": "Legal Clause",
    "research": "Research",
    "table": "Table",
    "image": "Figure/Image",
}


_BLOCK_SEP = "\n\n---\n\n"


def _build_context(chunks: list) -> str:
    """Assemble numbered context blocks, highest-ranked chunk first, while
    keeping the TOTAL assembled context under settings.SYNTHESIS_CONTEXT_MAX_CHARS.

    Per-chunk truncation (see _content_for_chunk) already bounds each block, but
    with enough retrieved chunks the sum can still overflow the Gemma context
    window. Blocks are accumulated in order; once the next block would push the
    running total over budget, it is trimmed to fit (if any room remains) or
    dropped entirely, and accumulation stops. Because chunks arrive pre-sorted
    by relevance, the budget always keeps the best-ranked chunks."""
    budget = settings.SYNTHESIS_CONTEXT_MAX_CHARS

    blocks = []
    total_len = 0
    dropped = 0

    for i, chunk in enumerate(chunks, 1):
        label = _STORE_LABELS.get(chunk.store_type, "Document")
        meta_parts = [f"Source: {chunk.document_filename or 'unknown'}"]
        if chunk.page_number:
            meta_parts.append(f"Page {chunk.page_number}")
        if chunk.section_title:
            meta_parts.append(f"Section: {chunk.section_title}")
        if chunk.clause_type:
            meta_parts.append(f"Type: {chunk.clause_type}")
        if chunk.risk_level:
            meta_parts.append(f"Risk: {chunk.risk_level}")
        if chunk.source_doi:
            meta_parts.append(f"DOI: {chunk.source_doi}")

        content = _content_for_chunk(chunk)
        block = f"[{i}] {label} | {' | '.join(meta_parts)}\n{content}"

        sep_len = len(_BLOCK_SEP) if blocks else 0
        remaining = budget - total_len - sep_len

        if remaining <= 0:
            dropped += len(chunks) - i + 1
            logger.info(
                "synthesis context budget (%d chars) reached — dropping %d remaining chunk(s)",
                budget, dropped,
            )
            break

        if len(block) > remaining:
            # Trim this block to fit exactly, then stop — it's the last one.
            block = block[:remaining]
            blocks.append(block)
            total_len += sep_len + len(block)
            dropped = len(chunks) - i
            logger.info(
                "synthesis context budget (%d chars) reached — trimmed chunk %d and "
                "dropped %d remaining chunk(s)",
                budget, i, dropped,
            )
            break

        blocks.append(block)
        total_len += sep_len + len(block)

    return _BLOCK_SEP.join(blocks)


def _content_for_chunk(chunk) -> str:
    """Render the most informative text for a chunk depending on its store.
    Tables → markdown grid; images → caption + OCR; everything else → text."""
    store = chunk.store_type
    if store == "table":
        # Child row-window match: chunk.text is the SPECIFIC rows that matched
        # the query (table_chunk_store.serialized_text) — render those first,
        # then a short parent-table excerpt for header/column context. Without
        # this, the full-table markdown's [:1000] head slice could drop the
        # very rows the fine-grained search matched. getattr (not attribute
        # access) so objects lacking the field degrade to the legacy render.
        if getattr(chunk, "is_child_match", False) and chunk.text:
            child_part = chunk.text[:1200]
            parent_part = (chunk.table_markdown or "")[:400]
            if parent_part:
                return f"{child_part}\n\n(Full table excerpt: {parent_part})"
            return child_part
        return (chunk.table_markdown or chunk.text or "")[:2000]
    if store == "image":
        parts = []
        if getattr(chunk, "caption", None):
            parts.append(f"Caption: {chunk.caption}")
        if getattr(chunk, "ocr_text", None):
            parts.append(f"Text in image: {chunk.ocr_text}")
        return ("\n".join(parts) or chunk.text or "(image)")[:1000]
    # Text stores: prefer the extractive-compressed subset when present
    # (context_compression_service). It's a verbatim sentence-selection of
    # chunk.text, so citation fidelity is unaffected; falls back to full text.
    content = getattr(chunk, "compressed_text", None) or chunk.text or ""
    return content[:600]


async def _call_gemma_conversational(query: str) -> dict:
    """Call Gemma for greetings / off-topic queries without any document context."""
    raw = await gemma_client.chat_async(
        messages=[
            {"role": "system", "content": _CONVERSATIONAL_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        max_tokens=256,
        temperature=0.7,
    )
    answer = raw.strip() if isinstance(raw, str) else str(raw)
    # Strip JSON if model still wraps response
    obj = _extract_first_json_object(answer)
    if obj and str(obj.get("answer", "")).strip():
        answer = str(obj["answer"])
    return {
        "answer": answer,
        "confidence": 1.0,
        "confidence_breakdown": _retrieval_only_breakdown(retrieval_confidence_breakdown([])),
        "sources_used": [],
        "notes": None,
    }


async def _call_gemma(query: str, context_blocks: str, retrieval_breakdown: dict) -> dict:
    user_prompt = _USER_TEMPLATE.format(query=query, context_blocks=context_blocks)
    raw = await gemma_client.chat_async(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=settings.GEMMA4_MAX_TOKENS,
        temperature=0.1,
    )
    return _parse_answer(raw, retrieval_breakdown=retrieval_breakdown)


def _parse_answer(raw: str, retrieval_breakdown: dict) -> dict:
    """Turn Gemma's reply into the answer dict, tolerating the model's habits.

    The CDAC Gemma endpoint frequently wraps JSON in markdown fences, appends a
    line of commentary after the closing brace (json.loads → 'Extra data'), or
    just answers in plain prose. We try hard to recover a structured answer, and
    when there's no parseable JSON we keep the prose itself as the answer instead
    of throwing it away and dumping raw chunks at the user.

    Blends the LLM's self-rated confidence with the retrieval signal so that a
    correct answer backed by high-scoring retrieved chunks always scores well,
    regardless of LLM conservatism."""
    retrieval_conf = retrieval_breakdown["score"]
    obj = _extract_first_json_object(raw)
    if obj and str(obj.get("answer", "")).strip():
        llm_conf = _safe_float(obj.get("confidence"), retrieval_conf)
        blended = _blend_confidence(llm_conf, retrieval_conf)
        return {
            "answer": _sanitize_html(str(obj["answer"]).strip()),
            "confidence": blended,
            "confidence_breakdown": _blended_breakdown(llm_conf, retrieval_breakdown),
            "sources_used": obj.get("sources_used") or [],
            "notes": obj.get("notes"),
        }

    # No usable JSON answer — salvage the prose Gemma actually produced.
    prose = _strip_fences(raw).strip()
    if prose:
        # Pull any [n] citation numbers the model included inline.
        cites = sorted({int(n) for n in re.findall(r"\[(\d+)\]", prose)})
        assumed_llm_conf = 0.65  # no self-rating available from plain prose
        blended = _blend_confidence(assumed_llm_conf, retrieval_conf)
        return {
            "answer": _sanitize_html(prose),
            "confidence": blended,
            "confidence_breakdown": _blended_breakdown(assumed_llm_conf, retrieval_breakdown),
            "sources_used": cites,
            "notes": None,
        }

    # Truly empty response — let the caller fall back to raw chunks.
    raise ValueError("Gemma returned an empty response")


def _strip_fences(text: str) -> str:
    return re.sub(r"```(?:json)?", "", text or "").strip().strip("`").strip()


# HTML tags Gemma sometimes generates despite markdown-only instructions.
# Conversion order matters: convert paired tags first, then strip stragglers.
_HTML_PAIRS = [
    (re.compile(r"<u\s*>(.*?)</u\s*>",           re.IGNORECASE | re.DOTALL), r"**\1**"),
    (re.compile(r"<(?:b|strong)\s*>(.*?)</(?:b|strong)\s*>", re.IGNORECASE | re.DOTALL), r"**\1**"),
    (re.compile(r"<(?:i|em)\s*>(.*?)</(?:i|em)\s*>",         re.IGNORECASE | re.DOTALL), r"*\1*"),
]
_HTML_STRAY_TAG = re.compile(r"</?[a-zA-Z][^<>]{0,60}>")


def _sanitize_html(text: str) -> str:
    """Convert inline HTML tags to markdown equivalents; strip anything else."""
    for pattern, replacement in _HTML_PAIRS:
        text = pattern.sub(replacement, text)
    return _HTML_STRAY_TAG.sub("", text)


def _extract_first_json_object(text: str) -> dict:
    """Parse the first JSON object from an LLM reply, tolerating markdown fences
    and trailing prose after the closing brace (raw_decode reads just the first
    value)."""
    cleaned = _strip_fences(text)
    start = cleaned.find("{")
    if start == -1:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_RETRIEVAL_TOP_CHUNK_WEIGHT = 0.6
_RETRIEVAL_TOP3_MEAN_WEIGHT = 0.4
_BLEND_LLM_WEIGHT = 0.4
_BLEND_RETRIEVAL_WEIGHT = 0.6


def retrieval_confidence_breakdown(chunks: list) -> dict:
    """Derive a 0-1 confidence from the reranker relevance scores of the top chunks,
    with every weighted component broken out (for UI display / debugging).

    Strategy: take the MAX relevance score (the best-matching chunk drives trust),
    then blend with the mean of the top-3 to avoid a single fluke chunk inflating
    the score. Clamp to [0, 1]."""
    if not chunks:
        return {
            "top_chunk_score": 0.0,
            "top3_mean_score": 0.0,
            "weights": {"top_chunk": _RETRIEVAL_TOP_CHUNK_WEIGHT, "top3_mean": _RETRIEVAL_TOP3_MEAN_WEIGHT},
            "score": 0.0,
        }
    scores = sorted(
        (getattr(c, "relevance_score", 0.0) for c in chunks),
        reverse=True,
    )
    top_score = scores[0]
    top3_mean = sum(scores[:3]) / min(len(scores), 3)
    raw = _RETRIEVAL_TOP_CHUNK_WEIGHT * top_score + _RETRIEVAL_TOP3_MEAN_WEIGHT * top3_mean
    return {
        "top_chunk_score": round(min(max(top_score, 0.0), 1.0), 4),
        "top3_mean_score": round(min(max(top3_mean, 0.0), 1.0), 4),
        "weights": {"top_chunk": _RETRIEVAL_TOP_CHUNK_WEIGHT, "top3_mean": _RETRIEVAL_TOP3_MEAN_WEIGHT},
        "score": round(min(max(raw, 0.0), 1.0), 4),
    }


def retrieval_confidence(chunks: list) -> float:
    """Thin wrapper over retrieval_confidence_breakdown() for callers that only
    need the final float (unchanged public behavior)."""
    return retrieval_confidence_breakdown(chunks)["score"]


def _blend_confidence(llm_conf: float, retrieval_conf: float) -> float:
    """Blend LLM self-rated confidence (40 %) with retrieval signal (60 %).

    The retrieval signal is more objective (reranker CrossEncoder scores) so we
    weight it higher. Both values are clamped to [0, 1] before blending."""
    llm = min(max(llm_conf, 0.0), 1.0)
    ret = min(max(retrieval_conf, 0.0), 1.0)
    blended = _BLEND_LLM_WEIGHT * llm + _BLEND_RETRIEVAL_WEIGHT * ret
    return round(min(max(blended, 0.0), 1.0), 4)


def _blended_breakdown(llm_conf: float, retrieval_breakdown: dict) -> dict:
    """Full confidence_breakdown dict for paths where a real Gemma self-rating
    exists alongside the retrieval signal (JSON synthesis, or the streaming
    path's post-hoc rating call — see rate_gemma_confidence)."""
    llm = round(min(max(llm_conf, 0.0), 1.0), 4)
    blended = _blend_confidence(llm_conf, retrieval_breakdown["score"])
    return {
        "method": "blended",
        "final": blended,
        "components": [
            {
                "label": "Retrieval confidence",
                "score": retrieval_breakdown["score"],
                "weight": _BLEND_RETRIEVAL_WEIGHT,
                "detail": retrieval_breakdown,
            },
            {
                "label": "Gemma confidence",
                "score": llm,
                "weight": _BLEND_LLM_WEIGHT,
                "detail": None,
            },
        ],
    }


def _retrieval_only_breakdown(retrieval_breakdown: dict, final: float | None = None) -> dict:
    """confidence_breakdown dict for paths with no Gemma self-rating available
    at all (e.g. GEMMA4_BASE_URL unset — see _fallback).

    ``final`` overrides the reported top-level score when the caller applies
    extra adjustment on top of the raw retrieval score (e.g. _fallback's 0.5
    floor) — keeps confidence_breakdown["final"] equal to the "confidence"
    value actually shown to the user."""
    return {
        "method": "retrieval_only",
        "final": retrieval_breakdown["score"] if final is None else round(final, 4),
        "components": [
            {
                "label": "Retrieval confidence",
                "score": retrieval_breakdown["score"],
                "weight": 1.0,
                "detail": retrieval_breakdown,
            },
        ],
    }


_STREAM_USER_TEMPLATE = """Query: {query}

Context:
{context_blocks}

Answer with citations [1], [2]. Use markdown."""


async def synthesize_stream(query: str, chunks: list):
    """Stream answer tokens. Yields raw text deltas; no JSON envelope.
    Falls back to yielding the full fallback text on error."""
    if not chunks:
        yield (
            "No relevant documents found for your query. "
            "Please try a different search or upload relevant documents first."
        )
        return

    await _maybe_compress(query, chunks)
    context_blocks = _build_context(chunks)

    if not (settings.GROQ_BASE_URL or settings.GEMMA4_BASE_URL):
        yield _fallback(chunks, None)["answer"]
        return

    user_prompt = _STREAM_USER_TEMPLATE.format(query=query, context_blocks=context_blocks)
    try:
        async for token in gemma_client.chat_async_stream(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=settings.GROQ_MAX_TOKENS or settings.GEMMA4_MAX_TOKENS or 800,
            temperature=0.1,
        ):
            yield token
    except Exception as e:
        logger.warning("Groq stream failed (%s) — yielding fallback", e)
        yield _fallback(chunks, None)["answer"]


async def rate_gemma_confidence(query: str, answer: str, chunks: list) -> float | None:
    """Post-hoc self-rating call for the streaming path.

    synthesize_stream() yields raw token deltas with no JSON envelope, so
    there's no way to get the model's own confidence for a streamed answer the
    way _parse_answer() does for the non-streaming path. This makes one
    small, fast follow-up call (low max_tokens, temperature=0, no retries —
    it's a nice-to-have, not worth retry latency) asking the LLM to grade the
    already-generated answer against the context. Returns None on any
    failure so the caller falls back to a retrieval-only confidence."""
    if not (settings.GROQ_BASE_URL or settings.GEMMA4_BASE_URL) or not chunks:
        return None
    try:
        context_blocks = _build_context(chunks)
        raw = await gemma_client.chat_async(
            messages=[
                {"role": "system", "content": _RATING_SYSTEM_PROMPT},
                {"role": "user", "content": _RATING_USER_TEMPLATE.format(
                    query=query, context_blocks=context_blocks, answer=answer,
                )},
            ],
            max_tokens=40,
            temperature=0.0,
            retries=0,
        )
    except Exception as e:
        logger.warning("Groq confidence rating call failed (%s)", e)
        return None

    obj = _extract_first_json_object(raw)
    if not obj or "confidence" not in obj:
        return None
    try:
        return float(obj["confidence"])
    except (TypeError, ValueError):
        return None


async def blended_confidence_for_stream(query: str, answer: str, chunks: list) -> dict:
    """confidence + confidence_breakdown for the streaming path.

    Makes the rate_gemma_confidence() follow-up call and blends it with the
    retrieval signal exactly like the non-streaming path (_blend_confidence /
    _blended_breakdown) when it succeeds; falls back to a retrieval-only
    breakdown when the LLM is unreachable or the rating call fails/times out."""
    ret_breakdown = retrieval_confidence_breakdown(chunks)
    llm_conf = await rate_gemma_confidence(query, answer, chunks)
    if llm_conf is None:
        return {
            "confidence": ret_breakdown["score"],
            "confidence_breakdown": _retrieval_only_breakdown(ret_breakdown),
        }
    return {
        "confidence": _blend_confidence(llm_conf, ret_breakdown["score"]),
        "confidence_breakdown": _blended_breakdown(llm_conf, ret_breakdown),
    }


def _fallback(chunks: list, ret_breakdown: dict | None) -> dict:
    parts = []
    for i, chunk in enumerate(chunks[:5], 1):
        preview = chunk.text[:400].rstrip()
        parts.append(f"[{i}] From '{chunk.document_filename}' (p.{chunk.page_number or '?'}):\n{preview}…")

    answer = (
        "The following content was retrieved from your documents:\n\n"
        + "\n\n".join(parts)
        + "\n\n_Note: Groq LLM synthesis is unavailable — showing direct retrieved content._"
    )

    ret_breakdown = ret_breakdown or retrieval_confidence_breakdown(chunks)
    ret_conf = ret_breakdown["score"]
    # Use retrieval confidence directly in fallback (no LLM to blend with)
    conf = round(min(max(ret_conf, 0.5), 1.0), 4) if ret_conf > 0 else 0.5
    return {
        "answer": answer,
        "confidence": conf,
        "confidence_breakdown": _retrieval_only_breakdown(ret_breakdown, final=conf),
        "sources_used": list(range(1, len(parts) + 1)),
        "notes": "LLM synthesis unavailable. Displaying top retrieved chunks directly.",
    }
