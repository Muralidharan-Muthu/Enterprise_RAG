"""
Entity extraction for the multi-PDF graph (Phase 4).

`extract_entities` pulls named entities (orgs, people, places, products, …)
from document/query text via Gemma-4 NER, with a rule-based fallback when the
endpoint is unavailable or errors. `canonicalize` produces the stable
case-insensitive key used to MERGE Entity nodes in Neo4j, so "Acme Corp",
"ACME CORP", and "acme  corp" all map to one node.
"""
import json
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTITIES = 20

# Capitalized tokens that are structural / sentence-starters, not entities.
_STOPLIST = {
    "the", "this", "that", "these", "those", "a", "an", "and", "or", "but",
    "section", "page", "figure", "table", "chapter", "article", "clause",
    "appendix", "exhibit", "schedule", "annex", "introduction", "scope",
    "overview", "summary", "conclusion", "abstract", "note", "notes",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "company", "agreement", "policy", "document", "report", "we", "i", "it",
    "he", "she", "they", "you", "however", "therefore", "moreover", "furthermore",
}

# A capitalized word, optionally with internal &/./- (e.g. "AT&T", "U.S.").
_WORD = r"[A-Z][A-Za-z0-9&.\-']*"
# 1–6 capitalized words in sequence ("Acme Corporation", "New York"). Bounded so
# a run of title-case text (a heading) isn't swallowed as one giant entity.
_CAP_SEQUENCE = re.compile(rf"\b{_WORD}(?:\s+{_WORD}){{0,5}}\b")

_SYSTEM_PROMPT = (
    "You are a named-entity recognizer. Extract the distinct named entities "
    "(organizations, people, locations, products, laws/standards, projects) "
    "from the user text. Ignore generic words and section headers. "
    "Respond with ONLY JSON, no markdown: "
    '{"entities": [{"name": "<exact surface form>", "type": '
    '"org|person|location|product|law|project|misc"}]}. '
    "Return at most %d entities, most salient first."
)


def canonicalize(name: str) -> str:
    """Stable MERGE key: trim, collapse internal whitespace, lowercase."""
    return re.sub(r"\s+", " ", name or "").strip().lower()


def extract_entities(text: str, max_entities: int = DEFAULT_MAX_ENTITIES) -> list[dict]:
    """Return up to `max_entities` [{name, type}] from `text`.
    Gemma NER when configured; rule-based fallback otherwise. Never raises."""
    if not text or not text.strip():
        return []

    if settings.GROQ_BASE_URL:
        try:
            parsed = _call_groq_ner(text, max_entities)
            # `is not None` (not truthiness) matters here: _parse_groq_entities
            # returns [] when Groq *correctly* found zero entities in the text —
            # that's a valid answer, not a failure, and must not fall through to
            # the regex fallback (which would invent pseudo-entities from any
            # capitalized phrase, e.g. table headers or fiscal periods like
            # "FY 2023-24"). Only a genuine parse failure returns None.
            if parsed is not None:
                return parsed[:max_entities]
            logger.warning("Entity NER parse failed — using rule-based fallback")
        except Exception as exc:
            logger.warning("Entity NER failed (%s) — using rule-based fallback", exc)

    return _rule_based_entities(text, max_entities)


def _rule_based_entities(text: str, max_entities: int) -> list[dict]:
    """Heuristic: capitalized multiword sequences, stoplist-filtered, deduped
    by canonical form, preserving first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for m in _CAP_SEQUENCE.finditer(text):
        surface = m.group().strip(" .")
        if not surface:
            continue
        words = surface.split()
        # Drop single bare stoplist words ("The", "Section"); keep multiword
        # phrases that merely start with one ("United States").
        if len(words) == 1 and words[0].lower() in _STOPLIST:
            continue
        key = canonicalize(surface)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": surface, "type": "unknown"})
        if len(out) >= max_entities:
            break
    return out


def _call_groq_ner(text: str, max_entities: int) -> list[dict] | None:
    base = settings.GROQ_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.GROQ_API_KEY:
        headers["Authorization"] = f"Bearer {settings.GROQ_API_KEY}"
    payload = {
        "model": settings.GROQ_EXTRACTION_MODEL or settings.GROQ_MODEL_NAME,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT % max_entities},
            {"role": "user", "content": text[:6000]},
        ],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    with httpx.Client(timeout=settings.GROQ_TIMEOUT_SECONDS) as client:
        resp = client.post(f"{base}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_groq_entities(content)


# Backward-compat aliases
_call_gemma_ner = _call_groq_ner


def _parse_groq_entities(raw: str) -> list[dict] | None:
    """Parse the NER JSON; dedup by canonical name, skip blanks. None on bad JSON."""
    cleaned = re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()
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

    raw_entities = obj.get("entities") if isinstance(obj, dict) else None
    if not isinstance(raw_entities, list):
        return None

    seen: set[str] = set()
    out: list[dict] = []
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        key = canonicalize(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "type": str(e.get("type", "misc")) or "misc"})
    return out
