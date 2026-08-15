"""Explicit, content-class-driven routing decision for extracted images.

Two stages so routing is deterministic and easy to extend:

    VLM output ──▶ classify_content() ──▶ content class
                                             │
                                             ▼
                          route_for_class(class, store-hint, confidence) ──▶ RoutingDecision

Content classes:
    structured_table   – JSON with data rows                -> table_store
    structured_chart   – JSON chart data (categories/series) -> table_store
    legal_content      – VLM read it as legal/clauses        -> clause_store
    plain_document_text– prose (research or general text)    -> document_store / vector_store
    mixed_content      – claimed structured but no structure -> vector_store
    decorative         – no meaningful searchable content    -> image_store (repository)
    unknown            – searchable but unclassifiable        -> vector_store

Design guards learnt from real runs:
  * The class is derived from the EXTRACTED CONTENT (JSON shape) + the VLM's
    *semantic* store hint for clause/document. We never trust a VLM "image_store"
    verdict for a content-bearing figure — VLMs mislabel informative charts as
    "pure figures" and would orphan them.
  * A specialized class (table/chart/legal) only routes to its specialized store
    when confidence is high enough; otherwise it degrades to vector_store — still
    searchable, never a wrong specialized destination.
  * image_store is reached ONLY by the decorative class (nothing searchable).

Pure and dependency-free: deterministic, debuggable, unit-testable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Minimum meaningful text length (chars) to consider content genuinely searchable.
SEARCHABLE_MIN_CHARS = 12
# Below this VLM confidence, a specialized class (table/chart/legal) degrades to
# vector_store rather than committing to a specialized store on a shaky read.
MIN_CONFIDENCE_FOR_SPECIALIZED = 0.35

# ── content classes ──
CLASS_STRUCTURED_TABLE = "structured_table"
CLASS_STRUCTURED_CHART = "structured_chart"
CLASS_LEGAL = "legal_content"
CLASS_DOCUMENT_TEXT = "plain_document_text"
CLASS_MIXED = "mixed_content"
CLASS_DECORATIVE = "decorative"
CLASS_UNKNOWN = "unknown"

_CHART_KEYS = {"categories", "series", "slices", "segments", "percentages",
               "data_points", "values", "labels"}
_CONTENT_TYPE = {
    "table_store": "table",
    "clause_store": "text",
    "document_store": "text",
    "vector_store": "text",
    "image_store": "figure",
}
_FENCE_RE = re.compile(r"```(?:json|JSON)?|~~~|`")


def content_type_for(store: str) -> str:
    return _CONTENT_TYPE.get(store, "figure")


def is_searchable(text: str | None) -> bool:
    """True when *text* carries enough real content to be worth indexing."""
    return bool(text and len(text.strip()) >= SEARCHABLE_MIN_CHARS)


def _parse_json_obj(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        val = json.loads(_FENCE_RE.sub("", text).strip())
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


# ── extraction quality ──
QUALITY_HIGH = "high"
QUALITY_MEDIUM = "medium"
QUALITY_LOW = "low"


@dataclass
class ContentValidation:
    is_valid: bool            # structurally usable for its specialized store
    quality: str              # high | medium | low
    issues: tuple[str, ...]   # human-readable problems found


@dataclass
class RoutingDecision:
    destination_store: str
    content_type: str
    confidence: float
    reason: str
    content_class: str
    extraction_quality: str = QUALITY_MEDIUM


def classify_content(
    *,
    canonical_store: str,
    structured_content: str,
    ocr_text: str,
    vlm_succeeded: bool,
) -> str:
    """Classify the extracted content into a content class (see module docstring).

    ``canonical_store`` is the VLM's store guess mapped to a canonical name; it is
    used only as a *semantic hint* for legal/document prose — never to keep a
    content-bearing image in image_store.
    """
    if not (is_searchable(structured_content) or is_searchable(ocr_text)):
        return CLASS_DECORATIVE

    data = _parse_json_obj(structured_content)
    if data is not None:
        rows = data.get("rows")
        if isinstance(rows, list) and rows:
            return CLASS_STRUCTURED_TABLE
        if any(k in data for k in _CHART_KEYS):
            return CLASS_STRUCTURED_CHART

    # Prose / non-structured content — lean on the VLM's semantic store hint, but
    # only for the trustworthy semantic stores (NOT image_store).
    if vlm_succeeded:
        if canonical_store == "clause_store":
            return CLASS_LEGAL
        if canonical_store == "document_store":
            return CLASS_DOCUMENT_TEXT
        if canonical_store == "vector_store":
            return CLASS_DOCUMENT_TEXT
        if canonical_store == "table_store":
            # Claimed a table but produced no structured rows -> treat as mixed prose.
            return CLASS_MIXED
    # image_store / unknown store, or failed VLM, but there IS searchable text.
    return CLASS_UNKNOWN


def route_for_class(content_class: str, canonical_store: str, confidence: float) -> str:
    """Map a content class (+ store hint + confidence) to a destination store."""
    if content_class in (CLASS_STRUCTURED_TABLE, CLASS_STRUCTURED_CHART):
        return "table_store" if confidence >= MIN_CONFIDENCE_FOR_SPECIALIZED else "vector_store"
    if content_class == CLASS_LEGAL:
        return "clause_store" if confidence >= MIN_CONFIDENCE_FOR_SPECIALIZED else "vector_store"
    if content_class == CLASS_DOCUMENT_TEXT:
        # Research/citation prose -> document_store; general prose -> vector_store.
        return "document_store" if canonical_store == "document_store" else "vector_store"
    if content_class == CLASS_DECORATIVE:
        return "image_store"
    # MIXED / UNKNOWN -> keep searchable as a descriptive vector chunk.
    return "vector_store"


def validate_content(content_class: str, structured_content: str, ocr_text: str = "") -> ContentValidation:
    """Validate extracted content for its class and grade extraction quality.

    A SEPARATE decision from classification: classification says WHAT the content is;
    validation says whether it is well-formed enough for its specialized store and how
    good the extraction is. A structured class that fails validation (no rows,
    inconsistent columns) is not usable as a table and should be demoted so it never
    pollutes table_store. ``quality`` (high|medium|low) is carried for human review,
    analytics, and re-run filtering.
    """
    if content_class in (CLASS_STRUCTURED_TABLE, CLASS_STRUCTURED_CHART):
        data = _parse_json_obj(structured_content) or {}
        rows = [r for r in (data.get("rows") or []) if isinstance(r, list)]
        headers = data.get("headers") if isinstance(data.get("headers"), list) else []
        row_lens = [len(r) for r in rows]
        n_rows = len(row_lens)
        consistent = len(set(row_lens)) <= 1
        issues: list[str] = []
        if n_rows == 0:
            issues.append("no data rows")
        if n_rows and not consistent:
            issues.append("inconsistent column counts")
        if headers and n_rows and consistent and row_lens[0] != len(headers):
            issues.append("row width != header count")
        if headers:
            norm = [str(h).strip().lower() for h in headers]
            if len(set(norm)) != len(norm):
                issues.append("duplicate headers")
        # Usable as a structured table iff at least one consistent data row exists.
        is_valid = n_rows >= 1 and consistent
        if is_valid and headers and n_rows >= 2 and not issues:
            quality = QUALITY_HIGH
        elif is_valid:
            quality = QUALITY_MEDIUM
        else:
            quality = QUALITY_LOW
        return ContentValidation(is_valid, quality, tuple(issues))

    if content_class == CLASS_DECORATIVE:
        return ContentValidation(False, QUALITY_LOW, ("no searchable content",))

    # Prose classes (legal/document/mixed/unknown): text is always "valid"; grade by length.
    text = (structured_content or "").strip() or (ocr_text or "").strip()
    n = len(text)
    quality = QUALITY_HIGH if n >= 200 else (QUALITY_MEDIUM if n >= 60 else QUALITY_LOW)
    return ContentValidation(True, quality, ())


def decide_route(
    *,
    canonical_store: str,
    structured_content: str,
    ocr_text: str,
    confidence: float = 0.0,
    vlm_succeeded: bool,
    base_reason: str = "",
) -> RoutingDecision:
    """Resolve the final destination for a KEPT image: classify -> validate -> route."""
    conf = float(confidence or 0.0)
    # 1) classify — WHAT is this content?
    content_class = classify_content(
        canonical_store=canonical_store,
        structured_content=structured_content,
        ocr_text=ocr_text,
        vlm_succeeded=vlm_succeeded,
    )
    # 2) validate — well-formed enough for its specialized store? + extraction quality.
    validation = validate_content(content_class, structured_content, ocr_text)
    # 3) route — a malformed structured output must not pollute a specialized store;
    #    demote it to mixed_content so it stays searchable in vector_store instead.
    routing_class = content_class
    if content_class in (CLASS_STRUCTURED_TABLE, CLASS_STRUCTURED_CHART) and not validation.is_valid:
        routing_class = CLASS_MIXED
    dest = route_for_class(routing_class, canonical_store, conf)

    bits = [f"class={content_class}", f"quality={validation.quality}"]
    if validation.issues:
        bits.append("issues=" + ",".join(validation.issues))
    if routing_class != content_class:
        bits.append(f"demoted->{routing_class}(invalid)")
    reason = (f"{base_reason}; " if base_reason else "") + " ".join(bits) + f" -> {dest}"

    return RoutingDecision(
        destination_store=dest,
        content_type=content_type_for(dest),
        confidence=conf,
        reason=reason,
        content_class=content_class,
        extraction_quality=validation.quality,
    )
