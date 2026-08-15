"""Exact structured table query engine (Phase 2, additive).

Runs ALONGSIDE the existing semantic retrieval pipeline — never replaces it.
``try_structured_query`` is a fast, deterministic, rule-based classifier +
executor: it recognizes a small set of exact aggregate/lookup intents over
`table_store.json_data` ({"headers": [...], "rows": [[...]]}) and, when
recognized, computes an EXACT answer directly from the stored table data
(post Phase-1 continuation-merge, so one logical table's json_data already
spans all of its original pages — a SUM here sums across the whole merged
table for free).

When the query does NOT match a recognized intent (or matching fails for any
reason — no candidate tables, unknown column, malformed data, DB error), this
module returns None and the caller falls through to the existing semantic
retrieval + synthesis path, completely unchanged.

No LLM call is used here — everything is regex/keyword matching over the
query text and plain Python arithmetic over already-stored JSON, so this is
safe to run on every query without adding meaningful latency.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app.services.table_reconstruction import _normalize_numeric_token

logger = logging.getLogger(__name__)


# ── Intent detection ────────────────────────────────────────────────────────

_SUM_RE = re.compile(r"\b(total|sum(?:\s+of)?)\b", re.IGNORECASE)
_AVG_RE = re.compile(r"\b(average|mean)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(how many|count(?:\s+of)?|number of)\b", re.IGNORECASE)
_MIN_RE = re.compile(r"\b(lowest|minimum|smallest|min)\b", re.IGNORECASE)
_MAX_RE = re.compile(r"\b(highest|maximum|largest|max)\b", re.IGNORECASE)

# Ordered so a more specific/earlier match wins when a query could plausibly
# hit more than one keyword set (e.g. "average" also matching nothing else).
_AGGREGATE_PATTERNS = [
    ("SUM", _SUM_RE),
    ("AVG", _AVG_RE),
    ("COUNT", _COUNT_RE),
    ("MIN", _MIN_RE),
    ("MAX", _MAX_RE),
]

# "what is the Actual value for Marketing in Month-05" style exact row lookup:
# "<value column> ... for/of/where <filter value>"
_ROW_LOOKUP_RE = re.compile(
    r"\bwhat\s+is\s+the\s+(.+?)\s+(?:value\s+)?(?:for|of|where)\s+(.+?)\s*[\?\.]?$",
    re.IGNORECASE,
)

# Attribute-lookup phrasing: "which <attr> does <entity> belong to",
# "what sector is <entity> in", "which industry does <entity> operate in".
# Same capture contract as _ROW_LOOKUP_RE: group(1)=value column (the asked
# attribute), group(2)=filter value (the row-identifying entity). A trailing
# preposition (to/under/in/...) is REQUIRED so this never swallows ranking/
# comparison shapes ("which sector is the largest") that have no relational
# tail; the belonging verb (belong/fall/operate/...) is optional.
_ATTR_LOOKUP_RE = re.compile(
    r"\b(?:which|what)\s+(.+?)\s+(?:do(?:es)?|is|are|did)\s+(.+?)"
    r"\s+(?:(?:belong|fall|come|classif\w*|categor\w*|includ\w*|plac\w*|"
    r"group\w*|operat\w*|list\w*)\s*)?"
    r"(?:to|under|in|into|as|within)\s*[\?\.]?$",
    re.IGNORECASE,
)

# Natural subject-first variants: "HDFC Bank is what sector?" and
# "HDFC Bank belongs to which sector?". These are normalized to the
# canonical attribute-first form before row lookup.
_SUBJECT_FIRST_ATTR_LOOKUP_RE = re.compile(
    r"^\s*(.+?)\s+(?:(?:is\s+)?belong\w*\s+to|is\s+(?:in|under|within))\s+"
    r"(?:which|what)\s+(.+?)\s*[\?\.]?$|"
    r"^\s*(.+?)\s+is\s+(?:which|what)\s+(.+?)\s*[\?\.]?$",
    re.IGNORECASE,
)


def _canonicalize_subject_first_lookup(query: str) -> str:
    """Rewrite subject-first attribute questions for the existing lookup parser."""
    match = _SUBJECT_FIRST_ATTR_LOOKUP_RE.search(query.strip())
    if not match:
        return query
    entity = (match.group(1) or match.group(3) or "").strip()
    attribute = (match.group(2) or match.group(4) or "").strip()
    return f"which {attribute} is {entity} in?"

# "list all companies in the chemical sector" / "show all rows where region is
# East" / "which companies are in Banking" style: unlike _ROW_LOOKUP_RE this
# asks for EVERY matching row, not the single best one. Without this intent,
# such queries fell through to plain semantic top-k retrieval, which silently
# truncates to whatever chunks happened to rank in the top_k — for a table
# with rows spread across many chunks, that means an incomplete list with no
# indication anything was left out.
_LIST_RE = re.compile(
    r"\b(list|show|display|enumerate)\b.*\b(all|every|out)\b|\bwhich\b.+\bare\b|\bwhat\s+are\b",
    re.IGNORECASE,
)
_LIST_FILTER_RE = re.compile(
    r"\b(?:in|from|under|within|belonging\s+to)\s+(?:the\s+)?(.+?)"
    r"(?:\s+sector|\s+category|\s+industry|\s+segment)?\s*[\?\.]?$",
    re.IGNORECASE,
)


def _detect_aggregate(query: str) -> Optional[str]:
    for op, pattern in _AGGREGATE_PATTERNS:
        if pattern.search(query):
            return op
    return None


# ── Column name normalization / fuzzy matching ──────────────────────────────

def _normalize_label(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — used for fuzzy
    column-name / cell-value matching (case/whitespace/punctuation-insensitive)."""
    if s is None:
        return ""
    s = str(s).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _fuzzy_find_column(target: str, headers: list[str]) -> Optional[int]:
    """Find the header index that best matches `target`, tolerating case/
    whitespace/punctuation differences and substring matches in either
    direction. Returns None when nothing plausible matches."""
    norm_target = _normalize_label(target)
    if not norm_target or not headers:
        return None

    norm_headers = [_normalize_label(h) for h in headers]

    # Exact normalized match first.
    for i, nh in enumerate(norm_headers):
        if nh == norm_target:
            return i

    # Substring match (either direction) — e.g. "actual" vs "actual usd ($)".
    best_idx: Optional[int] = None
    best_len = -1
    for i, nh in enumerate(norm_headers):
        if not nh:
            continue
        if norm_target in nh or nh in norm_target:
            match_len = min(len(norm_target), len(nh))
            if match_len > best_len:
                best_len = match_len
                best_idx = i
    if best_idx is not None:
        return best_idx

    # Token-overlap match — e.g. target "actual value" vs header "actual".
    target_tokens = set(norm_target.split())
    if target_tokens:
        best_idx = None
        best_overlap = 0
        for i, nh in enumerate(norm_headers):
            header_tokens = set(nh.split())
            overlap = len(target_tokens & header_tokens)
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = i
        if best_overlap > 0:
            return best_idx

    return None


def _extract_target_column(query: str, headers: list[str]) -> Optional[int]:
    """Best-effort extraction of the column the query is asking about, by
    fuzzy-matching every plausible substring of the query against the actual
    header names. Strategy: try progressively shorter windows of words from
    the query and keep the best header match found."""
    norm_headers = [_normalize_label(h) for h in headers]
    if not any(norm_headers):
        return None

    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", query)
    best_idx: Optional[int] = None
    best_score = -1

    # Try each header directly against the whole query first (handles
    # multi-word headers like "Actual USD" appearing verbatim in the query).
    for i, nh in enumerate(norm_headers):
        if not nh:
            continue
        if nh in _normalize_label(query):
            score = len(nh)
            if score > best_score:
                best_score = score
                best_idx = i

    if best_idx is not None:
        return best_idx

    # Fall back to sliding windows of query words fuzzy-matched per header.
    n = len(words)
    for size in range(min(4, n), 0, -1):
        for start in range(0, n - size + 1):
            window = " ".join(words[start:start + size])
            idx = _fuzzy_find_column(window, headers)
            if idx is not None:
                return idx

    return None


# ── Data access (mirrors retriever_service.TableFilters filter pattern) ────

def _fetch_candidate_tables(
    document_id: Optional[str],
    document_types: Optional[list],
) -> list[dict]:
    """Fetch candidate table_store rows (id, document_id, json_data, ...),
    filtered by document_id/document_types exactly like retriever_service's
    _doc_filter / _type_filter helpers (mirrored here, not imported, to keep
    this module independent of retriever_service's internals per the task's
    file-ownership boundaries)."""
    from app.db.connection import get_db

    clauses = ["dr.status = 'completed'", "ts.json_data IS NOT NULL"]
    params: list = []

    if document_id:
        clauses.append("ts.document_id = %s")
        params.append(document_id)

    if document_types:
        placeholders = ", ".join(["%s"] * len(document_types))
        clauses.append(f"dr.document_type IN ({placeholders})")
        params.extend(document_types)

    where_sql = " AND ".join(clauses)
    sql = f"""
        SELECT
            ts.id::text, ts.document_id::text, ts.json_data,
            ts.table_title, ts.table_category, ts.currency, ts.fiscal_year,
            dr.original_filename, dr.storage_path, dr.storage_bucket,
            ts.page_number, ts.table_metadata
        FROM multi_store_rag_working.table_store ts
        JOIN multi_store_rag_working.document_registry dr ON dr.id = ts.document_id
        WHERE {where_sql}
    """

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    tables = []
    for r in rows:
        json_data = r[2]
        if isinstance(json_data, str):
            try:
                import json as _json
                json_data = _json.loads(json_data)
            except Exception:
                continue
        if not isinstance(json_data, dict):
            continue
        headers = json_data.get("headers") or []
        data_rows = json_data.get("rows") or []
        if not isinstance(headers, list) or not isinstance(data_rows, list):
            continue
        table_metadata = r[11] or {}
        if isinstance(table_metadata, str):
            try:
                table_metadata = json.loads(table_metadata)
            except Exception:
                table_metadata = {}
        row_page_numbers = table_metadata.get("row_page_numbers") if isinstance(table_metadata, dict) else None
        if isinstance(row_page_numbers, str):
            try:
                row_page_numbers = json.loads(row_page_numbers)
            except Exception:
                row_page_numbers = None
        if isinstance(row_page_numbers, list):
            # JSONB can contain numeric strings after older ingestion runs.
            # Normalize them once so citation generation never silently falls
            # back to the table's representative (first) page.
            normalized_pages = []
            for page in row_page_numbers:
                try:
                    normalized_pages.append(int(page) if page is not None else None)
                except (TypeError, ValueError):
                    normalized_pages.append(None)
            row_page_numbers = normalized_pages

        tables.append({
            "table_id": r[0],
            "document_id": r[1],
            "headers": headers,
            "rows": data_rows,
            "table_title": r[3],
            "table_category": r[4],
            "currency": r[5],
            "fiscal_year": r[6],
            "filename": r[7],
            "pdf_storage_path": r[8],
            "pdf_bucket": r[9],
            "page_number": r[10],
            "row_page_numbers": row_page_numbers,
        })
    return tables


# ── Numeric parsing (mirrors table_reconstruction._numeric_tokens conventions) ─

def _parse_cell_numeric(cell) -> Optional[float]:
    """Parse a single cell value to float, reusing table_reconstruction's
    currency/percentage/comma-stripping/parens-negative conventions
    (_normalize_numeric_token). Returns None when the cell isn't numeric."""
    if cell is None:
        return None
    norm = _normalize_numeric_token(str(cell))
    if norm is None:
        return None
    try:
        return float(norm)
    except ValueError:
        return None


# ── Structured query execution ──────────────────────────────────────────────

def _run_aggregate(operation: str, table: dict, col_idx: int) -> Optional[dict]:
    headers = table["headers"]
    rows = table["rows"]
    column_name = headers[col_idx]

    values: list[float] = []
    unparseable = 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or col_idx >= len(row):
            unparseable += 1
            continue
        val = _parse_cell_numeric(row[col_idx])
        if val is None:
            unparseable += 1
        else:
            values.append(val)

    if operation == "COUNT":
        # COUNT is a row-count style question — count all rows considered,
        # numeric parseability isn't required for a plain "how many rows".
        value = len(rows)
    else:
        if not values:
            return None
        if operation == "SUM":
            value = sum(values)
        elif operation == "AVG":
            value = sum(values) / len(values)
        elif operation == "MIN":
            value = min(values)
        elif operation == "MAX":
            value = max(values)
        else:
            return None

    return {
        "operation": operation,
        "column": column_name,
        "value": value,
        "matched_table_ids": [table["table_id"]],
        "row_count_considered": len(rows),
        "unparseable_count": unparseable,
        "filter_description": f"{operation}({column_name}) across all rows of table "
                               f"{table.get('table_title') or table['table_id']}",
        "table_title": table.get("table_title"),
        "document_id": table["document_id"],
        "filename": table.get("filename"),
        "pdf_storage_path": table.get("pdf_storage_path"),
        "pdf_bucket": table.get("pdf_bucket"),
        "page_number": table.get("page_number"),
    }


def _run_row_lookup(query: str, table: dict) -> Optional[dict]:
    """Handle 'what is the <value col> for/of/where <filter value>' style
    exact row filtering + column lookup."""
    lookup_query = _canonicalize_subject_first_lookup(query)
    m = _ROW_LOOKUP_RE.search(lookup_query.strip()) or _ATTR_LOOKUP_RE.search(lookup_query.strip())
    if not m:
        return None

    value_col_text, filter_text = m.group(1).strip(), m.group(2).strip()
    headers = table["headers"]
    rows = table["rows"]

    value_col_idx = _fuzzy_find_column(value_col_text, headers) or _extract_target_column(value_col_text, headers)
    if value_col_idx is None:
        return None

    norm_filter = _normalize_label(filter_text)
    if not norm_filter:
        return None

    # The filter text can name more than one row-identifying value spread
    # across different columns (e.g. "Month-05 in Marketing" naming both a
    # Month cell and a Region cell). Score each ROW as a whole by how many
    # distinct filter tokens it covers across ALL of its cells combined, so a
    # row matching multiple filter clauses outranks a row matching only one
    # of them (e.g. many rows contain "Marketing" but only one also contains
    # "Month-05"). Ties are broken by preferring an exact/substring cell hit.
    filter_tokens = set(norm_filter.split())
    matched_row = None
    matched_col_for_filter = None
    best_row_score = 0
    best_cell_score = 0
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue

        covered_tokens: set[str] = set()
        row_best_cell_score = 0
        row_best_cell_idx = None
        for ci, cell in enumerate(row):
            norm_cell = _normalize_label(cell)
            if not norm_cell:
                continue
            cell_tokens = set(norm_cell.split())
            covered_tokens |= (filter_tokens & cell_tokens)

            score = 0
            if norm_cell == norm_filter:
                score = 100
            elif norm_filter in norm_cell or norm_cell in norm_filter:
                score = 50
            else:
                overlap = len(filter_tokens & cell_tokens)
                if overlap:
                    score = overlap
            if score > row_best_cell_score:
                row_best_cell_score = score
                row_best_cell_idx = ci

        row_score = len(covered_tokens)
        if row_score == 0:
            continue

        if (row_score > best_row_score or
                (row_score == best_row_score and row_best_cell_score > best_cell_score)):
            best_row_score = row_score
            best_cell_score = row_best_cell_score
            matched_row = row
            matched_col_for_filter = row_best_cell_idx

    if matched_row is None or best_row_score <= 0:
        return None

    if value_col_idx >= len(matched_row):
        return None

    raw_value = matched_row[value_col_idx]
    numeric_value = _parse_cell_numeric(raw_value)
    value = numeric_value if numeric_value is not None else raw_value

    filter_col_name = headers[matched_col_for_filter] if matched_col_for_filter < len(headers) else None
    value_col_name = headers[value_col_idx]
    matched_row_index = next((i for i, candidate in enumerate(rows) if candidate is matched_row), None)
    row_pages = table.get("row_page_numbers") or []
    row_page = (
        row_pages[matched_row_index]
        if matched_row_index is not None and matched_row_index < len(row_pages)
        else None
    )
    table_page = table.get("page_number")
    source_page = row_page if row_page is not None else table_page

    # Debug logging: trace page resolution so stale page_number fallbacks are
    # immediately visible in logs instead of silently showing the wrong page.
    if row_page is not None:
        logger.debug(
            "LOOKUP page resolved via row_page_numbers[%s] = %s (table base page = %s)",
            matched_row_index, row_page, table_page,
        )
    elif row_pages:
        logger.warning(
            "LOOKUP row_page_numbers exists (%d entries) but matched_row_index=%s "
            "is out of range — falling back to table page %s",
            len(row_pages), matched_row_index, table_page,
        )
    else:
        logger.debug(
            "LOOKUP no row_page_numbers in table_metadata — using table base page %s "
            "(re-ingest the document to populate per-row page tracking)",
            table_page,
        )

    return {
        "operation": "LOOKUP",
        "column": value_col_name,
        "value": value,
        "matched_table_ids": [table["table_id"]],
        "row_count_considered": 1,
        "unparseable_count": 0 if numeric_value is not None else 1,
        "filter_description": f"{value_col_name} where {filter_col_name} matches '{filter_text}'",
        "table_title": table.get("table_title"),
        "document_id": table["document_id"],
        "filename": table.get("filename"),
        "pdf_storage_path": table.get("pdf_storage_path"),
        "pdf_bucket": table.get("pdf_bucket"),
        "page_number": source_page,
        # Debug: expose the resolution path so the frontend can show if the
        # page was resolved per-row or fell back to the table's base page.
        "matched_row_index": matched_row_index,
        "row_page_available": len(row_pages) > 0,
        "table_base_page": table_page,
    }


def _run_list_filter(query: str, table: dict) -> Optional[dict]:
    """Handle 'list/show all X where <filter>' style queries. Unlike
    _run_row_lookup this returns EVERY matching row (not just the single best
    match), so the answer is never silently truncated to whatever a top-k
    semantic retrieval happened to surface."""
    m = _LIST_FILTER_RE.search(query.strip())
    if not m:
        return None

    filter_text = m.group(1).strip()
    norm_filter = _normalize_label(filter_text)
    if len(norm_filter) < 3:
        return None

    headers = table["headers"]
    rows = table["rows"]
    if not headers or not rows:
        return None

    filter_tokens = set(norm_filter.split())

    def _cell_matches(norm_cell: str) -> bool:
        if not norm_cell:
            return False
        if norm_cell == norm_filter:
            return True
        # Bridge simple singular/plural mismatches ("chemical" query vs a
        # "Chemicals" sector value) without falling back to raw substring
        # matching — a raw `norm_filter in norm_cell` check would also match
        # "chemical" inside "agrochemicals", silently pulling in an unrelated
        # sector's rows. Token-set comparison below is word-boundary-safe for
        # the same reason: "agrochemicals" is one token, never equal to or a
        # superset containing the token "chemical".
        if norm_filter + "s" == norm_cell or norm_cell + "s" == norm_filter:
            return True
        if norm_filter + "es" == norm_cell or norm_cell + "es" == norm_filter:
            return True
        cell_tokens = set(norm_cell.split())
        return bool(filter_tokens) and filter_tokens <= cell_tokens

    # Find the column whose cell values most consistently match the filter
    # phrase (e.g. a "Sector" column full of "Chemicals"/"Banking"/... values)
    # — scored across the whole column rather than a single best cell, so a
    # column that matches many rows outranks one that coincidentally matches
    # a single unrelated cell.
    best_col_idx: Optional[int] = None
    best_col_matches = 0
    for ci in range(len(headers)):
        matches = 0
        for row in rows:
            if isinstance(row, (list, tuple)) and ci < len(row):
                if _cell_matches(_normalize_label(row[ci])):
                    matches += 1
        if matches > best_col_matches:
            best_col_matches = matches
            best_col_idx = ci

    if best_col_idx is None or best_col_matches == 0:
        return None

    matched_rows = [
        row for row in rows
        if isinstance(row, (list, tuple)) and best_col_idx < len(row)
        and _cell_matches(_normalize_label(row[best_col_idx]))
    ]
    if not matched_rows:
        return None

    filter_col_name = headers[best_col_idx]
    row_dicts = [
        {headers[i]: row[i] for i in range(len(headers)) if i < len(row)}
        for row in matched_rows
    ]

    return {
        "operation": "LIST",
        "column": filter_col_name,
        "value": row_dicts,
        "matched_table_ids": [table["table_id"]],
        "row_count_considered": len(rows),
        "matched_row_count": len(matched_rows),
        "unparseable_count": 0,
        "filter_description": f"all rows where {filter_col_name} matches '{filter_text}'",
        "table_title": table.get("table_title"),
        "document_id": table["document_id"],
        "filename": table.get("filename"),
    }


def try_structured_query(
    query: str,
    document_id: Optional[str] = None,
    document_types: Optional[list] = None,
) -> Optional[dict]:
    """Attempt to answer `query` as an exact structured table query.

    Two-tier execution:
      Tier 1 (table_sql_compiler): indexed SQL pushdown against
        table_cell_store/table_row_store (migration 019) — runs entirely
        server-side, scales past what fits comfortably in Python memory,
        supports full AND/OR/BETWEEN/IN condition trees and GROUP BY.
        Only usable for tables that have been backfilled into the cell
        store; a document_id/document_types filter that resolves to zero
        cell-store-backed tables falls through to tier 2.
      Tier 2 (this module's original engine, below): scans
        table_store.json_data in Python — always available (every ingested
        table has json_data), single-AND-condition, no GROUP BY. Kept as
        the universal fallback so a table that hasn't been backfilled yet
        (or a tier-1 query error) never silently returns nothing.

    Returns None when:
      - the query doesn't match any recognized intent, OR
      - no candidate tables are found in either tier, OR
      - a target column can't be resolved against any candidate table, OR
      - anything else goes wrong (defensive: never raises).

    Returns a dict shaped like:
      {
        "operation": "SUM" | "AVG" | "COUNT" | "MIN" | "MAX" | "LOOKUP"
                     | "LIST" | "FILTER" | "RANKING" | "GROUP_BY",
        "column": <resolved header name>,
        "value": <computed result, or list[dict] rows for FILTER/LIST/RANKING,
                  or list[{"group","value"}] for GROUP_BY>,
        "matched_table_ids": [<table_store.id>, ...],
        "row_count_considered": <int>,
        "unparseable_count": <int>,
        "filter_description": <human-readable description>,
        "table_title": <optional>,
        "document_id": <optional>,
        "filename": <optional>,
      }
    when recognized and successfully computed.
    """
    if not query or not query.strip():
        return None

    tier1_result = _try_structured_query_tier1(query, document_id, document_types)
    if tier1_result is not None:
        return tier1_result

    return _try_structured_query_tier2(query, document_id, document_types)


def _try_structured_query_tier1(
    query: str,
    document_id: Optional[str],
    document_types: Optional[list],
) -> Optional[dict]:
    """SQL-pushdown tier — see try_structured_query's docstring. Lazy
    imports of table_intent_classifier/table_condition_parser/
    table_sql_compiler avoid a circular import: those modules import
    _fuzzy_find_column/_normalize_label/_extract_target_column from THIS
    module at their own top level, so this module can't import them back
    at ITS top level without a cycle — importing inside the function body
    (executed only after both modules have finished loading) sidesteps it,
    the same pattern query.py already uses for this module."""
    try:
        from app.config import settings as _settings
        if not getattr(_settings, "TABLE_CELL_STORE_ENABLED", True):
            return None

        from app.services.table_intent_classifier import classify_table_intent
        from app.services.table_condition_parser import parse_filter
        from app.services import table_sql_compiler as _sql

        intent = classify_table_intent(query)
        if intent == "semantic_qa":
            return None

        parsed = parse_filter(query)
        max_rows = getattr(_settings, "TABLE_STRUCTURED_MAX_ROWS_INJECTED", 200)

        if intent in ("filter", "mixed", "ranking"):
            result = _sql.run_filter(
                parsed.tree, document_id=document_id, document_types=document_types,
                ranking=parsed.ranking, max_rows=max_rows,
            )
            if result is not None:
                return result
            return None

        if intent == "aggregation":
            operation = _detect_aggregate(query)
            if operation is None:
                return None

            if parsed.group_by_hint and getattr(_settings, "TABLE_GROUP_BY_ENABLED", True):
                # Strip the "by <column>" clause before column resolution so
                # the aggregate column's fuzzy match isn't misled by the
                # group column's own name appearing in the query text.
                agg_query_text = _GROUP_BY_STRIP_RE.sub("", query).strip()
                result = _sql.run_group_by(
                    parsed.group_by_hint, operation, agg_query_text, parsed.tree,
                    document_id=document_id, document_types=document_types,
                )
            elif parsed.group_by_hint:
                # GROUP BY recognized but disabled by config — fall through
                # to tier 2 rather than silently ignoring the grouping intent
                # and returning an ungrouped aggregate that misrepresents
                # what was asked.
                return None
            else:
                result = _sql.run_aggregate(
                    operation, query, parsed.tree,
                    document_id=document_id, document_types=document_types,
                )
            return result

        # exact_lookup: tier 2's row-scoring lookup is already a cheap,
        # well-tested single-row scan — no pushdown-specific benefit for a
        # "find the ONE best row" query, so it's intentionally tier-2-only.
        return None
    except Exception as exc:
        logger.warning("Structured query tier-1 (SQL pushdown) failed (falling back to tier-2): %s", exc)
        return None


_GROUP_BY_STRIP_RE = re.compile(
    r"\b(?:grouped\s+by|group\s+by|by|per)\s+[A-Za-z][A-Za-z0-9 _\-\(\)/%]*?\s*[\?\.]?$",
    re.IGNORECASE,
)


def _try_structured_query_tier2(
    query: str,
    document_id: Optional[str] = None,
    document_types: Optional[list] = None,
) -> Optional[dict]:
    """Original Python/JSONB-scan engine — see try_structured_query's
    docstring for tiering rationale. Unchanged behavior from before the
    tier-1 SQL pushdown was added."""
    try:
        tables = _fetch_candidate_tables(document_id, document_types)
    except Exception as exc:
        logger.warning("Structured query: failed to fetch candidate tables (%s)", exc)
        return None

    if not tables:
        return None

    # ── Row-filter + column lookup (checked first — more specific pattern) ──
    if (_ROW_LOOKUP_RE.search(query.strip()) or _ATTR_LOOKUP_RE.search(query.strip())
            or _SUBJECT_FIRST_ATTR_LOOKUP_RE.search(query.strip())):
        for table in tables:
            try:
                result = _run_row_lookup(query, table)
            except Exception as exc:
                logger.warning("Structured query: row lookup failed for table %s (%s)",
                                table.get("table_id"), exc)
                result = None
            if result is not None:
                return result
        # Recognized the lookup shape but couldn't resolve it against any
        # candidate table — fall through to aggregate detection, then None.

    # ── List/filter (checked before aggregate — "how many" COUNT wording can
    # overlap with list phrasing, but an explicit "list/show all"/"which ...
    # are" ask means the user wants the actual rows, not just a count) ──────
    if _LIST_RE.search(query.strip()):
        for table in tables:
            try:
                result = _run_list_filter(query, table)
            except Exception as exc:
                logger.warning("Structured query: list filter failed for table %s (%s)",
                                table.get("table_id"), exc)
                result = None
            if result is not None:
                return result
        # Recognized the list shape but couldn't resolve a filter column
        # against any candidate table — fall through to aggregate, then None.

    # ── Aggregate detection ──────────────────────────────────────────────────
    operation = _detect_aggregate(query)
    if operation is None:
        return None

    for table in tables:
        try:
            col_idx = _extract_target_column(query, table["headers"])
            if col_idx is None and operation != "COUNT":
                continue
            if operation == "COUNT" and col_idx is None:
                # "how many rows/entries" with no specific column named —
                # count rows using the first column as a stand-in label.
                col_idx = 0 if table["headers"] else None
                if col_idx is None:
                    continue
            result = _run_aggregate(operation, table, col_idx)
        except Exception as exc:
            logger.warning("Structured query: aggregate failed for table %s (%s)",
                            table.get("table_id"), exc)
            result = None
        if result is not None:
            return result

    return None
