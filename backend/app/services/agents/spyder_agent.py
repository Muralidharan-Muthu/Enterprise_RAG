"""
SPYDER — Post-rerank sufficiency judge (corrective/self-RAG).

Inspects the reranked chunks and decides whether they are sufficient to
answer the query. If not, it proposes a reframed query for a follow-up
retrieval pass.

Entry point: judge(query, reranked_chunks) -> dict
Output shape: {
    "sufficient":      bool,
    "confidence":      float,   # 0.0 – 1.0
    "missing":         str,     # short description of what is missing
    "reframed_query":  str | None,  # None when sufficient or no better query
}

All errors → fail-open: sufficient=True so the caller does NOT loop.
"""
import json
import logging
import re
from typing import Optional

from app.config import settings
from app.services import groq_client
from app.services import synthesis_service

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are SPYDER, a retrieval-quality judge for a RAG system. "
    "You will receive a user query and a numbered list of retrieved context chunks. "
    "Decide whether the chunks are sufficient to answer the query fully. "
    "IMPORTANT: if the query asks for specific data (numbers, figures, rates, "
    "amounts, dates, named values) and the chunks only describe a table's "
    "existence, purpose, or methodology WITHOUT the actual values — that is "
    "NOT sufficient, even if a Table source is listed among the chunks. Only "
    "mark sufficient=true when the concrete values themselves are present in "
    "the chunk text. "
    "Respond with ONLY a raw JSON object — no markdown fences, no extra text:\n"
    '{"sufficient": true|false, '
    '"confidence": <float 0-1 reflecting certainty that chunks ARE sufficient>, '
    '"missing": "<one sentence: what key information is absent, or empty string>", '
    '"reframed_query": "<a better query to find missing info, or null if sufficient>"}'
)

_USER_TEMPLATE = """Query: {query}

Retrieved context:
{context_blocks}

Is the context above sufficient to fully answer the query?"""

_FAIL_OPEN: dict = {
    "sufficient": True,
    "confidence": 1.0,
    "missing": "",
    "reframed_query": None,
}


def _parse_spyder(raw: str) -> Optional[dict]:
    """Tolerant JSON parse — strips markdown fences."""
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return None

    if not isinstance(obj, dict):
        return None

    sufficient = bool(obj.get("sufficient", True))
    confidence = float(obj.get("confidence", 1.0))
    confidence = max(0.0, min(1.0, confidence))
    missing = str(obj.get("missing") or "").strip()
    reframed = obj.get("reframed_query")
    if isinstance(reframed, str):
        reframed = reframed.strip() or None

    return {
        "sufficient": sufficient,
        "confidence": confidence,
        "missing": missing,
        "reframed_query": reframed,
    }


async def judge(query: str, reranked_chunks: list) -> dict:
    """Judge whether the reranked chunks sufficiently answer the query.

    Returns fail-open (_FAIL_OPEN) when:
    - SPYDER_ENABLED is False (should not be called, but guard anyway)
    - GROQ_BASE_URL is not configured
    - Groq call or parsing fails for any reason
    """
    if not settings.SPYDER_ENABLED:
        logger.debug("SPYDER disabled — returning sufficient=True")
        return _FAIL_OPEN.copy()

    if not settings.GROQ_BASE_URL:
        logger.debug("GROQ_BASE_URL not set — SPYDER returning fail-open")
        return _FAIL_OPEN.copy()

    if not reranked_chunks:
        # No chunks → not sufficient, but nothing to reframe usefully.
        return {
            "sufficient": False,
            "confidence": 0.0,
            "missing": "No relevant chunks were retrieved.",
            "reframed_query": None,
        }

    try:
        context_blocks = synthesis_service._build_context(reranked_chunks)
        user_prompt = _USER_TEMPLATE.format(query=query, context_blocks=context_blocks)

        raw = await groq_client.chat_async(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=300,
            temperature=0.0,
        )
        parsed = _parse_spyder(raw)
        if parsed:
            logger.info(
                "SPYDER judgement | sufficient=%s confidence=%.2f",
                parsed["sufficient"],
                parsed["confidence"],
            )
            return parsed

        logger.warning("SPYDER: failed to parse LLM output — fail-open")
        return _FAIL_OPEN.copy()

    except Exception as exc:
        logger.warning("SPYDER: error during judgement (%s) — fail-open", exc)
        return _FAIL_OPEN.copy()
