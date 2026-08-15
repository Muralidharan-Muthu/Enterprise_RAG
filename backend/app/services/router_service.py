"""
Gemma 4 document router — classifies a parsed document into one of 5 types
and extracts metadata. Falls back to a rule-based classifier if the CDAC
endpoint is unreachable or returns low-confidence results.
"""
import json
import logging
import re
import threading
from dataclasses import dataclass
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
    a new connection (and paying TLS handshake cost) per classification.
    Thread-safe double-checked init; never closed per-call."""
    global _client
    if _client is None or _client.is_closed:
        with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.Client(timeout=settings.GEMMA4_TIMEOUT_SECONDS)
    return _client

DOCUMENT_TYPES = ("policy", "financial", "legal", "entity", "research")

SYSTEM_PROMPT = """You are a Software Engineer working on an Enterprise Document
Intelligence system. Your job is to analyze document content and classify it into
exactly one category, then extract structured metadata.

Respond ONLY with valid JSON. No text, explanation, or markdown outside the JSON."""

USER_PROMPT_TEMPLATE = """Analyze this document and classify it.

Filename: {filename}
First 2000 characters of content:
{content_excerpt}

Detected tables: {table_count}
Page count: {page_count}

Classify into EXACTLY ONE of:
- "policy":    Policy documents, SOPs, operational manuals, HR policies, FAQs, compliance guides
- "financial": Financial reports, balance sheets, invoices, budgets, P&L statements, revenue reports
- "legal":     Contracts, NDAs, agreements, terms of service, legal notices, court documents
- "entity":    Org charts, entity relationship documents, knowledge bases, directories
- "research":  Research papers, scientific reports, academic publications, technical papers

Respond with this exact JSON structure:
{{
  "document_type": "<one of the 5 types>",
  "document_subtype": "<specific subtype, e.g. NDA, balance_sheet, SOP>",
  "confidence": <0.0 to 1.0>,
  "reasoning": "<2-3 sentence explanation of classification>",
  "doc_title": "<extracted or inferred title>",
  "doc_author": "<extracted author name or null>",
  "doc_date": "<YYYY-MM-DD or null>",
  "doc_summary": "<100-word summary of the document>",
  "language": "en"
}}"""


@dataclass
class RouterResult:
    document_type: str
    document_subtype: Optional[str]
    confidence: float
    reasoning: str
    doc_title: Optional[str]
    doc_author: Optional[str]
    doc_date: Optional[str]
    doc_summary: Optional[str]
    language: str = "en"
    used_fallback: bool = False


def classify_document(parsed_doc: ParsedDocument) -> RouterResult:
    """
    Classify the document type using Gemma 4 on CDAC.
    Falls back to rule-based classification if Gemma is unavailable.
    """
    content_excerpt = parsed_doc.raw_text[:2000]
    prompt = USER_PROMPT_TEMPLATE.format(
        filename=parsed_doc.filename,
        content_excerpt=content_excerpt,
        table_count=len(parsed_doc.tables),
        page_count=parsed_doc.page_count,
    )

    if settings.GEMMA4_BASE_URL:
        try:
            raw_json = _call_gemma(prompt)
            result = _parse_gemma_response(raw_json)
            if result and result.confidence >= 0.5:
                logger.info(
                    "Gemma classified '%s' as '%s' (confidence=%.2f)",
                    parsed_doc.filename,
                    result.document_type,
                    result.confidence,
                )
                return result
            logger.warning("Gemma returned low confidence (%.2f), using fallback", result.confidence if result else 0)
        except Exception as exc:
            logger.warning("Gemma4 call failed: %s — using rule-based fallback", exc)
    else:
        logger.info("GEMMA4_BASE_URL not configured — using rule-based fallback")

    return _rule_based_classify(parsed_doc)


@retry(stop=stop_after_attempt(2), wait=wait_fixed(2))
def _call_gemma(user_prompt: str) -> str:
    """HTTP call to CDAC Gemma 4 endpoint. Tries OpenAI-compatible format first."""
    base_url = settings.GEMMA4_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.GEMMA4_API_KEY:
        headers["Authorization"] = f"Bearer {settings.GEMMA4_API_KEY}"

    payload = {
        "model": settings.GEMMA4_MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": settings.GEMMA4_MAX_TOKENS,
        "temperature": 0.1,
    }

    client = _get_client()
    response = client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()

    # OpenAI-compatible response format
    return data["choices"][0]["message"]["content"]


def _parse_gemma_response(raw: str) -> Optional[RouterResult]:
    # Strip markdown code fences if LLM wrapped the JSON
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object within the text
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            obj = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    doc_type = obj.get("document_type", "").lower()
    if doc_type not in DOCUMENT_TYPES:
        doc_type = "policy"

    return RouterResult(
        document_type=doc_type,
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
    Fast keyword-based fallback. No LLM call required.
    Analyzes filename + first 1000 chars of text to determine document type.
    """
    text = (parsed_doc.filename + " " + parsed_doc.raw_text[:1000]).lower()

    scores: dict[str, int] = {t: 0 for t in DOCUMENT_TYPES}

    # Financial signals
    for kw in ["revenue", "profit", "loss", "balance sheet", "income", "expense",
               "cash flow", "quarterly", "annual report", "fiscal", "ebitda",
               "invoice", "budget", "financial", "p&l"]:
        if kw in text:
            scores["financial"] += 1

    # Legal signals
    for kw in ["agreement", "contract", "clause", "whereas", "party", "parties",
               "indemnif", "liability", "termination", "governing law", "nda",
               "confidential", "breach", "obligation", "arbitration"]:
        if kw in text:
            scores["legal"] += 1

    # Research signals
    for kw in ["abstract", "methodology", "hypothesis", "conclusion", "doi",
               "journal", "citation", "bibliography", "experiment", "research",
               "study", "findings", "paper"]:
        if kw in text:
            scores["research"] += 1

    # Entity signals
    for kw in ["organization chart", "org chart", "subsidiary", "hierarchy",
               "relationship", "entity", "parent company", "division"]:
        if kw in text:
            scores["entity"] += 1

    # Policy signals (default)
    for kw in ["policy", "procedure", "sop", "guideline", "standard", "manual",
               "process", "faq", "regulation", "compliance", "rule"]:
        if kw in text:
            scores["policy"] += 1

    # If document has many tables, lean toward financial
    if len(parsed_doc.tables) >= 3:
        scores["financial"] += 2

    best = max(scores, key=lambda k: scores[k])
    top_score = scores[best]

    if top_score == 0:
        best = "policy"

    confidence = min(0.9, 0.4 + top_score * 0.1)

    return RouterResult(
        document_type=best,
        document_subtype=None,
        confidence=confidence,
        reasoning=f"Rule-based classification: '{best}' scored {top_score} keyword matches.",
        doc_title=parsed_doc.metadata.get("title"),
        doc_author=parsed_doc.metadata.get("author"),
        doc_date=None,
        doc_summary=None,
        used_fallback=True,
    )
