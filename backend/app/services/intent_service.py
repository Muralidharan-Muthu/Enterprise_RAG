"""
Query intent classifier — decides which stores likely hold the answer so
retrieval can route to them (precision + speed) while staying recall-safe:
ambiguous queries fall back to searching all stores.

classify_intent() escalates through three tiers, from cheapest to most
expensive, and stops at the first one that produces a confident answer:

  1. Rule-based keywords (_rule_based_intent) — instant, no model call. Wins
     outright on an unambiguous single-category keyword hit.
  2. Semantic router (semantic_router.classify) — cosine similarity against
     per-store prototype embeddings, using the query embedding retrieve()
     already computed. No extra model call or network round-trip; catches
     paraphrases the keyword list misses. Gated by INTENT_USE_SEMANTIC_ROUTER
     (default True) and only usable when the caller passes query_embedding.
  3. Groq (groq_client) — a full LLM round-trip. Gated by INTENT_USE_LLM
     (default False) since it adds real latency to the query's critical path;
     reserved for cases the first two tiers couldn't resolve confidently.

Whichever tier wins, the result is only *trusted* by the caller
(retriever_service._select_stores) above INTENT_CONFIDENCE_THRESHOLD — below
that, the caller searches all stores regardless of what's returned here.
"""
import json
import logging
import re

from app.config import settings
from app.services import groq_client

logger = logging.getLogger(__name__)

# A rule-based match at/above this confidence is an unambiguous single-category
# keyword hit (see _rule_based_intent) — cheap and reliable enough to skip the
# semantic/LLM tiers entirely.
RULE_HIGH_CONFIDENCE = 0.8

# image_store is NOT a searchable store (migration 008 — pure repository, no
# embedding). Image-derived searchable content lives in the destination stores
# (table/vector/clause/document), so visual queries route there.
ALL_STORES = ["vector", "clause", "table"]

# keyword → content category. Each category maps to its MINIMAL, content-precise
# store set so a clearly single-content query routes to one store (precision)
# rather than always spilling into vector.
_TABLE_KW = ("table", "tabular", "spreadsheet", "row of", "column", "cell value",
             "line item", "line items")
_TABLE_ATTRIBUTE_KW = ("sector", "industry", "segment", "classification",
                       "classified as", "belongs to")
_LEGAL_KW = ("clause", "contract", "agreement", "liability", "indemnif", "termination",
             "obligation", "governing law", "confidential", "warrant", "dispute", "breach")
_FINANCIAL_KW = ("revenue", "profit", "ebitda", "fiscal", "quarter", "balance sheet",
                 "cash flow", "margin", "financial", "earnings", "income statement")
_VISUAL_KW = ("chart", "graph", "figure", "image", "diagram", "plot", "visual", "picture",
              "screenshot", "infographic")
_POLICY_KW = ("policy", "procedure", "sop", "guideline", "compliance", "governance",
              "standard operating", "handbook")

SYSTEM_PROMPT = (
    "You classify a search query to route it to the right document stores in a "
    "RAG system. Stores: vector (policy/general text), clause (legal/contract), "
    "table (financial/numeric tables, charts and figures with data). Respond with ONLY JSON: "
    '{"stores": ["..."], "doc_types": ["policy|financial|legal|entity"] or null, '
    '"confidence": 0..1, "reasoning": "..."}. Return the MINIMAL set of stores that '
    "clearly match the query — if it asks about a table, numbers, a chart or a figure, "
    'return just ["table"]; about a contract clause, just ["clause"]. Use high '
    "confidence (>=0.8) for an unambiguous single-content query. Only when genuinely "
    "unsure, return all stores with low confidence (<0.5)."
)


def _rule_based_intent(query: str) -> dict:
    q = query.lower()

    def hit(kws):
        return any(k in q for k in kws)

    # category -> minimal store set
    matched: dict[str, set[str]] = {}
    doc_types: set[str] = set()

    if hit(_TABLE_KW):
        matched["table"] = {"table"}
    if hit(_TABLE_ATTRIBUTE_KW):
        matched["table"] = {"table"}
    if hit(_FINANCIAL_KW):
        matched["financial"] = {"table", "vector"}; doc_types.add("financial")
    if hit(_LEGAL_KW):
        matched["legal"] = {"clause"}; doc_types.add("legal")
    if hit(_VISUAL_KW):
        # Charts/figures with real data were cross-stored to table_store; any
        # descriptive text lives in vector_store. image_store is not searchable.
        matched["visual"] = {"table", "vector"}
    if hit(_POLICY_KW):
        matched["policy"] = {"vector"}; doc_types.add("policy")

    if not matched:
        # Ambiguous — recall-safe: search everything, low confidence.
        return {"stores": list(ALL_STORES), "doc_types": None, "confidence": 0.4,
                "used_fallback": True}

    stores = set().union(*matched.values())
    # One unambiguous content signal → high confidence + minimal stores; multiple
    # signals → union with medium confidence (still narrower than "all").
    confidence = 0.85 if len(matched) == 1 else 0.65
    return {
        "stores": sorted(stores),
        "doc_types": sorted(doc_types) or None,
        "confidence": confidence,
        "used_fallback": True,
    }


def _parse_intent(raw: str) -> dict | None:
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
    stores = [s for s in obj.get("stores", []) if s in ALL_STORES]
    if not stores:
        return None
    dts = obj.get("doc_types") or None
    return {
        "stores": stores,
        "doc_types": dts,
        "confidence": float(obj.get("confidence", 0.5)),
        "used_fallback": False,
    }


def classify_intent(query: str, query_embedding=None) -> dict:
    """Return {stores, doc_types, confidence, used_fallback}. Recall-safe.

    query_embedding (optional): the L2-normalized query vector retrieve()
    already computed for the vector search itself. Passing it in lets tier 2
    (semantic_router) classify for free — no extra embedding call. Omitting it
    (e.g. from callers outside the retrieval path) just skips straight from
    rules to the LLM tier, matching the pre-existing behavior.
    """
    rule_intent = _rule_based_intent(query)
    if rule_intent["confidence"] >= RULE_HIGH_CONFIDENCE:
        return rule_intent

    if settings.INTENT_USE_SEMANTIC_ROUTER and query_embedding is not None:
        try:
            from app.services import semantic_router
            semantic_intent = semantic_router.classify(query_embedding)
            if semantic_intent["confidence"] > rule_intent["confidence"]:
                return semantic_intent
        except Exception as exc:
            logger.warning("Semantic router failed (%s) — continuing to next tier", exc)

    if not settings.INTENT_USE_LLM or not (settings.GROQ_BASE_URL or settings.GROQ_BASE_URL):
        return rule_intent

    try:
        content = groq_client.chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            max_tokens=256,
            temperature=0.0,
            model=settings.GROQ_ROUTING_MODEL,
        )
        parsed = _parse_intent(content)
        if parsed:
            return parsed
        logger.warning("Intent parse failed — using rule-based fallback")
    except Exception as exc:
        logger.warning("Intent classification failed (%s) — using rule-based fallback", exc)

    return rule_intent
