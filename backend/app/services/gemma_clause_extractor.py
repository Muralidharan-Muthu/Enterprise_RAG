"""
Gemma-first legal clause extraction.

Primary path  → Gemma extracts clause boundaries AND all metadata in one pass per segment.
Fallback path → regex extract_legal_clauses() when Gemma is unavailable, times out, or
                too many segments fail.

Caller checks ExtractionMeta.source:
  "gemma"  → LegalClause list fully populated; no further enrichment needed.
  "regex"  → structural fields only; caller should run enrich_clauses_batch() next.

Document segmentation strategy:
  Split on text_block boundaries (Docling output) up to MAX_SEGMENT_CHARS each,
  keeping a 2-block overlap so clauses that span a segment boundary are still captured
  fully in at least one segment. Dedup by clause_number or text fingerprint removes
  duplicates introduced by the overlap.

Under-extraction safety net (coverage check):
  A long, heading-dense segment (many short un-numbered policy-statement-style
  sub-headings, as opposed to sparse numbered "N. Title" legal clauses) can cause
  Gemma to enumerate only some of the segment's distinct sections and silently
  drop the rest — no error, no malformed JSON, just an incomplete-but-valid
  response. _segment_coverage() estimates how much of the segment's text the
  returned clauses actually account for; a low ratio triggers one corrective
  retry (_retry_low_coverage_segment) that explicitly calls out the shortfall and
  merges the union of both passes. This is a real, measured problem class (not
  hypothetical) — see test_gemma_clause_extractor.py's coverage-retry tests.
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.models.document import LegalClause, ParsedDocument
from app.services.clause_enrichment_service import VALID_CLAUSE_TYPES, VALID_RISK_LEVELS

logger = logging.getLogger(__name__)

MAX_SEGMENT_CHARS = 5_000   # ~1 250 tokens; smaller segments -> fewer distinct
                            # headings per call -> less enumeration undercounting
MIN_CLAUSES_EXPECTED = 1    # fewer than this triggers fallback (sanity check)
MAX_FAILED_RATIO = 0.5      # >50% segments failing → fallback
MAX_RETRIES = 2
RETRY_DELAY = 1.0           # linear backoff: attempt × RETRY_DELAY
SEGMENT_MAX_TOKENS = 6_000  # response budget per segment (was 4096 — too tight for
                            # a segment with several fully-detailed clause objects)
MIN_SEGMENT_COVERAGE = 0.55 # below this fraction of segment text "explained" by the
                            # extracted clauses, assume under-extraction and retry once


# ── Response schema ────────────────────────────────────────────────────────────

class _GemmaClause(BaseModel):
    clause_number: Optional[str] = None
    clause_title: Optional[str] = None
    clause_text: str
    clause_type: str = "general"
    risk_level: Optional[str] = None
    risk_rationale: Optional[str] = None
    obligor: Optional[str] = None
    obligee: Optional[str] = None
    parties_mentioned: list[str] = Field(default_factory=list)
    key_dates: dict = Field(default_factory=dict)
    monetary_values: list[dict] = Field(default_factory=list)

    @field_validator("clause_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        return v if v in VALID_CLAUSE_TYPES else "general"

    @field_validator("risk_level")
    @classmethod
    def _valid_risk(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v if v in VALID_RISK_LEVELS else None

    @field_validator("parties_mentioned", mode="before")
    @classmethod
    def _coerce_parties(cls, v) -> list[str]:
        return [str(x).strip() for x in v if x and str(x).strip()] if isinstance(v, list) else []

    @field_validator("key_dates", mode="before")
    @classmethod
    def _coerce_dates(cls, v) -> dict:
        return {str(k): str(val) for k, val in v.items() if k and val} if isinstance(v, dict) else {}

    @field_validator("monetary_values", mode="before")
    @classmethod
    def _coerce_monetary(cls, v) -> list[dict]:
        return [i for i in v if isinstance(i, dict) and i] if isinstance(v, list) else []


@dataclass
class ExtractionMeta:
    source: str                       # "gemma" | "regex"
    segment_count: int = 0
    failed_segments: int = 0
    extracted_count: int = 0
    fallback_reason: Optional[str] = None


# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a legal clause extractor for enterprise contracts. "
    "Given a segment of a legal document, extract EVERY distinct clause or section "
    "present — do not skip any.\n\n"
    "A 'clause' is not limited to numbered legal clauses like \"12.3 Indemnification\". "
    "It also includes un-numbered sub-headings — bold text, underlined text, or short "
    "standalone titles such as \"Data Retention Policy\" or \"Vendor Management Policy\" "
    "— even when they have no number and even when several appear close together under "
    "a shared parent heading (e.g. multiple policy statements listed one after another "
    "under a \"Section A\" heading). Extract the PARENT heading itself as its own clause "
    "too (clause_text = its own intro text, or empty if it is a bare heading with no "
    "text of its own), in addition to each of its sub-headings.\n"
    "If a segment contains many short sections, this makes it MORE important — not "
    "less — that you list all of them: do not stop early or summarize a run of short "
    "sections as one entry. Count the distinct headings in the segment before you "
    "answer, and make sure your output has that many entries.\n\n"
    "For each clause return a JSON object with EXACTLY these keys "
    "(use null when a field does not apply):\n"
    '  "clause_number":     string | null  (e.g. "12.3.1", "Article III", "Section 4.2")\n'
    '  "clause_title":      string | null  (heading without the number)\n'
    '  "clause_text":       string         (verbatim clause body, without the heading)\n'
    '  "clause_type":       one of [obligation, prohibition, right, definition, liability,\n'
    "                        indemnification, termination, confidentiality, dispute_resolution,\n"
    "                        force_majeure, warranty, penalty, governing_law, general]\n"
    '  "risk_level":        "high" | "medium" | "low" | null\n'
    '  "risk_rationale":    string | null  (one sentence explaining the risk)\n'
    '  "obligor":           string | null  (party bearing the primary obligation)\n'
    '  "obligee":           string | null  (party receiving the primary benefit)\n'
    '  "parties_mentioned": [string]       (all named parties; empty list if none)\n'
    '  "key_dates":         {}             (label → ISO-8601 date)\n'
    '  "monetary_values":   []             (list of {"amount": number, "currency": "ISO-4217", "description": string})\n\n'
    'Return ONLY: {"clauses": [...]}\n'
    "No markdown fences, no explanation, no trailing text."
)


# ── Public entry point ─────────────────────────────────────────────────────────

def extract_clauses_gemma(
    parsed_doc: ParsedDocument,
) -> tuple[list[LegalClause], ExtractionMeta]:
    """
    Gemma-first extraction. Returns (clauses, meta). Never raises.

    meta.source == "gemma"  → clauses fully enriched, store directly.
    meta.source == "regex"  → structural fields only, call enrich_clauses_batch() next.
    """
    if not getattr(settings, "GEMMA4_BASE_URL", None):
        return _regex_fallback(parsed_doc, "GEMMA4_BASE_URL not configured")

    segments = _build_segments(parsed_doc)
    meta = ExtractionMeta(source="gemma", segment_count=len(segments))

    all_pairs: list[tuple[_GemmaClause, int]] = []  # (clause, start_page)
    for seg_idx, (seg_text, seg_page) in enumerate(segments):
        result = _extract_segment(seg_text, seg_page, seg_idx)
        if result is None:
            meta.failed_segments += 1
        else:
            all_pairs.extend((gc, seg_page) for gc in result)

    failed_ratio = meta.failed_segments / max(len(segments), 1)
    if failed_ratio > MAX_FAILED_RATIO:
        return _regex_fallback(
            parsed_doc,
            f"{meta.failed_segments}/{len(segments)} segments failed (>{MAX_FAILED_RATIO:.0%})",
        )

    deduped = _dedup_pairs(all_pairs)
    if len(deduped) < MIN_CLAUSES_EXPECTED:
        return _regex_fallback(
            parsed_doc,
            f"Only {len(deduped)} clauses extracted (minimum {MIN_CLAUSES_EXPECTED})",
        )

    stitched = _stitch_continuations(deduped)

    meta.extracted_count = len(stitched)
    legal_clauses = [_to_legal_clause(i, gc, pages) for i, (gc, pages) in enumerate(stitched)]
    logger.info(
        "[gemma_extractor] %d clauses from %d segments (%d failed, %d deduped, %d stitched)",
        len(legal_clauses), len(segments), meta.failed_segments,
        len(all_pairs) - len(deduped), len(deduped) - len(stitched),
    )
    return legal_clauses, meta


# ── Document segmentation ──────────────────────────────────────────────────────

def _build_segments(parsed_doc: ParsedDocument) -> list[tuple[str, int]]:
    """Return (text, start_page) tuples sized ≤ MAX_SEGMENT_CHARS with block overlap."""
    if parsed_doc.text_blocks:
        return _segment_from_blocks(parsed_doc)
    return _segment_from_raw(parsed_doc.raw_text or "")


def _segment_from_blocks(parsed_doc: ParsedDocument) -> list[tuple[str, int]]:
    segments: list[tuple[str, int]] = []
    current_parts: list[str] = []
    current_len = 0
    start_page = parsed_doc.text_blocks[0].page_number if parsed_doc.text_blocks else 1

    for block in parsed_doc.text_blocks:
        text = block.text.strip()
        if not text:
            continue
        if current_len + len(text) > MAX_SEGMENT_CHARS and current_parts:
            segments.append(("\n\n".join(current_parts), start_page))
            # 2-block overlap: keeps partial clause context at boundary
            overlap = current_parts[-2:] if len(current_parts) >= 2 else current_parts[-1:]
            current_parts = list(overlap)
            current_len = sum(len(p) for p in current_parts)
            start_page = block.page_number
        current_parts.append(text)
        current_len += len(text)

    if current_parts:
        segments.append(("\n\n".join(current_parts), start_page))
    return segments or [("", 1)]


def _segment_from_raw(raw: str) -> list[tuple[str, int]]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", raw) if p.strip()]
    segments: list[tuple[str, int]] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        if current_len + len(para) > MAX_SEGMENT_CHARS and current:
            segments.append(("\n\n".join(current), 1))
            overlap = current[-2:] if len(current) >= 2 else current[-1:]
            current = list(overlap)
            current_len = sum(len(p) for p in current)
        current.append(para)
        current_len += len(para)

    if current:
        segments.append(("\n\n".join(current), 1))
    return segments or [("", 1)]


# ── Gemma call per segment ─────────────────────────────────────────────────────

def _extract_segment(
    text: str,
    start_page: int,
    seg_idx: int,
) -> Optional[list[_GemmaClause]]:
    """Call Gemma for one segment with retries. Returns None on total failure.

    A parsed-but-suspiciously-thin result (see _segment_coverage) triggers one
    additional corrective retry before returning — see module docstring."""
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            raw = _call_gemma(text, start_page)
            parsed = _parse_gemma_response(raw)
            if parsed is not None:
                coverage = _segment_coverage(text, parsed)
                if coverage < MIN_SEGMENT_COVERAGE:
                    logger.info(
                        "[gemma_extractor] seg %d low coverage (%.0f%%, %d clauses) — "
                        "retrying with corrective nudge",
                        seg_idx, coverage * 100, len(parsed),
                    )
                    parsed = _retry_low_coverage_segment(text, start_page, parsed) or parsed
                return parsed
            logger.debug("[gemma_extractor] seg %d parse fail, attempt %d", seg_idx, attempt)
        except Exception as exc:
            last_exc = exc
            logger.debug("[gemma_extractor] seg %d error, attempt %d: %s", seg_idx, attempt, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * (attempt + 1))

    logger.warning(
        "[gemma_extractor] seg %d failed after %d retries: %s", seg_idx, MAX_RETRIES, last_exc
    )
    return None


def _segment_coverage(segment_text: str, clauses: list[_GemmaClause]) -> float:
    """Rough diagnostic: fraction of the segment's normalized text length accounted
    for by the extracted clauses' text. Not exact (clause_text may be lightly
    reworded), but a large shortfall reliably flags under-extraction — e.g. Gemma
    enumerating only the last item(s) of a long, dense segment and silently
    dropping earlier un-numbered headings (the "Section A" bug this guards against:
    3 of 4 sibling policy statements — plus the section heading itself — were
    dropped while only the last one, "Vendor Management Policy", survived)."""
    seg_len = len(re.sub(r"\s+", " ", segment_text).strip())
    if seg_len == 0:
        return 1.0
    covered = sum(len(re.sub(r"\s+", " ", c.clause_text).strip()) for c in clauses)
    return min(1.0, covered / seg_len)


def _retry_low_coverage_segment(
    text: str, start_page: int, first_pass: list[_GemmaClause],
) -> Optional[list[_GemmaClause]]:
    """One corrective retry when the first pass covers too little of the segment.
    Merges the union of both passes (deduped) so a partially-improved retry still
    keeps anything the first pass already got right. Returns None (caller keeps
    the first pass) if the retry itself fails or parses to nothing."""
    try:
        raw = _call_gemma_corrective(text, start_page, len(first_pass))
        second_pass = _parse_gemma_response(raw)
    except Exception as exc:
        logger.debug("[gemma_extractor] corrective retry failed: %s", exc)
        return None
    if not second_pass:
        return None
    combined = _dedup_pairs(
        [(c, start_page) for c in first_pass] + [(c, start_page) for c in second_pass]
    )
    return [c for c, _ in combined]


def _gemma_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if getattr(settings, "GEMMA4_API_KEY", None):
        headers["Authorization"] = f"Bearer {settings.GEMMA4_API_KEY}"
    return headers


def _call_gemma(text: str, start_page: int) -> str:
    base = settings.GEMMA4_BASE_URL.rstrip("/")
    payload = {
        "model": settings.GEMMA4_MODEL_NAME,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"[Document segment starting at page {start_page}]\n\n{text}"},
        ],
        "max_tokens": SEGMENT_MAX_TOKENS,
        "temperature": 0.0,
    }
    timeout = getattr(settings, "GEMMA4_TIMEOUT_SECONDS", 30) * 2
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{base}/chat/completions", json=payload, headers=_gemma_headers())
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_gemma_corrective(text: str, start_page: int, first_pass_count: int) -> str:
    """Re-ask Gemma for the same segment, explicitly calling out that the first
    pass looked incomplete. Framed as a follow-up turn (not a fresh prompt) so
    Gemma sees its own thin answer and is pushed to reconsider it specifically,
    rather than just re-rolling the same extraction."""
    base = settings.GEMMA4_BASE_URL.rstrip("/")
    payload = {
        "model": settings.GEMMA4_MODEL_NAME,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"[Document segment starting at page {start_page}]\n\n{text}"},
            {"role": "assistant", "content": json.dumps(
                {"clauses": [{"note": f"{first_pass_count} clause(s) found"}]}
            )},
            {"role": "user", "content": (
                f"That pass only found {first_pass_count} clause(s), which looks incomplete "
                "for a segment this size. Re-read the FULL segment above and list EVERY "
                "distinct section, including short un-numbered policy-statement headings "
                "(bold or underlined sub-headings without a number are still clauses) and "
                "the parent heading they sit under. Return the complete corrected list in "
                'the same {"clauses": [...]} format.'
            )},
        ],
        "max_tokens": SEGMENT_MAX_TOKENS,
        "temperature": 0.0,
    }
    timeout = getattr(settings, "GEMMA4_TIMEOUT_SECONDS", 30) * 2
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{base}/chat/completions", json=payload, headers=_gemma_headers())
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_gemma_response(raw: str) -> Optional[list[_GemmaClause]]:
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

    if not isinstance(obj, dict):
        return None
    raw_list = obj.get("clauses")
    if not isinstance(raw_list, list):
        return None

    out: list[_GemmaClause] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        if not str(item.get("clause_text", "")).strip():
            continue
        try:
            out.append(_GemmaClause(**item))
        except Exception:
            pass
    return out  # empty list is valid (segment genuinely has no clauses)


# ── Deduplication ──────────────────────────────────────────────────────────────

def _fingerprint(gc: _GemmaClause) -> str:
    """Stable dedup key: clause_number (preferred) or first-120-char text hash."""
    if gc.clause_number and gc.clause_number.strip():
        return f"num:{gc.clause_number.strip().lower()}"
    norm = re.sub(r"\s+", " ", gc.clause_text[:120]).strip().lower()
    return f"txt:{norm}"


def _dedup_pairs(
    pairs: list[tuple[_GemmaClause, int]]
) -> list[tuple[_GemmaClause, int]]:
    seen: set[str] = set()
    out: list[tuple[_GemmaClause, int]] = []
    for gc, page in pairs:
        fp = _fingerprint(gc)
        if fp not in seen:
            seen.add(fp)
            out.append((gc, page))
    return out


# ── Cross-page/segment continuation stitching ──────────────────────────────────

def _stitch_continuations(
    pairs: list[tuple[_GemmaClause, int]],
) -> list[tuple[_GemmaClause, list[int]]]:
    """Merge a headerless clause fragment (no clause_number AND no clause_title) into
    the immediately preceding titled clause when it is a direct textual continuation.

    The common trigger: a clause's tail spills across a page boundary, and Docling
    inserts the page's running header/footer text between the clause body and its
    continuation. That noise sits between the two in the segment text Gemma sees, so
    Gemma — correctly, from its narrow per-segment view — returns the tail as its own
    unlabeled clause instead of recognizing it as part of the preceding one. This pass
    catches that after extraction, using a small Gemma classification call per orphan
    (rare — only fires at genuine page/segment-boundary splits, not on every clause).

    Fails open to "merge" if Gemma is unreachable: a genuinely freestanding unlabeled
    clause is extremely unlikely in a document where every real clause already
    follows the "N. Title" convention this extractor relies on end to end.
    """
    if not pairs:
        return []
    stitched: list[tuple[_GemmaClause, list[int]]] = [(pairs[0][0], [pairs[0][1]])]
    for gc, page in pairs[1:]:
        prev_gc, prev_pages = stitched[-1]
        is_orphan_fragment = (
            not (gc.clause_number and gc.clause_number.strip())
            and not (gc.clause_title and gc.clause_title.strip())
        )
        if is_orphan_fragment and _is_continuation(prev_gc.clause_text, gc.clause_text):
            prev_gc.clause_text = f"{prev_gc.clause_text.rstrip()} {gc.clause_text.lstrip()}"
            if page not in prev_pages:
                prev_pages.append(page)
            logger.info(
                "[gemma_extractor] Stitched orphan continuation (page %s) into clause %r",
                page, prev_gc.clause_number or prev_gc.clause_title or "(untitled)",
            )
        else:
            stitched.append((gc, [page]))
    return stitched


def _is_continuation(prev_text: str, fragment_text: str) -> bool:
    """Ask Gemma whether fragment_text is the direct continuation of prev_text (the
    tail of the preceding clause) rather than the start of an unrelated clause.
    Fails open to True — see _stitch_continuations for why that default is safe here.
    """
    try:
        tail = prev_text[-400:]
        head = fragment_text[:400]
        payload = {
            "model": settings.GEMMA4_MODEL_NAME,
            "messages": [{
                "role": "user",
                "content": (
                    "Two text fragments from a legal contract. Fragment A is the end of "
                    "one clause. Fragment B is a candidate for being the tail end of that "
                    "SAME clause — cut apart only by page-layout noise (a running header/"
                    "footer) between them — rather than the start of a genuinely new, "
                    "unrelated clause.\n\n"
                    f"Fragment A (end):\n{tail}\n\nFragment B (start):\n{head}\n\n"
                    'Respond with ONLY {"continuation": true} or {"continuation": false}.'
                ),
            }],
            "max_tokens": 20,
            "temperature": 0.0,
        }
        base = settings.GEMMA4_BASE_URL.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if getattr(settings, "GEMMA4_API_KEY", None):
            headers["Authorization"] = f"Bearer {settings.GEMMA4_API_KEY}"
        timeout = getattr(settings, "GEMMA4_TIMEOUT_SECONDS", 30)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{base}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        m = re.search(r'"continuation"\s*:\s*(true|false)', content, re.IGNORECASE)
        if m:
            return m.group(1).lower() == "true"
        # Ambiguous/unparseable reply — same fail-open default as an outright error.
        return True
    except Exception as exc:
        logger.debug("[gemma_extractor] continuation check failed, defaulting to merge: %s", exc)
        return True


# ── Model conversion ───────────────────────────────────────────────────────────

def _to_legal_clause(idx: int, gc: _GemmaClause, pages: list[int]) -> LegalClause:
    clause = LegalClause(
        clause_index=idx,
        clause_text=gc.clause_text,
        clause_number=gc.clause_number,
        clause_title=gc.clause_title,
        page_number=pages[0],
        page_numbers=sorted(set(pages)),
        section_path=[gc.clause_number] if gc.clause_number else [],
    )
    clause.clause_type = gc.clause_type
    clause.risk_level = gc.risk_level
    clause.risk_rationale = gc.risk_rationale
    clause.obligor = gc.obligor
    clause.obligee = gc.obligee
    clause.parties_mentioned = gc.parties_mentioned
    clause.key_dates = gc.key_dates
    clause.monetary_values = gc.monetary_values
    return clause


# ── Regex fallback ─────────────────────────────────────────────────────────────

def _regex_fallback(
    parsed_doc: ParsedDocument,
    reason: str,
) -> tuple[list[LegalClause], ExtractionMeta]:
    meta = ExtractionMeta(source="regex", fallback_reason=reason)
    try:
        from app.services.chunker import extract_legal_clauses
        clauses = extract_legal_clauses(parsed_doc)
        meta.extracted_count = len(clauses)
        logger.info("[gemma_extractor] Regex fallback: %d clauses (reason: %s)", len(clauses), reason)
    except Exception as exc:
        logger.warning("[gemma_extractor] Regex fallback also failed: %s", exc)
        clauses = []
    return clauses, meta
