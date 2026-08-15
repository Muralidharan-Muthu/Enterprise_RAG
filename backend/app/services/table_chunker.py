"""
table_chunker — feature 1.5: parent/child row-windows for scalable table vectorization.

Public API
----------
serialize_row(headers, row) -> str
    Format one data row as "Header1: val1; Header2: val2".

build_row_windows(table, max_tokens, max_rows, overlap_rows) -> list[TableRowChunk]
    Split a table's rows into token-bounded windows; repeat header context at top
    of each window's serialized_text.

build_table_summary_text(table) -> str
    Build the parent-level summary text (caption + columns + sample rows + flags).
    Replaces the old `caption or "Table {idx}: " + raw_text[:200]` string.

chunk_tables(tables, max_tokens, max_rows, overlap_rows, max_windows_per_table)
    -> tuple[list[TableRowChunk], list[str]]
    Returns (all child TableRowChunks across all tables, parent summary texts
    aligned to tables order).  Summary text list is always len(tables).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.document import ExtractedTable

from app.models.document import TableRowChunk

logger = logging.getLogger(__name__)

# ── token budget heuristic ────────────────────────────────────────────────────
# We have no tokenizer here; approximate tokens ≈ words (1 word ≈ 1.3 tokens).
# A 256-token budget → ~197 words.  Using word count keeps us dependency-free
# and fast enough for thousands of rows.
_WORDS_PER_TOKEN = 1.0 / 1.3


def _word_count(text: str) -> int:
    return len(text.split())


# ── public helpers ────────────────────────────────────────────────────────────

def serialize_row(headers: list[str], row: list[str]) -> str:
    """Format one data row as "Header1: val1; Header2: val2".

    Handles length mismatches (extra headers → empty string value, extra cells
    → unnamed_N key).  None and empty-string cells are kept as empty strings.
    """
    parts: list[str] = []
    n_headers = len(headers)
    n_cells = len(row)
    n_cols = max(n_headers, n_cells)

    for i in range(n_cols):
        header = headers[i] if i < n_headers else f"col_{i}"
        cell = row[i] if i < n_cells else ""
        cell = "" if cell is None else str(cell)
        parts.append(f"{header}: {cell}")

    return "; ".join(parts)


def build_row_windows(
    table: "ExtractedTable",
    max_tokens: int = 256,
    max_rows: int = 25,
    overlap_rows: int = 0,
) -> list[TableRowChunk]:
    """Split table.rows into token-bounded windows.

    Each window's serialized_text starts with a header context line:
        "Table: <caption> | Columns: Col1, Col2, ..."
    followed by one serialized row per line.

    Parameters
    ----------
    table:         ExtractedTable (must have .headers and .rows).
    max_tokens:    Approximate upper token bound per window (word-based heuristic).
    max_rows:      Hard upper bound on rows per window (checked before token budget).
    overlap_rows:  Number of rows from the end of the previous window to repeat at
                   the start of the next window (0 = no overlap).

    Returns an empty list for tables with 0 data rows.
    """
    rows = table.rows or []
    headers = table.headers or []
    if not rows:
        return []

    # Header context prepended to every window.
    caption_part = table.caption or f"Table {table.table_index}"
    header_line = (
        f"Table: {caption_part} | Columns: {', '.join(headers) if headers else '(no headers)'}"
    )

    # Windowing is driven by ROW COUNT (max_rows), not a token budget: a table
    # splits into deterministic fixed-size windows — e.g. 200 rows with
    # max_rows=25 → 8 windows of 25 rows each (200 = 25 * 8). max_tokens only
    # sets a per-row safety ceiling: a single pathologically-wide row is
    # truncated to ~4 chars/token so its embedding input stays sane (same ratio
    # as SYNTHESIS_CONTEXT_MAX_CHARS in config.py). Normal rows are never
    # truncated. This keeps window boundaries predictable regardless of how wide
    # the rows are (narrow vs. wide cells no longer change the row count/window).
    max_row_chars = max_tokens * 4

    chunks: list[TableRowChunk] = []
    chunk_index = 0
    i = 0  # current data-row index

    while i < len(rows):
        window_rows: list[str] = []   # serialized lines for this window
        row_start = i

        while i < len(rows) and len(window_rows) < max(1, max_rows):
            serialized = serialize_row(headers, rows[i])
            if len(serialized) > max_row_chars:
                logger.warning(
                    "table_chunker: table %d row %d serializes to %d chars "
                    "(ceiling %d = max_tokens*4) — truncating before embedding",
                    table.table_index, i, len(serialized), max_row_chars,
                )
                serialized = serialized[:max_row_chars]
            window_rows.append(serialized)
            i += 1

        row_end = i - 1  # last row index included (inclusive)

        serialized_text = header_line + "\n" + "\n".join(window_rows)

        meta: dict = {}
        # Carry the parent oversized flag into chunk metadata if present
        if table.table_metadata.get("oversized"):
            meta["oversized"] = True
            meta["parent_row_count"] = table.table_metadata.get("row_count")

        # Multi-page continuation tables (document_parser._merge_continued_tables)
        # carry a per-row page array; record which page(s) this window's row
        # range spans so a chunk can be traced back to its source page(s) even
        # after the fragments were merged into one logical table upstream.
        row_page_numbers = getattr(table, "row_page_numbers", None)
        if row_page_numbers:
            page_slice = row_page_numbers[row_start:row_end + 1]
            if page_slice:
                meta["page_start"] = page_slice[0]
                meta["page_end"] = page_slice[-1]

        chunks.append(TableRowChunk(
            table_index=table.table_index,
            chunk_index=chunk_index,
            row_start=row_start,
            row_end=row_end,
            serialized_text=serialized_text,
            page_number=table.page_number,
            chunk_metadata=meta,
        ))
        chunk_index += 1

        # Overlap: step back by overlap_rows so next window re-includes them
        if overlap_rows > 0 and i < len(rows):
            i = max(row_start + 1, i - overlap_rows)

    return chunks


def build_table_summary_text(table: "ExtractedTable") -> str:
    """Build a rich parent-level summary text for embedding.

    Replaces the old `caption or "Table {idx}: " + raw_text[:200]` string.
    Includes: caption, column names, a sample of up to 3 rows, and flags.

    This text is embedded into table_store.embedding (the parent summary vector).
    """
    parts: list[str] = []

    caption = (table.caption or "").strip()
    if caption:
        parts.append(f"Table: {caption}")
    else:
        parts.append(f"Table {table.table_index}")

    headers = table.headers or []
    if headers:
        parts.append("Columns: " + ", ".join(str(h) for h in headers))

    rows = table.rows or []
    sample = rows[:3]
    if sample:
        parts.append("Sample rows:")
        for row in sample:
            parts.append("  " + serialize_row(headers, row))

    flags: list[str] = []
    if table.table_metadata.get("oversized"):
        flags.append(f"oversized ({table.table_metadata.get('row_count', '?')} rows)")
    if flags:
        parts.append("Flags: " + "; ".join(flags))

    return "\n".join(parts)


def build_window_structured_content(table: "ExtractedTable", row_start: int, row_end: int) -> str:
    """JSON slice mirroring table_store.structured_content, for one row-window.

    Used for LARGE tables (row_count > TABLE_CHUNK_MAX_ROWS): each child window in
    table_chunk_store carries the structured (JSON) view of just its own rows, so a
    query can match/surface it at window granularity instead of the diluted
    whole-table structured_content_embedding on table_store.

    Rows are ``table.rows[row_start:row_end+1]`` — the canonical (reconciled) rows
    already on the ExtractedTable (VLM output when it won the faithfulness gate,
    Docling otherwise), never a re-parse of the raw VLM blob.
    """
    import json
    return json.dumps(
        {
            "title": getattr(table, "caption", None),
            "headers": table.headers or [],
            "rows": [list(r) for r in (table.rows or [])[row_start:row_end + 1]],
        },
        indent=2,          # pretty-printed / multi-line (mirrors table_store.structured_content)
        ensure_ascii=False,  # keep ₹ / € / £ literal instead of \uXXXX escapes
    )


def chunk_tables(
    tables: list["ExtractedTable"],
    max_tokens: int = 256,
    max_rows: int = 25,
    overlap_rows: int = 0,
    max_windows_per_table: int = 200,
) -> tuple[list[TableRowChunk], list[str]]:
    """Chunk all tables into child windows and build parent summary texts.

    Returns
    -------
    (all_children, summary_texts)
    - all_children : flat list of TableRowChunk across all tables (in table order).
    - summary_texts: list of str, one per table (aligned to tables order).
                     Always len(tables); 0-row tables get a summary but 0 children.

    Cost bounding
    -------------
    If a table would produce more than max_windows_per_table windows, the per-window
    row count is increased so the result fits within the cap (coarser windows).
    This is noted in chunk_metadata["coarsened"] for observability.
    """
    all_children: list[TableRowChunk] = []
    summary_texts: list[str] = []

    for table in tables:
        summary_texts.append(build_table_summary_text(table))

        rows = table.rows or []
        headers = table.headers or []
        if not rows:
            # 0 data rows → no children
            continue

        # Estimate window count with current settings
        effective_max_rows = max_rows
        coarsened = False

        # Quick estimate: assume each row fills budget (worst case = 1 row/window)
        # Better: try once and count, then rescale if needed.
        trial_chunks = build_row_windows(table, max_tokens, effective_max_rows, overlap_rows)
        n_windows = len(trial_chunks)

        if n_windows > max_windows_per_table:
            # Rescale: how many rows do we need per window to fit within cap?
            # rows_needed_per_window = ceil(len(rows) / max_windows_per_table)
            import math
            rows_needed = math.ceil(len(rows) / max_windows_per_table)
            effective_max_rows = max(rows_needed, max_rows)
            # The token budget must ALSO permit that many rows per window, else it
            # stays the binding constraint and the window count never drops (the
            # coarsening would silently no-op). Raise max_tokens to fit the widest
            # `effective_max_rows` rows plus the header context line.
            widest_row_words = max(
                (_word_count(serialize_row(headers, r)) for r in rows), default=1
            )
            caption_part = table.caption or f"Table {table.table_index}"
            header_line = (
                f"Table: {caption_part} | "
                f"Columns: {', '.join(headers) if headers else '(no headers)'}"
            )
            header_words = _word_count(header_line)
            needed_words = header_words + effective_max_rows * widest_row_words
            effective_max_tokens = max(max_tokens, math.ceil(needed_words / _WORDS_PER_TOKEN) + 1)
            coarsened = True
            trial_chunks = build_row_windows(
                table, effective_max_tokens, effective_max_rows, overlap_rows
            )
            logger.info(
                "table_chunker: table %d had %d windows (cap=%d) → coarsened to "
                "%d rows/window (budget %d tok) → %d windows",
                table.table_index, n_windows, max_windows_per_table,
                effective_max_rows, effective_max_tokens, len(trial_chunks),
            )

        if coarsened:
            for chunk in trial_chunks:
                chunk.chunk_metadata["coarsened"] = True
                chunk.chunk_metadata["rows_per_window"] = effective_max_rows

        all_children.extend(trial_chunks)

    return all_children, summary_texts
