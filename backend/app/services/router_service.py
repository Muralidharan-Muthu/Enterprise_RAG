"""
Groq document router — classifies a parsed document across 4 core business
categories ('financial', 'legal', 'entity', 'policy') using full-document
chunk/section analysis. Supports multi-type classification (e.g., a document
containing both financial tables and legal agreements is categorized as both).
Falls back to a full-text rule-based classifier if Groq is unreachable.
"""
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed

from app.config import settings
from app.models.document import ParsedDocument

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """Process-wide pooled sync client, reused across calls instead of opening
    a new connection per classification. Thread-safe double-checked init."""
    global _client
    if _client is None or _client.is_closed:
        with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.Client(timeout=settings.GROQ_TIMEOUT_SECONDS)
    return _client


DOCUMENT_TYPES = ("financial", "legal", "entity", "policy")

SYSTEM_PROMPT = """You are an Enterprise Document Intelligence specialist.
Your job is to analyze the FULL content of a document and classify it into ONE OR MORE
of the 4 enterprise categories:
- "financial": Financial reports, balance sheets, invoices, budgets, P&L statements, quarterly/annual results, cash flows
- "legal": Contracts, NDAs, Master Service Agreements (MSAs), terms of service, legal notices, compliance clauses
- "entity": Corporate hierarchy, org charts, subsidiary structures, company directories, entity relationship maps
- "policy": Standard operating procedures (SOPs), company policies, operational manuals, HR guidelines, compliance handbooks

IMPORTANT RULES:
1. Documents often contain multiple facets (for example: an annual report or acquisition filing contains both "financial" data and "legal" agreements). In such cases, include ALL matching categories in "document_types".
2. If the document fits a single category, return a list with that single category.
3. Extract accurate metadata (title, author, date, summary) from the full text.

Respond ONLY with valid JSON. No markdown or explanation outside the JSON."""

USER_PROMPT_TEMPLATE = """Analyze the complete document content and classify its categories.

Filename: {filename}
Page count: {page_count}
Detected tables: {table_count}

--- FULL EXTRACTED CONTENT / SECTION BREAKDOWN ---
{full_content}
--- END OF CONTENT ---

Respond with this exact JSON structure:
{{
  "document_types": ["<one or more of: financial, legal, entity, policy>"],
  "document_subtype": "<specific subtype, e.g. Annual_Report, NDA, MSA_with_Financials, SOP, Org_Chart>",
  "confidence": <0.0 to 1.0>,
  "reasoning": "<2-3 sentence explanation detailing why each selected category applies based on specific sections>",
  "doc_title": "<extracted or inferred title>",
  "doc_author": "<extracted author name, organisation, or null>",
  "doc_date": "<YYYY-MM-DD or null>",
  "doc_summary": "<concise 100-word summary of the entire document>",
  "language": "en"
}}"""


@dataclass
class RouterResult:
    document_type: str  # Primary or comma-joined string: e.g. "financial, legal"
    document_types: list[str] = field(default_factory=list)  # e.g. ["financial", "legal"]
    document_subtype: Optional[str] = None
    confidence: float = 0.5
    reasoning: str = ""
    doc_title: Optional[str] = None
    doc_author: Optional[str] = None
    doc_date: Optional[str] = None
    doc_summary: Optional[str] = None
    language: str = "en"
    used_fallback: bool = False

    def has_type(self, type_name: str) -> bool:
        """Check if a specific document type was identified."""
        return type_name in self.document_types or type_name == self.document_type


def _build_full_document_representation(parsed_doc: ParsedDocument, max_chars: int = 50000) -> str:
    """
    Build a comprehensive representation of the full document without missing sections.
    Includes section titles, table structures, and text blocks across all pages.
    """
    lines: list[str] = []

    # Include table structure overview if present
    if parsed_doc.tables:
        lines.append(f"[Document contains {len(parsed_doc.tables)} structured tables]")
        for t in parsed_doc.tables[:10]:
            headers = ", ".join(t.headers[:8]) if t.headers else "No headers"
            table_name = getattr(t, "caption", None) or f"Table {getattr(t, 'table_index', 0)}"
            lines.append(f"- Table on Page {t.page_number} ({table_name}): Headers = [{headers}], Rows = {len(t.rows)}")
        lines.append("")

    # If raw text is within budget, include full raw text directly
    if len(parsed_doc.raw_text) <= max_chars:
        lines.append(parsed_doc.raw_text)
    else:
        # For very large documents, construct a representative map across all pages/sections
        # so every single section of the document is covered.
        head_len = max_chars // 3
        tail_len = max_chars // 3
        mid_len = max_chars - head_len - tail_len

        mid_start = (len(parsed_doc.raw_text) - mid_len) // 2

        lines.append("[Beginning of document]")
        lines.append(parsed_doc.raw_text[:head_len])
        lines.append("\n[... Middle sections of document ...]\n")
        lines.append(parsed_doc.raw_text[mid_start : mid_start + mid_len])
        lines.append("\n[... Final sections and closing terms of document ...]\n")
        lines.append(parsed_doc.raw_text[-tail_len:])

    return "\n".join(lines)


def classify_document(parsed_doc: ParsedDocument) -> RouterResult:
    """
    Classify the document categories using Groq LLM with full-document analysis.
    Supports multi-type classification (e.g. ['financial', 'legal']).
    Falls back to full-text rule-based classification if Groq is unavailable.
    """
    full_content = _build_full_document_representation(parsed_doc)
    prompt = USER_PROMPT_TEMPLATE.format(
        filename=parsed_doc.filename,
        full_content=full_content,
        table_count=len(parsed_doc.tables),
        page_count=parsed_doc.page_count,
    )

    if settings.GROQ_BASE_URL:
        try:
            raw_json = _call_groq(prompt)
            result = _parse_groq_response(raw_json)
            if result and result.confidence >= 0.5:
                logger.info(
                    "Groq classified '%s' as %s (confidence=%.2f)",
                    parsed_doc.filename,
                    result.document_types,
                    result.confidence,
                )
                return result
            logger.warning("Groq returned low confidence (%.2f), using fallback", result.confidence if result else 0)
        except Exception as exc:
            logger.warning("Groq call failed: %s — using rule-based fallback", exc)
    else:
        logger.info("GROQ_BASE_URL not configured — using rule-based fallback")

    return _rule_based_classify(parsed_doc)


@retry(stop=stop_after_attempt(2), wait=wait_fixed(2))
def _call_groq(user_prompt: str) -> str:
    """HTTP call to Groq endpoint."""
    base_url = settings.GROQ_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.GROQ_API_KEY:
        headers["Authorization"] = f"Bearer {settings.GROQ_API_KEY}"

    payload = {
        "model": settings.GROQ_ROUTING_MODEL or settings.GROQ_MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": settings.GROQ_MAX_TOKENS,
        "temperature": 0.1,
    }

    client = _get_client()
    response = client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()

    return data["choices"][0]["message"]["content"]


# Backward-compat alias
_call_gemma = _call_groq


def _parse_groq_response(raw: str) -> Optional[RouterResult]:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            obj = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    # Handle multi-type list or single type string
    raw_types = obj.get("document_types") or obj.get("document_type") or []
    if isinstance(raw_types, str):
        # Could be comma-separated or single string
        type_list = [t.strip().lower() for t in raw_types.split(",") if t.strip()]
    elif isinstance(raw_types, list):
        type_list = [str(t).strip().lower() for t in raw_types if str(t).strip()]
    else:
        type_list = []

    # Filter to valid core document types (strictly 4 types)
    valid_types = [t for t in type_list if t in DOCUMENT_TYPES]
    if not valid_types:
        valid_types = ["policy"]

    primary_type = ", ".join(valid_types)

    return RouterResult(
        document_type=primary_type,
        document_types=valid_types,
        document_subtype=obj.get("document_subtype"),
        confidence=float(obj.get("confidence", 0.5)),
        reasoning=obj.get("reasoning", ""),
        doc_title=obj.get("doc_title"),
        doc_author=obj.get("doc_author"),
        doc_date=obj.get("doc_date"),
        doc_summary=obj.get("doc_summary"),
        language=obj.get("language", "en"),
    )


def _rule_based_classify(parsed_doc: ParsedDocument) -> RouterResult:
    """
    Rule-based multi-type classifier analyzing full document text, section titles, and tables.
    """
    full_text = (parsed_doc.filename + " " + parsed_doc.raw_text).lower()

    scores: dict[str, int] = {t: 0 for t in DOCUMENT_TYPES}

    # Financial signals
    for kw in ["revenue", "profit", "loss", "balance sheet", "income", "expense",
               "cash flow", "quarterly", "annual report", "fiscal", "ebitda",
               "invoice", "budget", "financial", "p&l", "dividend", "shares", "operating profit"]:
        if kw in full_text:
            scores["financial"] += 1

    # Legal signals
    for kw in ["agreement", "contract", "clause", "whereas", "party", "parties",
               "indemnif", "liability", "termination", "governing law", "nda",
               "confidential", "breach", "obligation", "arbitration", "jurisdiction", "warranty"]:
        if kw in full_text:
            scores["legal"] += 1

    # Entity signals
    for kw in ["organization chart", "org chart", "subsidiary", "hierarchy",
               "relationship", "entity", "parent company", "division", "board of directors", "affiliates"]:
        if kw in full_text:
            scores["entity"] += 1

    # Policy signals
    for kw in ["policy", "procedure", "sop", "guideline", "standard", "manual",
               "process", "faq", "regulation", "compliance", "rule", "code of conduct"]:
        if kw in full_text:
            scores["policy"] += 1

    # If document has tables with financial indicators, boost financial
    if len(parsed_doc.tables) >= 2:
        scores["financial"] += 2

    # Determine all categories meeting threshold (score >= 2 or top score)
    max_score = max(scores.values())
    if max_score == 0:
        matched_types = ["policy"]
    else:
        # Include types that have a strong match (at least 2 hits and within 50% of top score)
        threshold = max(2, int(max_score * 0.5))
        matched_types = [t for t, s in scores.items() if s >= threshold]
        if not matched_types:
            best = max(scores, key=lambda k: scores[k])
            matched_types = [best]

    primary_type = ", ".join(matched_types)
    confidence = min(0.9, 0.4 + max_score * 0.05)

    return RouterResult(
        document_type=primary_type,
        document_types=matched_types,
        document_subtype=None,
        confidence=confidence,
        reasoning=f"Full-document rule-based classification: detected {matched_types} based on keyword density and table analysis.",
        doc_title=parsed_doc.metadata.get("title"),
        doc_author=parsed_doc.metadata.get("author"),
        doc_date=None,
        doc_summary=None,
        used_fallback=True,
    )
