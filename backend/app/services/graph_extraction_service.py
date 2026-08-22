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

_GRAPH_SYSTEM_PROMPT = """You are an expert enterprise Knowledge Graph extractor. From the provided document text, extract:
1. Highly important named entities:
   - "organization": companies, subsidiaries, regulatory authorities, financial institutions, vendors, partners.
   - "person": corporate executives, directors, designated signatories, key personnel.
   - "legal_clause": contractual rules, termination provisions, governing laws, indemnities, dispute resolution clauses.
   - "financial_metric": key figures, revenues, EBITDA, margins, penalties, capital expenditures, fees.
   - "location": cities, states, legal jurisdictions, countries, facilities.
   - "product": platforms, technologies, brands, hardware, software, services.
   - "policy": governance frameworks, compliance mandates, standards, codes of conduct.

2. Explicit, typed domain relationships connecting pairs of extracted entities (e.g. OWNS, OPERATES, TERMINATES, SUBJECT_TO, BOUND_BY, SUPPLIES, PARTNERS_WITH, OBLIGATED_TO, RESOLVES_DISPUTES_IN, REGULATES, REPORTS_TO, PAYS_FEE_TO, AMENDS, CONTAINS).

Respond with ONLY valid JSON without markdown code blocks:
{
  "entities": [
    {"name": "<exact entity name>", "type": "organization|person|legal_clause|financial_metric|location|product|policy|misc", "description": "<concise one-sentence role or context>", "confidence": 0.95}
  ],
  "relationships": [
    {"source": "<exact source entity name>", "target": "<exact target entity name>", "type": "<PRECISE_RELATIONSHIP_VERB>", "description": "<concise explanation of how source relates to target>", "confidence": 0.90}
  ]
}
Return at most %d key entities and %d meaningful relationships, sorted by importance and grounded directly in the text."""


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

    if settings.GROQ_BASE_URL:
        try:
            result = _call_groq_graph(text, max_entities, max_relationships)
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


def _call_groq_graph(text: str, max_entities: int, max_relationships: int) -> dict | None:
    """Call Groq LLM for graph extraction. Returns parsed dict or None on parse failure."""
    from app.services.groq_client import chat

    system_prompt = _GRAPH_SYSTEM_PROMPT % (max_entities, max_relationships)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text[:6000]},
    ]
    raw = chat(
        messages=messages,
        max_tokens=2500,
        temperature=0.0,
        model=settings.GROQ_EXTRACTION_MODEL,
    )
    return _parse_graph_response(raw)


# Backward-compat alias
_call_gemma_graph = _call_groq_graph

_JUNK_ENTITIES = {
    "table of contents", "table of content", "table of co", "table of", "prepared",
    "strategic review", "overview", "introduction", "conclusion", "annexure",
    "appendix", "section", "chapter", "schedule", "page", "note", "notes",
}


def _parse_graph_response(raw: str) -> dict | None:
    """Tolerant JSON parse for graph extraction response with auto-repair and regex fallback."""
    if not raw:
        return None

    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    obj = None
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try finding outer JSON block
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group())
            except json.JSONDecodeError:
                pass

        # Try repairing truncated JSON by closing arrays and braces
        if obj is None:
            for suffix in ["]}", "}", "\"]}", "}\n]}"]:
                # Find last complete object closing
                last_brace = cleaned.rfind("}")
                if last_brace != -1:
                    candidate = cleaned[:last_brace + 1]
                    # Count open brackets vs closing brackets
                    open_sq = candidate.count("[") - candidate.count("]")
                    open_cu = candidate.count("{") - candidate.count("}")
                    repaired = candidate + ("]" * max(0, open_sq)) + ("}" * max(0, open_cu))
                    try:
                        obj = json.loads(repaired)
                        if isinstance(obj, dict):
                            break
                    except json.JSONDecodeError:
                        pass

    raw_entities = obj.get("entities", []) if isinstance(obj, dict) else []
    raw_rels = obj.get("relationships", []) if isinstance(obj, dict) else []

    # If top-level dict parsing completely failed, use regex recovery for objects
    if not raw_entities and not raw_rels:
        # Entity recovery regex
        ent_matches = re.finditer(
            r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"([^"]+)"(?:[^\}]*?"description"\s*:\s*"([^"]*)")?',
            cleaned,
        )
        for em in ent_matches:
            raw_entities.append({
                "name": em.group(1),
                "type": em.group(2),
                "description": em.group(3) or "",
            })

        # Relationship recovery regex
        rel_matches = re.finditer(
            r'\{\s*"source"\s*:\s*"([^"]+)"\s*,\s*"target"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"([^"]+)"(?:[^\}]*?"description"\s*:\s*"([^"]*)")?',
            cleaned,
        )
        for rm in rel_matches:
            raw_rels.append({
                "source": rm.group(1),
                "target": rm.group(2),
                "type": rm.group(3),
                "description": rm.group(4) or "",
            })

    if not isinstance(raw_entities, list):
        raw_entities = []
    if not isinstance(raw_rels, list):
        raw_rels = []

    # Deduplicate and filter entities
    from app.services.entity_service import canonicalize
    seen_ent: set[str] = set()
    entities: list[dict] = []
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name or len(name) < 2:
            continue
        key = canonicalize(name)
        if not key or key in seen_ent or key in _JUNK_ENTITIES:
            continue
        # Drop leading numbers or symbols
        clean_name = re.sub(r"^[\d\.\-\)\(\s]+", "", name).strip()
        if not clean_name:
            continue
        seen_ent.add(key)
        entities.append({
            "name": clean_name,
            "type": str(e.get("type", "misc")) or "misc",
            "description": str(e.get("description", "")).strip(),
            "confidence": _coerce_confidence(e.get("confidence")),
        })

    # Parse and validate relationships
    relationships: list[dict] = []
    seen_rels: set[tuple[str, str, str]] = set()
    for r in raw_rels:
        if not isinstance(r, dict):
            continue
        src = str(r.get("source", "")).strip()
        tgt = str(r.get("target", "")).strip()
        if not src or not tgt or src.lower() == tgt.lower():
            continue
        src_key = canonicalize(src)
        tgt_key = canonicalize(tgt)
        if src_key in _JUNK_ENTITIES or tgt_key in _JUNK_ENTITIES:
            continue
        rel_type = str(r.get("type", "RELATES_TO")).strip().upper().replace(" ", "_") or "RELATES_TO"
        desc = str(r.get("description", "")).strip()
        
        rel_sig = (src_key, tgt_key, rel_type)
        if rel_sig in seen_rels:
            continue
        seen_rels.add(rel_sig)

        relationships.append({
            "source": src,
            "target": tgt,
            "type": rel_type,
            "description": desc,
            "confidence": _coerce_confidence(r.get("confidence")),
        })

    if not entities and not relationships:
        return None

    return {"entities": entities, "relationships": relationships}


def _coerce_confidence(raw) -> float | None:
    """Clamp an optional extraction confidence into [0,1]; None when absent/bad."""
    if raw is None:
        return None
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return None
