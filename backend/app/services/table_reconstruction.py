"""VLM-based reconstruction of Docling-extracted table crops (Task 1 / Slice 2b).

Docling's TableFormer often misses merged cells, multi-line cells, borders, and
partially-visible values. This module re-runs the multimodal VLM on each table
CROP image (passing the original image + Docling's text as OCR evidence).

Slice 2b — numeric-faithfulness gate (design Section 10)
----------------------------------------------------------
Blindly trusting the VLM is dangerous for financial tables: VLMs can silently
alter or hallucinate numbers. Instead of always preferring the VLM when it
returns a parseable table, ``reconcile_table`` decides, per table, whether the
VLM output is trustworthy enough to become canonical:

1. No usable VLM table -> keep Docling (``pdf_grid``).
2. Usable VLM table -> run the faithfulness gate: every numeric token the VLM
   introduces must be traceable to the reference (Docling rows + crop OCR
   text), within a small tolerance. A VLM that invents numbers is rejected.
3. A VLM that passes faithfulness only becomes canonical when the Docling grid
   is empty/ragged (the VLM is the only usable source) or genuinely ambiguous.
   A well-formed native Docling grid is preferred for fidelity even when the
   VLM agrees — Docling's text-layer extraction has zero OCR error.

Only mutate the ``ExtractedTable`` (headers/rows/markdown_text) when the
decision is VLM-canonical; ``raw_text`` is always preserved untouched for audit.

Fail-open throughout: any error in reconciliation falls back to keeping the
Docling extraction — ingestion never crashes because of this module.

Pure/injectable (``analyze_fn`` is passed in) so it unit-tests without a real VLM.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json|JSON)?|~~~|`")

# Currency/percent/thousands-separator symbols stripped before numeric comparison.
_NUMERIC_STRIP_CHARS = "$€£₹%, "

# Matches numbers with optional leading currency symbol, thousands separators,
# and a decimal part, e.g. "$1,300.50", "1300", "12.5%", "(1,200)".
_NUMERIC_TOKEN_RE = re.compile(
    r"[$€£₹]?\(?-?\d[\d,]*(?:\.\d+)?\)?%?"
)

# Faithfulness gate tolerance: allow up to this fraction of "unseen" VLM numbers
# before rejecting the VLM output outright.
_FAITHFULNESS_TOLERANCE = 0.2

# Middle-tier tolerance for tables Docling itself flagged as containing
# genuine merged cells (row_span/col_span > 1 on table.data.table_cells — see
# document_parser._detect_merged_cells). These are NOT "ragged" in the sense
# _rows_consistent_width can see (Docling's own `grid` property duplicates a
# spanned cell's text into every covered position, so the row grid this gate
# receives is always uniform width even when spans are present). Historically
# that meant a merged-cell table was silently treated exactly like a clean,
# well-formed grid and got the STRICT (zero-tolerance) rule — backwards, since
# merged-cell layouts are exactly where Docling's own extraction is weakest
# (a spanned value can be mis-attributed to the wrong sub-row/column) and VLM
# hallucination risk is highest. This tier sits strictly between the two:
# tighter than the fully-loose 20% (that bucket is for genuinely empty/ragged
# grids with no reliable structure at all), looser than zero-tolerance (the
# Docling grid still has real duplicated-but-correct values worth protecting).
_MERGED_CELL_TOLERANCE = 0.08


def parse_vlm_table(structured_content: str) -> Optional[tuple[Optional[str], list[str], list[list[str]]]]:
    """Extract (title, headers, rows) from a VLM structured_content blob.

    Returns None when the content is not a usable table (not JSON, not a dict, or
    no data rows) — the caller keeps the original Docling extraction.
    """
    if not structured_content:
        return None
    txt = _FENCE_RE.sub("", structured_content).strip()
    try:
        data = json.loads(txt)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    norm_rows = [[str(c) for c in r] for r in rows if isinstance(r, list) and r]
    if not norm_rows:
        return None
    headers_raw = data.get("headers") or []
    headers = [str(h) for h in headers_raw] if isinstance(headers_raw, list) else []
    title = str(data.get("title") or "").strip() or None
    return title, headers, norm_rows


def rows_to_markdown(headers: list[str], rows: list[list[str]]) -> str:
    """Render a pipe-delimited markdown table (header row optional)."""
    lines: list[str] = []
    if headers:
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# ── Slice 2b: numeric-faithfulness gate ─────────────────────────────────────


def _normalize_numeric_token(tok: str) -> Optional[str]:
    """Normalize a raw numeric token for comparison: strip currency/percent/
    thousands-separator noise, drop parens-as-negative markers, collapse a
    trailing ".0". Returns None if the cleaned token isn't actually numeric.
    """
    cleaned = tok.strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    for ch in _NUMERIC_STRIP_CHARS:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if negative:
        val = -abs(val)
    # Normalize "1300.0" and "1300" to the same comparable form.
    if val == int(val):
        return str(int(val))
    return f"{val:g}"


def _numeric_tokens(text_or_cells) -> set[str]:
    """Extract the set of normalized numeric tokens from a string, or from a
    nested list of cells/rows (any depth of list-of-strings). Non-numeric text
    is ignored. Used both for VLM output and for the Docling+OCR reference.
    """
    if text_or_cells is None:
        return set()

    pieces: list[str] = []
    if isinstance(text_or_cells, str):
        pieces = [text_or_cells]
    elif isinstance(text_or_cells, (list, tuple)):
        def _flatten(x):
            if isinstance(x, (list, tuple)):
                for item in x:
                    _flatten(item)
            elif x is not None:
                pieces.append(str(x))
        _flatten(text_or_cells)
    else:
        pieces = [str(text_or_cells)]

    tokens: set[str] = set()
    for piece in pieces:
        for raw in _NUMERIC_TOKEN_RE.findall(piece):
            norm = _normalize_numeric_token(raw)
            if norm is not None:
                tokens.add(norm)
    return tokens


def _grid_wellformed(headers: list[str] | None, rows: list[list[str]] | None) -> bool:
    """A Docling grid is 'well-formed' when it has at least one header, at
    least one data row, and consistent column counts across all rows (and,
    when headers are present, matching the header width)."""
    if not rows:
        return False
    if not headers:
        return False
    width = len(headers)
    if width == 0:
        return False
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != width:
            return False
    return True


def _rows_consistent_width(rows: list[list[str]] | None) -> bool:
    """True when ``rows`` is non-empty and every row has the same column
    count (a weaker, headers-agnostic form of well-formedness used by the
    faithfulness gate, which only sees the row grid, not headers)."""
    if not rows:
        return False
    width = len(rows[0]) if isinstance(rows[0], (list, tuple)) else 0
    if width == 0:
        return False
    return all(isinstance(r, (list, tuple)) and len(r) == width for r in rows)


def faithfulness_ok(
    vlm_rows: list[list[str]],
    reference_text: str,
    docling_rows: list[list[str]] | None,
    has_merged_cells: bool = False,
) -> tuple[bool, list[str]]:
    """Faithfulness gate: check whether the VLM's numeric tokens are all
    traceable to the reference (Docling rows unioned with crop OCR text).

    Returns (passed, issues). ``issues`` is empty when passed is True.

    Three tolerance tiers, from strictest to loosest:
      1. Well-formed Docling grid, NO detected merged cells -> STRICT. ANY
         unseen number rejects the VLM (a clean native grid is the strongest,
         most trustworthy reference).
      2. Well-formed Docling grid (uniform row widths) but Docling itself
         reported merged/spanned cells (``has_merged_cells=True``) -> MIDDLE
         tier (``_MERGED_CELL_TOLERANCE``, 8%). Docling's dense grid always
         reports uniform widths even when cells are spanned (a spanned
         value's text is duplicated into every covered position by Docling's
         own `grid` property), so a merged-cell table looks "well-formed" to
         ``_rows_consistent_width`` while actually being the shape Docling's
         extraction handles least reliably — exactly where VLM hallucination
         risk is highest. It gets more scrutiny than a clean grid, but not
         the zero-tolerance rule, since a legitimately-duplicated value can
         make a faithful VLM re-transcription look like it "repeats" a number
         in a way that superficially resembles hallucination.
      3. Docling grid empty/ragged, no merged-cell signal -> LOOSE tier
         (``_FAITHFULNESS_TOLERANCE``, 20%) — no reliable native structure to
         check against at all.
    """
    vlm_numbers = _numeric_tokens(vlm_rows)
    if not vlm_numbers:
        # Nothing numeric to verify — treat as faithful (e.g. purely textual table).
        return True, []

    reference_numbers = _numeric_tokens(docling_rows) | _numeric_tokens(reference_text)
    unseen = vlm_numbers - reference_numbers
    if not unseen:
        return True, []

    docling_wellformed = _rows_consistent_width(docling_rows)
    unseen_fraction = len(unseen) / len(vlm_numbers)

    if docling_wellformed and not has_merged_cells:
        issues = [
            f"VLM introduced {len(unseen)} number(s) not in reference "
            f"(well-formed Docling grid present): {sorted(unseen)[:5]}"
        ]
        return False, issues

    if docling_wellformed and has_merged_cells:
        if unseen_fraction > _MERGED_CELL_TOLERANCE:
            issues = [
                f"VLM introduced {len(unseen)}/{len(vlm_numbers)} "
                f"({unseen_fraction:.0%}) numeric token(s) not found in reference "
                f"(merged-cell Docling grid, {_MERGED_CELL_TOLERANCE:.0%} tolerance): "
                f"{sorted(unseen)[:5]}"
            ]
            return False, issues
        return True, []

    if unseen_fraction > _FAITHFULNESS_TOLERANCE:
        issues = [
            f"VLM introduced {len(unseen)}/{len(vlm_numbers)} "
            f"({unseen_fraction:.0%}) numeric token(s) not found in reference: "
            f"{sorted(unseen)[:5]}"
        ]
        return False, issues

    return True, []


def reconcile_table(
    docling_headers: list[str] | None,
    docling_rows: list[list[str]] | None,
    ocr_text: str,
    vlm_parsed: Optional[tuple[Optional[str], list[str], list[list[str]]]],
    has_merged_cells: bool = False,
) -> dict:
    """Decide whether the VLM or Docling extraction should be canonical for a
    single table. Pure — no VLM calls, no DB, no ExtractedTable mutation.

    Returns a dict:
      {
        "method": "pdf_grid" | "image_vlm",
        "quality": "high" | "medium" | "low" | None,
        "confidence": float | None,          # VLM confidence, only when VLM canonical
        "canonical_headers": list[str],
        "canonical_rows": list[list[str]],
        "use_vlm": bool,
        "issues": list[str],
      }
    """
    docling_headers = docling_headers or []
    docling_rows = docling_rows or []
    docling_wellformed = _grid_wellformed(docling_headers, docling_rows)

    # Rule 1: no usable VLM table -> keep Docling.
    if vlm_parsed is None:
        return {
            "method": "pdf_grid",
            "quality": "high" if docling_wellformed else ("medium" if docling_rows else "low"),
            "confidence": None,
            "canonical_headers": docling_headers,
            "canonical_rows": docling_rows,
            "use_vlm": False,
            "issues": [],
        }

    _title, vlm_headers, vlm_rows = vlm_parsed

    # Rule 2: faithfulness gate.
    passed, issues = faithfulness_ok(vlm_rows, ocr_text or "", docling_rows, has_merged_cells)
    if not passed:
        return {
            "method": "pdf_grid",
            "quality": "low",
            "confidence": None,
            "canonical_headers": docling_headers,
            "canonical_rows": docling_rows,
            "use_vlm": False,
            "issues": issues,
        }

    # Rule 3: faithful VLM — decide canonical source.
    if not docling_rows or not docling_wellformed:
        # Docling grid empty or ragged -> VLM is the only good source.
        vlm_wellformed = _grid_wellformed(vlm_headers, vlm_rows)
        return {
            "method": "image_vlm",
            "quality": "high" if vlm_wellformed else "medium",
            "confidence": None,  # filled in by the caller with the real VLM confidence
            "canonical_headers": vlm_headers,
            "canonical_rows": vlm_rows,
            "use_vlm": True,
            "issues": [],
        }

    # Docling grid is well-formed on a table that passed faithfulness ->
    # prefer Docling for fidelity (text-layer, zero OCR error). VLM agreed,
    # which is recorded in provenance by the caller.
    return {
        "method": "pdf_grid",
        "quality": "high",
        "confidence": None,
        "canonical_headers": docling_headers,
        "canonical_rows": docling_rows,
        "use_vlm": False,
        "issues": [],
    }


def reconstruct_tables_with_vlm(
    parsed_doc,
    analyze_fn: Optional[Callable[[bytes, str], dict]] = None,
    max_workers: int = 1,
) -> dict[int, dict]:
    """Run the VLM on every table crop with a rendered image; reconcile the VLM
    output against the Docling baseline via ``reconcile_table`` and mutate the
    matching ExtractedTable in place ONLY when the reconciliation decides the
    VLM should be canonical.

    ``analyze_fn(png_bytes, ocr_text) -> analysis dict`` defaults to the real
    image_analysis_service.analyze_image (lazy import). Bounded-parallel via
    ``max_workers`` (>1). Returns {table_index: analysis dict} for every crop the
    VLM was run on (so callers can also enrich the image_store repo row).

    Each analysis dict retains the original VLM keys (structured_content,
    vlm_ocr_text, confidence, ...) and is additionally enriched with:
      method            "pdf_grid" | "image_vlm"
      extraction_quality "high" | "medium" | "low" | None
      confidence         float | None (VLM confidence, only set when VLM canonical)
      provenance         {"reconciled": bool, "issues": [...], "baseline_source": "docling",
                           "use_vlm": bool, "vlm_agreed": bool}
    """
    targets = [t for t in getattr(parsed_doc, "tables", []) if getattr(t, "image_png_bytes", None)]
    if not targets:
        return {}

    if analyze_fn is None:
        from app.services.image_analysis_service import analyze_image as analyze_fn  # type: ignore

    def _one(t) -> tuple[int, dict]:
        try:
            return t.table_index, analyze_fn(t.image_png_bytes, t.raw_text or "")
        except Exception as exc:  # fail-safe per table
            logger.warning("table-crop VLM failed (table %s): %s", t.table_index, exc)
            return t.table_index, {}

    workers = max(1, min(int(max_workers or 1), len(targets)))
    if workers <= 1:
        results = [_one(t) for t in targets]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = [f.result() for f in as_completed([ex.submit(_one, t) for t in targets])]

    by_index = {t.table_index: t for t in targets}
    analyses: dict[int, dict] = {}
    reconstructed = 0
    for tidx, analysis in results:
        analysis = dict(analysis or {})
        t = by_index[tidx]

        # Capture the Docling baseline BEFORE any mutation.
        docling_headers = list(t.headers or [])
        docling_rows = [list(r) for r in (t.rows or [])]

        try:
            vlm_parsed = parse_vlm_table(analysis.get("structured_content", ""))
        except Exception as exc:
            logger.warning("table-crop VLM parse failed (table %s): %s", tidx, exc)
            vlm_parsed = None

        # Merged-cell signal (document_parser._detect_merged_cells), fail-open
        # to False when table_metadata is absent/malformed (e.g. test doubles
        # in existing suites that don't set this attribute at all).
        try:
            has_merged_cells = bool(
                (getattr(t, "table_metadata", None) or {}).get("merged_cells", {}).get("has_merged_cells")
            )
        except Exception:
            has_merged_cells = False

        try:
            decision = reconcile_table(
                docling_headers, docling_rows, t.raw_text or "", vlm_parsed, has_merged_cells,
            )
        except Exception as exc:  # fail-open: any reconciliation error keeps Docling
            logger.warning("table-crop reconciliation failed (table %s): %s", tidx, exc)
            decision = {
                "method": "pdf_grid",
                "quality": None,
                "confidence": None,
                "canonical_headers": docling_headers,
                "canonical_rows": docling_rows,
                "use_vlm": False,
                "issues": [f"reconciliation error: {exc}"],
            }

        vlm_agreed = vlm_parsed is not None and not decision["issues"]
        if decision["use_vlm"]:
            headers = decision["canonical_headers"]
            rows = decision["canonical_rows"]
            if headers:
                t.headers = headers
            t.rows = rows
            t.markdown_text = rows_to_markdown(t.headers, rows)
            title = vlm_parsed[0] if vlm_parsed else None
            if title and not t.caption:
                t.caption = title
            reconstructed += 1
            confidence = analysis.get("confidence")
        else:
            confidence = None

        analysis["method"] = decision["method"]
        analysis["extraction_quality"] = decision["quality"]
        analysis["confidence"] = confidence
        analysis["provenance"] = {
            "reconciled": True,
            "use_vlm": decision["use_vlm"],
            "vlm_agreed": vlm_agreed,
            "issues": decision["issues"],
            "baseline_source": "docling",
            "source": "docling+vlm" if vlm_parsed is not None else "docling",
        }
        analyses[tidx] = analysis

    logger.info(
        "[%s] table-crop VLM: reconciled %d/%d crops (%d chose VLM canonical, rest kept Docling)",
        getattr(parsed_doc, "doc_id", "?"), len(targets), len(targets), reconstructed,
    )
    return analyses
