"""
Graph extraction service — Feature 1.3 GraphRAG.

Per-chunk Gemma NER + relationship extraction in a single call.
Returns {entities:[{name,type,description}], relationships:[{source,target,type,description}]}.

Tolerant JSON parsing mirrors entity_service. Falls back to entity_service.extract_entities
+ empty relationships on any failure. NEVER raises.
"""
import json
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

_GRAPH_SYSTEM_PROMPT = """You are a knowledge-graph extractor. From the user text, extract:
1. Named entities (organizations, people, locations, products, laws/standards, projects, concepts).
2. Typed relationships between pairs of extracted entities.

Respond with ONLY valid JSON, no markdown fences:
{
  "entities": [
    {"name": "<exact surface form>", "type": "org|person|location|product|law|project|concept|misc", "description": "<one sentence>", "confidence": <0.0-1.0>}
  ],
  "relationships": [
    {"source": "<entity name>", "target": "<entity name>", "type": "<verb phrase e.g. REGULATES|OWNS|PARTNERS_WITH|CONTAINS|REFERENCES|ISSUED_BY>", "description": "<one sentence>", "confidence": <0.0-1.0>}
  ]
}
confidence is your certainty the entity/relationship is genuinely grounded in the text (1.0 = explicit, lower = inferred). It is optional; omit if unsure.
Return at most %d entities and %d relationships, most salient first. Omit entities/relationships with no clear grounding in the text."""


def extract_graph_elements(
    text: str,
    max_entities: int = 15,
    max_relationships: int = 20,
) -> dict:
    """Extract entities + relationships from `text` in a single Gemma call.

    Returns:
        {
            "entities": [{"name", "type", "description"}, ...],
            "relationships": [{"source", "target", "type", "description"}, ...],
        }

    Never raises. On any failure falls back to entity_service.extract_entities
    for entities and returns empty relationships.
    """
    if not text or not text.strip():
        return {"entities": [], "relationships": []}

    if settings.GEMMA4_BASE_URL:
        try:
            result = _call_gemma_graph(text, max_entities, max_relationships)
            if result is not None:
                return result
            logger.warning("Graph element extraction parse failed — using fallback")
        except Exception as exc:
            logger.warning("Graph element extraction failed (%s) — using fallback", exc)

    # Fallback: use entity_service for entities, no relationships
    try:
        from app.services.entity_service import extract_entities
        entities_raw = extract_entities(text, max_entities=max_entities)
        entities = [
            {"name": e["name"], "type": e.get("type", "misc"), "description": ""}
            for e in entities_raw
        ]
        return {"entities": entities, "relationships": []}
    except Exception as exc:
        logger.warning("Fallback entity extraction failed (%s)", exc)
        return {"entities": [], "relationships": []}


def _call_gemma_graph(text: str, max_entities: int, max_relationships: int) -> dict | None:
    """Call Groq LLM for graph extraction. Returns parsed dict or None on parse failure."""
    from app.services.gemma_client import chat

    system_prompt = _GRAPH_SYSTEM_PROMPT % (max_entities, max_relationships)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:6000]},
    ]
    raw = chat(
        messages=messages,
        max_tokens=768,
        temperature=0.0,
        model=settings.GROQ_EXTRACTION_MODEL,
    )
    return _parse_graph_response(raw)


def _parse_graph_response(raw: str) -> dict | None:
    """Tolerant JSON parse for graph extraction response. Returns None on bad JSON."""
    if not raw:
        return None

    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    obj = None
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

    raw_entities = obj.get("entities") if isinstance(obj, dict) else []
    raw_rels = obj.get("relationships") if isinstance(obj, dict) else []

    if not isinstance(raw_entities, list):
        raw_entities = []
    if not isinstance(raw_rels, list):
        raw_rels = []

    # Deduplicate entities by canonical name
    from app.services.entity_service import canonicalize
    seen_ent: set[str] = set()
    entities: list[dict] = []
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        key = canonicalize(name)
        if not key or key in seen_ent:
            continue
        seen_ent.add(key)
        entities.append({
            "name": name,
            "type": str(e.get("type", "misc")) or "misc",
            "description": str(e.get("description", "")).strip(),
            "confidence": _coerce_confidence(e.get("confidence")),
        })

    # Parse relationships — only keep those whose source/target we recognised
    entity_names_lower = {canonicalize(e["name"]) for e in entities}
    relationships: list[dict] = []
    for r in raw_rels:
        if not isinstance(r, dict):
            continue
        src = str(r.get("source", "")).strip()
        tgt = str(r.get("target", "")).strip()
        rel_type = str(r.get("type", "RELATES_TO")).strip().upper().replace(" ", "_") or "RELATES_TO"
        desc = str(r.get("description", "")).strip()
        if not src or not tgt:
            continue
        # Allow even if not strictly in entity list (Gemma may use slightly different surface form)
        relationships.append({
            "source": src, "target": tgt, "type": rel_type, "description": desc,
            "confidence": _coerce_confidence(r.get("confidence")),
        })

    return {"entities": entities, "relationships": relationships}


def _coerce_confidence(raw) -> float | None:
    """Clamp an optional extraction confidence into [0,1]; None when absent/bad."""
    if raw is None:
        return None
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return None
