"""
Phase 2 Legal Clause Enrichment.

Calls Gemma 4 to classify each legal clause and extract structured metadata:
  clause_type, risk_level, risk_rationale, obligor/obligee, parties_mentioned,
  key_dates, monetary_values.

Strategy: batch up to BATCH_SIZE clauses per request, run MAX_WORKERS batches
concurrently via ThreadPoolExecutor. Falls back to safe defaults if Gemma is
unavailable or returns bad JSON — never raises, never blocks ingestion.

Performance estimate for large contracts:
  200 clauses / BATCH_SIZE=5 = 40 requests
  40 requests / MAX_WORKERS=4 = 10 rounds × ~3s/round ≈ 30s added to Stage 3
  500 clauses → ~75s (acceptable for a background Celery task)
"""
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.models.document import LegalClause
from app.services import gemma_client

logger = logging.getLogger(__name__)

BATCH_SIZE = 5      # clauses per Gemma request (trade-off: fewer calls vs. prompt length)
MAX_WORKERS = 4     # concurrent Gemma calls (tune to CDAC endpoint rate limits)
MAX_RETRIES = 2     # per batch before giving up
RETRY_DELAY = 1.0   # seconds; multiplied by attempt number (linear backoff)

# Belt-and-suspenders: the ThreadPoolExecutor below can never spawn more workers
# than the process-wide Gemma concurrency cap (gemma_client._get_sync_semaphore
# already enforces this globally, but capping the pool size avoids threads
# piling up blocked on semaphore.acquire()).
_EFFECTIVE_MAX_WORKERS = max(1, min(MAX_WORKERS, getattr(settings, "GEMMA4_MAX_CONCURRENT", MAX_WORKERS) or 1))

VALID_CLAUSE_TYPES = frozenset({
    "obligation", "prohibition", "right", "definition", "liability",
    "indemnification", "termination", "confidentiality", "dispute_resolution",
    "force_majeure", "warranty", "penalty", "governing_law", "general",
})
VALID_RISK_LEVELS = frozenset({"high", "medium", "low"})

_SYSTEM_PROMPT = (
    "You are a legal clause analyzer. For each numbered clause below, return a JSON array "
    "(one object per clause, same order, no extra keys).\n\n"
    "Each object must have exactly these keys (use null when a field does not apply):\n"
    '  "clause_type": one of [obligation, prohibition, right, definition, liability, '
    "indemnification, termination, confidentiality, dispute_resolution, "
    "force_majeure, warranty, penalty, governing_law, general]\n"
    '  "risk_level": "high" | "medium" | "low" | null\n'
    '    (null for purely procedural or definitional clauses)\n'
    '  "risk_rationale": string | null  (one sentence explaining the risk)\n'
    '  "obligor": string | null  (party bearing the primary obligation)\n'
    '  "obligee": string | null  (party receiving the primary benefit)\n'
    '  "parties_mentioned": [string]  (all named parties; empty list if none)\n'
    '  "key_dates": {}  (object mapping label → ISO-8601 date, e.g. {"effective_date": "2025-01-01"})\n'
    '  "monetary_values": []  (list of {"amount": number, "currency": "ISO-4217", "description": string})\n\n'
    "Respond with ONLY a valid JSON array. No markdown fences, no explanation."
)


class ClauseEnrichmentResult(BaseModel):
    """Validated enrichment output for a single clause."""
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
    def _valid_clause_type(cls, v: str) -> str:
        return v if v in VALID_CLAUSE_TYPES else "general"

    @field_validator("risk_level")
    @classmethod
    def _valid_risk_level(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v if v in VALID_RISK_LEVELS else None

    @field_validator("parties_mentioned", mode="before")
    @classmethod
    def _coerce_parties(cls, v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if x and str(x).strip()]

    @field_validator("key_dates", mode="before")
    @classmethod
    def _coerce_key_dates(cls, v) -> dict:
        if not isinstance(v, dict):
            return {}
        return {str(k): str(val) for k, val in v.items() if k and val}

    @field_validator("monetary_values", mode="before")
    @classmethod
    def _coerce_monetary_values(cls, v) -> list[dict]:
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict) and item]


_FALLBACK = ClauseEnrichmentResult()


def enrich_clauses_batch(clauses: list[LegalClause]) -> list[LegalClause]:
    """Enrich all clauses in-place via Gemma. Returns the same list (mutated).
    No-op when Gemma is not configured. Never raises."""
    if not clauses:
        return clauses
    if not getattr(settings, "GEMMA4_BASE_URL", None):
        logger.info("GEMMA4_BASE_URL not set — skipping clause enrichment")
        return clauses

    batches = [clauses[i : i + BATCH_SIZE] for i in range(0, len(clauses), BATCH_SIZE)]
    results: dict[int, list[ClauseEnrichmentResult]] = {}

    with ThreadPoolExecutor(max_workers=_EFFECTIVE_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_enrich_one_batch, batch): idx
            for idx, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                logger.warning("Enrichment batch %d thread error: %s", idx, exc)
                results[idx] = [_FALLBACK] * len(batches[idx])

    for batch_idx, batch in enumerate(batches):
        enrichments = results.get(batch_idx, [_FALLBACK] * len(batch))
        for clause, enrichment in zip(batch, enrichments):
            _apply(clause, enrichment)

    return clauses


# ── Internal helpers ───────────────────────────────────────────────────────────

def _enrich_one_batch(clauses: list[LegalClause]) -> list[ClauseEnrichmentResult]:
    """Single batch: call Gemma with retries. Returns fallback list on failure."""
    numbered = "\n\n".join(
        f"[Clause {i + 1}]\n{c.clause_text[:1500]}"
        for i, c in enumerate(clauses)
    )
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            raw = _call_gemma(numbered, len(clauses))
            parsed = _parse_response(raw, len(clauses))
            if parsed is not None:
                return parsed
            logger.debug("Enrichment parse failed on attempt %d", attempt)
        except Exception as exc:
            last_exc = exc
            logger.debug("Enrichment attempt %d error: %s", attempt, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * (attempt + 1))

    logger.warning(
        "All enrichment retries exhausted for batch of %d (%s) — using fallback",
        len(clauses), last_exc,
    )
    return [_FALLBACK] * len(clauses)


def _call_gemma(text: str, expected_count: int) -> str:
    """Delegates to the shared gemma_client.chat(), which pools connections and
    gates every sync/thread caller behind the process-wide GEMMA4_MAX_CONCURRENT
    semaphore. retries=0 preserves this module's own outer retry loop
    (_enrich_one_batch already retries MAX_RETRIES times)."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    return gemma_client.chat(
        messages,
        max_tokens=350 * expected_count,
        temperature=0.0,
        retries=0,
        # Batched multi-clause prompts generate far more than a single answer;
        # keep the original 2x read budget so large batches don't spuriously time out.
        timeout=(settings.GROQ_TIMEOUT_SECONDS or settings.GEMMA4_TIMEOUT_SECONDS or 60) * 2,
        model=settings.GROQ_ENRICHMENT_MODEL,
    )


def _parse_response(raw: str, expected_count: int) -> Optional[list[ClauseEnrichmentResult]]:
    """Parse and validate Gemma JSON output. Returns None on unrecoverable parse error."""
    cleaned = re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return None

    if not isinstance(obj, list):
        return None

    out: list[ClauseEnrichmentResult] = []
    for item in obj:
        if not isinstance(item, dict):
            out.append(_FALLBACK)
            continue
        try:
            out.append(ClauseEnrichmentResult(**item))
        except Exception:
            out.append(_FALLBACK)

    while len(out) < expected_count:
        out.append(_FALLBACK)
    return out[:expected_count]


def _apply(clause: LegalClause, e: ClauseEnrichmentResult) -> None:
    clause.clause_type = e.clause_type
    clause.risk_level = e.risk_level
    clause.risk_rationale = e.risk_rationale
    clause.obligor = e.obligor
    clause.obligee = e.obligee
    clause.parties_mentioned = e.parties_mentioned
    clause.key_dates = e.key_dates
    clause.monetary_values = e.monetary_values
