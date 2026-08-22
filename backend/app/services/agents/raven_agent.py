"""
RAVEN — Pre-retrieval query reframing agent.

Reframes the user's raw query into a cleaner form, optionally decomposes it
into sub-queries for fan-out retrieval, and emits a store_hint that matches
the intent dict shape retriever_service.retrieve(intent=...) already consumes.

Entry point: reframe(query) -> dict
Output shape: {
    "reframed":     str,          # cleaned/expanded query
    "sub_queries":  list[str],    # decomposed sub-questions (may be empty)
    "store_hint":   dict | None,  # intent-compatible dict or None
    "used_fallback": bool,        # True when LLM was skipped or failed
}

All errors → fail-open: returns the original query with empty sub_queries.
Feature-flagged: when RAVEN_ENABLED=False the fallback shape is returned
immediately without calling Groq.
"""
import json
import logging
import re
from typing import Optional

from app.config import settings
from app.services import groq_client

logger = logging.getLogger(__name__)

ALL_STORES = ["vector", "clause", "table", "image"]

_SYSTEM_PROMPT = (
    "You are RAVEN, a query pre-processor for a multi-store RAG system. "
    "Given a user query, produce a JSON object with these fields:\n"
    "  reframed   : a clearer, search-optimised restatement of the query\n"
    "  sub_queries: list of 0-3 focused sub-questions (empty list if not needed)\n"
    "  store_hint : null, or an object with:\n"
    "               stores: list from [vector, clause, table, image]\n"
    "               doc_types: list from [policy, financial, legal, entity] or null\n"
    "               confidence: float 0-1\n"
    "Respond with ONLY the raw JSON object — no markdown fences, no extra text.\n"
    "Stores: vector=policy/general text, clause=legal/contract, "
    "table=financial/numeric tables, image=figures/charts."
)

_FALLBACK_SHAPE = {
    "reframed": "",
    "sub_queries": [],
    "store_hint": None,
    "used_fallback": True,
}


def _parse_raven(raw: str) -> Optional[dict]:
    """Tolerant JSON parse — strips markdown fences and finds first JSON object."""
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

    reframed = str(obj.get("reframed", "")).strip()
    if not reframed:
        return None

    sub_queries = [s for s in (obj.get("sub_queries") or []) if isinstance(s, str) and s.strip()]

    raw_hint = obj.get("store_hint")
    store_hint: Optional[dict] = None
    if isinstance(raw_hint, dict):
        stores = [s for s in (raw_hint.get("stores") or []) if s in ALL_STORES]
        if stores:
            store_hint = {
                "stores": stores,
                "doc_types": raw_hint.get("doc_types") or None,
                "confidence": float(raw_hint.get("confidence", 0.5)),
                "used_fallback": False,
            }

    return {
        "reframed": reframed,
        "sub_queries": sub_queries[:3],  # cap at 3 sub-queries
        "store_hint": store_hint,
        "used_fallback": False,
    }


async def reframe(query: str) -> dict:
    """Reframe and optionally decompose the user query.

    Returns the fallback shape (reframed=query, sub_queries=[], store_hint=None,
    used_fallback=True) when:
    - RAVEN_ENABLED is False
    - GROQ_BASE_URL is not configured
    - Groq call fails for any reason
    - LLM output cannot be parsed
    """
    fallback = {**_FALLBACK_SHAPE, "reframed": query}

    if not settings.RAVEN_ENABLED:
        logger.debug("RAVEN disabled — returning raw query")
        return fallback

    if not settings.GROQ_BASE_URL:
        logger.debug("GROQ_BASE_URL not set — RAVEN returning raw query")
        return fallback

    try:
        raw = await groq_client.chat_async(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            max_tokens=256,
            temperature=0.0,
            model=settings.GROQ_RAVEN_MODEL,
        )
        parsed = _parse_raven(raw)
        if parsed:
            logger.info(
                "RAVEN reframed query | sub_queries=%d store_hint=%s",
                len(parsed["sub_queries"]),
                parsed["store_hint"] is not None,
            )
            return parsed

        logger.warning("RAVEN: failed to parse LLM output — using fallback")
        return fallback

    except Exception as exc:
        logger.warning("RAVEN: error during reframe (%s) — using fallback", exc)
        return fallback
