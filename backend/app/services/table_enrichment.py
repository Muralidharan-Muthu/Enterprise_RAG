"""
Table enrichment — rules-first derivation of table_store metadata columns.

Slice 3 (table-store enterprise design): every table (Docling-grid AND
image-VLM) must populate fiscal_year, reporting_period, currency,
table_category, detected_units, table_summary. Today only image-derived
tables get these (from the VLM's structured JSON, in store_router.
TableStoreHandler.insert). Docling-grid tables — the _store_tables path in
storage_service.py — leave them NULL.

This module closes that gap WITHOUT any new LLM/network call:
  - When a VLM already ran for this table (table crop reconstruction, see
    table_reconstruction.reconstruct_tables_with_vlm), its structured JSON
    output already carries these fields for free — prefer them.
  - Otherwise, derive rules-based best-effort values from the table's own
    headers/rows/caption (regex + keyword heuristics).

Pure / deterministic / side-effect-free — safe to unit test directly and
safe to call from any storage path. Never raises: any internal error is
caught and a fail-open all-None dict (with a non-empty table_summary) is
returned instead.
"""
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# currency
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOL_TO_CODE = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
}
_CURRENCY_CODES = ("USD", "EUR", "GBP", "INR")
# Longest-match-first so e.g. "₹" isn't shadowed and codes are matched as whole
# tokens (avoid matching "USD" inside an unrelated longer word).
_CURRENCY_CODE_RE = re.compile(r"\b(" + "|".join(_CURRENCY_CODES) + r")\b")
_CURRENCY_SYMBOL_RE = re.compile("[" + "".join(re.escape(s) for s in _CURRENCY_SYMBOL_TO_CODE) + "]")

# ---------------------------------------------------------------------------
# fiscal_year
# ---------------------------------------------------------------------------

# FY24, FY2024, FY 2024
_FY_RE = re.compile(r"\bFY\s?(\d{2,4})\b", re.IGNORECASE)
# A bare 4-digit year (19xx/20xx) near "fiscal"/"year"/"FY" — narrower window
# search than a blanket 4-digit-anywhere regex to avoid false positives on
# things like page numbers or unrelated figures.
_BARE_YEAR_NEAR_FISCAL_RE = re.compile(
    r"(?:fiscal|year)\D{0,10}(\b(?:19|20)\d{2}\b)|(\b(?:19|20)\d{2}\b)\D{0,10}(?:fiscal|year)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# reporting_period
# ---------------------------------------------------------------------------

_QUARTER_RE = re.compile(r"\bQ([1-4])\s?(\d{2,4})?\b", re.IGNORECASE)
_HALF_RE = re.compile(r"\bH([12])\s?(\d{2,4})?\b", re.IGNORECASE)
_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b",
    re.IGNORECASE,
)
_YTD_RE = re.compile(r"\bYTD\b", re.IGNORECASE)
_ANNUAL_RE = re.compile(r"\bannual\b", re.IGNORECASE)
_QUARTER_WORD_RE = re.compile(r"\bquarter(?:ly)?\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# table_category
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("balance_sheet", ("balance sheet", "assets", "liabilities", "shareholders equity",
                        "stockholders equity", "shareholders' equity")),
    ("income_statement", ("income statement", "revenue", "profit", "income",
                           "expenses", "p&l", "profit and loss", "profit & loss")),
    ("cash_flow", ("cash flow", "cashflow")),
    ("kpi", ("ratio", "margin", "kpi", "key performance indicator")),
    ("comparison", (" vs ", " vs. ", "budget", "actual", "variance", "comparison")),
]
_VALID_CATEGORIES = frozenset(
    {c for c, _ in _CATEGORY_KEYWORDS} | {"other"}
)

# ---------------------------------------------------------------------------
# detected_units
# ---------------------------------------------------------------------------

_UNIT_HINTS: list[str] = [
    "usd millions", "usd billions", "usd thousands",
    "in millions", "in billions", "in thousands",
    "millions", "billions", "thousands",
    "per share", "%",
]


def _safe_str(value) -> str:
    return "" if value is None else str(value)


def _flatten_cells(rows: list[list[str]] | None) -> list[str]:
    if not rows:
        return []
    return [_safe_str(cell) for row in rows for cell in (row or [])]


def _corpus(caption: str | None, headers: list[str] | None, rows: list[list[str]] | None) -> str:
    """Flat lowercase text blob of caption + headers + all cells, used for the
    keyword/regex scans below."""
    parts: list[str] = []
    if caption:
        parts.append(_safe_str(caption))
    if headers:
        parts.extend(_safe_str(h) for h in headers)
    parts.extend(_flatten_cells(rows))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# individual field derivations (rules)
# ---------------------------------------------------------------------------


def _derive_currency(corpus: str) -> str | None:
    m = _CURRENCY_SYMBOL_RE.search(corpus)
    if m:
        return _CURRENCY_SYMBOL_TO_CODE[m.group(0)]
    m = _CURRENCY_CODE_RE.search(corpus)
    if m:
        return m.group(1).upper()
    return None


def _normalize_fy(raw_digits: str) -> str:
    """Normalize FY digit group to 'FY20XX' — 2-digit years assumed 2000s
    (e.g. '24' -> '2024'); 4-digit years passed through."""
    if len(raw_digits) == 2:
        return f"FY20{raw_digits}"
    return f"FY{raw_digits}"


def _derive_fiscal_year(corpus: str) -> str | None:
    m = _FY_RE.search(corpus)
    if m:
        return _normalize_fy(m.group(1))
    m = _BARE_YEAR_NEAR_FISCAL_RE.search(corpus)
    if m:
        year = m.group(1) or m.group(2)
        if year:
            return f"FY{year}"
    return None


def _derive_reporting_period(corpus: str) -> str | None:
    m = _QUARTER_RE.search(corpus)
    if m:
        q, year = m.group(1), m.group(2)
        return f"Q{q} {year}".strip() if year else f"Q{q}"
    m = _HALF_RE.search(corpus)
    if m:
        h, year = m.group(1), m.group(2)
        return f"H{h} {year}".strip() if year else f"H{h}"
    if _YTD_RE.search(corpus):
        return "YTD"
    m = _MONTH_RE.search(corpus)
    if m:
        return m.group(1).title()
    if _ANNUAL_RE.search(corpus):
        return "annual"
    if _QUARTER_WORD_RE.search(corpus):
        return "quarterly"
    return None


def _derive_table_category(caption: str | None, headers: list[str] | None) -> str:
    """Keyword-classify from caption + headers only (not cell values — category
    is a structural/topical signal, not a data signal)."""
    text = " ".join(
        _safe_str(x) for x in ([caption] if caption else []) + list(headers or [])
    ).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in text for kw in keywords):
            return category
    return "other"


def _derive_units(corpus_lower: str) -> list[str] | None:
    found = [hint for hint in _UNIT_HINTS if hint in corpus_lower]
    if not found:
        return None
    # De-dupe while preserving first-seen order; prefer the more specific
    # "usd millions"-style hints over the bare "millions" if both matched the
    # same magnitude word (keep both — caller may want granularity signals).
    seen = set()
    ordered = []
    for hint in found:
        if hint not in seen:
            seen.add(hint)
            ordered.append(hint)
    return ordered


def _build_table_summary(caption: str | None, headers: list[str] | None, nrows: int, ncols: int) -> str:
    title = caption or "Table"
    cols_preview = ", ".join(_safe_str(h) for h in (headers or [])[:12])
    summary = f"{title} — {nrows} rows × {ncols} columns."
    if cols_preview:
        summary += f" Columns: {cols_preview}"
    return summary


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def _empty_result(caption: str | None = None, headers: list[str] | None = None,
                   rows: list[list[str]] | None = None) -> dict:
    """All-None fail-open result, but table_summary is always non-empty."""
    nrows = len(rows) if rows else 0
    ncols = len(headers) if headers else 0
    return {
        "fiscal_year": None,
        "reporting_period": None,
        "currency": None,
        "table_category": "other",
        "detected_units": None,
        "table_summary": _build_table_summary(caption, headers, nrows, ncols),
    }


def enrich_table(
    headers: list[str] | None,
    rows: list[list[str]] | None,
    caption: str | None = None,
    vlm_meta: dict | None = None,
) -> dict:
    """Derive the 6 table_store metadata columns for one table.

    Rules-first, VLM-assisted: when ``vlm_meta`` carries a non-empty value for
    a field, that value wins (it came from an actual visual read of the table
    by the VLM during crop reconstruction — no extra network call, just reuse).
    Otherwise the value is derived from the table's own headers/rows/caption
    via regex/keyword heuristics.

    Parameters
    ----------
    headers, rows:
        The table's own cells (Docling-extracted or VLM-reconciled — caller's
        choice, this function does not care which).
    caption:
        Optional table title/caption string.
    vlm_meta:
        Optional dict that may carry any of: fiscal_year, currency,
        reporting_period, table_category, units (str or list[str]). Any other
        keys are ignored. Falsy/empty-string values are treated as absent so
        the rules fallback still applies.

    Returns
    -------
    dict with keys: fiscal_year, reporting_period, currency, table_category,
    detected_units, table_summary. table_summary is always a non-empty string;
    every other key may be None (table_category defaults to 'other' rather
    than None since it's a closed enum with an explicit catch-all bucket).

    Never raises — any internal error is swallowed and a fail-open dict
    (all None + a best-effort table_summary) is returned instead.
    """
    headers = headers or []
    rows = rows or []
    vlm_meta = vlm_meta or {}

    try:
        corpus = _corpus(caption, headers, rows)
        corpus_lower = corpus.lower()

        vlm_currency = vlm_meta.get("currency") or None
        vlm_fiscal_year = vlm_meta.get("fiscal_year") or None
        vlm_reporting_period = vlm_meta.get("reporting_period") or None
        vlm_table_category = vlm_meta.get("table_category") or None
        vlm_units = vlm_meta.get("units") or None

        currency = vlm_currency or _derive_currency(corpus)
        fiscal_year = vlm_fiscal_year or _derive_fiscal_year(corpus)
        reporting_period = vlm_reporting_period or _derive_reporting_period(corpus)

        if vlm_table_category and vlm_table_category in _VALID_CATEGORIES:
            table_category = vlm_table_category
        elif vlm_table_category:
            # VLM returned something outside the closed enum — fall back to
            # rules rather than writing an invalid category value.
            table_category = _derive_table_category(caption, headers)
        else:
            table_category = _derive_table_category(caption, headers)

        if vlm_units:
            detected_units = [vlm_units] if isinstance(vlm_units, str) else list(vlm_units)
        else:
            detected_units = _derive_units(corpus_lower)

        table_summary = _build_table_summary(caption, headers, len(rows), len(headers))

        return {
            "fiscal_year": fiscal_year,
            "reporting_period": reporting_period,
            "currency": currency,
            "table_category": table_category,
            "detected_units": detected_units,
            "table_summary": table_summary,
        }
    except Exception as exc:  # fail-open — never let enrichment crash the ingest pipeline
        logger.warning("enrich_table failed (non-fatal): %s", exc)
        return _empty_result(caption, headers, rows)
