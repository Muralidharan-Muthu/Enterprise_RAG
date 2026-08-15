"""
Tests for Feature 1.5 — scalable table vectorization (parent/child row-windows).

Locks the core contract:
  - a large table is split into many bounded child windows (not 1 useless vector)
  - every data row is covered exactly once (no loss, no gaps) when no overlap
  - each window carries repeated header context for standalone embedding
  - one parent summary text per table (aligned to input order), always present
  - edge cases: empty table → 0 children (still 1 summary), tiny table → 1 window
  - cost bounding: windows are coarsened to stay under max_windows_per_table

No model / DB / network — pure windowing math, safe for the default (fast) run.
"""
from __future__ import annotations

from app.models.document import ExtractedTable
from app.services.table_chunker import (
    build_row_windows,
    build_table_summary_text,
    chunk_tables,
    serialize_row,
)


def _make_table(n_rows: int, table_index: int = 0, caption: str = "Employee Records") -> ExtractedTable:
    headers = ["ID", "Name", "Dept", "Salary"]
    depts = ["Eng", "Sales", "HR", "Ops"]
    rows = [
        [str(i), f"Person{i}", depts[i % 4], str(50000 + i * 100)]
        for i in range(n_rows)
    ]
    return ExtractedTable(
        table_index=table_index,
        page_number=1,
        headers=headers,
        rows=rows,
        caption=caption,
        raw_text="",
        markdown_text="",
    )


# ─────────────────────────────────────────────────────────────────────────────
# serialize_row
# ─────────────────────────────────────────────────────────────────────────────

class TestSerializeRow:
    def test_pairs_headers_with_values(self):
        assert serialize_row(["A", "B"], ["1", "2"]) == "A: 1; B: 2"

    def test_extra_cells_get_synthetic_header(self):
        out = serialize_row(["A"], ["1", "2"])
        assert "A: 1" in out and "col_1: 2" in out

    def test_none_cell_becomes_empty_string(self):
        assert serialize_row(["A"], [None]) == "A: "


# ─────────────────────────────────────────────────────────────────────────────
# build_row_windows — the core windowing
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildRowWindows:
    def test_large_table_produces_many_bounded_windows(self):
        table = _make_table(500)
        windows = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=0)
        # ~20 windows for a 500-row table (was 1 useless summary vector before 1.5)
        assert 15 <= len(windows) <= 40, f"expected ~20 windows, got {len(windows)}"

    def test_no_window_exceeds_max_rows(self):
        table = _make_table(500)
        windows = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=0)
        for w in windows:
            assert (w.row_end - w.row_start + 1) <= 25

    def test_every_row_covered_exactly_once_without_overlap(self):
        table = _make_table(500)
        windows = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=0)
        covered: list[int] = []
        for w in windows:
            covered.extend(range(w.row_start, w.row_end + 1))
        assert covered == list(range(500)), "rows must be contiguous, no gaps or dupes"

    def test_each_window_repeats_header_context(self):
        table = _make_table(100)
        windows = build_row_windows(table, max_tokens=256, max_rows=25)
        for w in windows:
            assert w.serialized_text.startswith("Table: Employee Records")
            assert "Columns: ID, Name, Dept, Salary" in w.serialized_text.splitlines()[0]

    def test_chunk_indices_are_sequential(self):
        table = _make_table(100)
        windows = build_row_windows(table, max_tokens=256, max_rows=25)
        assert [w.chunk_index for w in windows] == list(range(len(windows)))

    def test_empty_table_yields_no_windows(self):
        table = _make_table(0)
        assert build_row_windows(table) == []

    def test_overlap_repeats_rows_between_windows(self):
        table = _make_table(100)
        no_overlap = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=0)
        overlap = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=5)
        # Overlap re-includes rows → strictly more windows to cover the same rows.
        assert len(overlap) > len(no_overlap)

    def test_windows_split_by_row_count_not_token_budget(self):
        # Windowing is row-count driven (max_rows), NOT token-budget driven, so a
        # 200-row table always splits into exactly ceil(200/25) = 8 windows of 25
        # rows — regardless of how WIDE the rows are. Wide (many-word) rows used
        # to shrink windows to ~10 rows under the old token budget; lock that out.
        wide_headers = ["S.No", "Company Name", "NSE Symbol", "Sector", "Price (INR)", "Change (%)"]
        wide_rows = [
            [str(i + 1), f"Very Long Company Name Number {i + 1} Limited",
             f"SYMBOL{i + 1}", "Energy / Refining / Petrochemicals",
             f"Rs {10000 + i}.89", f"+{i % 5}.18%"]
            for i in range(200)
        ]
        table = ExtractedTable(
            table_index=0, page_number=1, headers=wide_headers, rows=wide_rows,
            caption=None, raw_text="", markdown_text="",
        )
        windows = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=0)
        assert len(windows) == 8, f"200 rows / 25 = 8 windows, got {len(windows)}"
        assert all((w.row_end - w.row_start + 1) == 25 for w in windows)
        # Contiguous, no gaps, no dupes.
        covered: list[int] = []
        for w in windows:
            covered.extend(range(w.row_start, w.row_end + 1))
        assert covered == list(range(200))


# ─────────────────────────────────────────────────────────────────────────────
# build_row_windows — oversized single-row edge case (truncation + warning)
# ─────────────────────────────────────────────────────────────────────────────

class TestOversizedSingleRow:
    """A single row whose serialized text alone exceeds max_tokens must still
    be included (no infinite loop / no data loss), but now bounded in size
    (~4 chars/token heuristic) and logged as a warning for observability."""

    def _oversized_table(self, table_index: int = 0) -> ExtractedTable:
        # One data cell far larger than any reasonable token budget. Must be
        # many separate words (not one giant unbroken string) since the
        # windowing budget is a WORD-count heuristic (_word_count splits on
        # whitespace) — a single unbroken blob only counts as ~1-2 "words"
        # and would never trip the degenerate (budget-busting) branch at all.
        huge_value = " ".join(["word"] * 2000)  # ~2000 words, ~10000 chars
        return ExtractedTable(
            table_index=table_index,
            page_number=1,
            headers=["Notes"],
            rows=[[huge_value]],
            caption="Huge Row Table",
            raw_text="",
            markdown_text="",
        )

    def test_oversized_row_is_included_not_dropped(self):
        table = self._oversized_table()
        windows = build_row_windows(table, max_tokens=50, max_rows=25, overlap_rows=0)
        assert len(windows) == 1
        assert windows[0].row_start == 0 and windows[0].row_end == 0

    def test_oversized_row_is_truncated_to_max_tokens_times_4_chars(self):
        table = self._oversized_table()
        max_tokens = 50
        windows = build_row_windows(table, max_tokens=max_tokens, max_rows=25, overlap_rows=0)
        serialized_text = windows[0].serialized_text
        # serialized_text = header_line + "\n" + row_text; the row portion
        # (after the header line) must be bounded to max_tokens*4 chars.
        header_line, row_part = serialized_text.split("\n", 1)
        assert len(row_part) <= max_tokens * 4

    def test_oversized_row_logs_warning_with_table_index(self, caplog):
        import logging
        table = self._oversized_table(table_index=7)
        with caplog.at_level(logging.WARNING, logger="app.services.table_chunker"):
            build_row_windows(table, max_tokens=50, max_rows=25, overlap_rows=0)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert any("7" in r.getMessage() for r in warnings)

    def test_non_oversized_row_is_not_truncated_or_warned(self, caplog):
        import logging
        table = _make_table(1)  # small row, well within budget
        with caplog.at_level(logging.WARNING, logger="app.services.table_chunker"):
            windows = build_row_windows(table, max_tokens=256, max_rows=25, overlap_rows=0)

        assert not any(r.levelno == logging.WARNING for r in caplog.records)
        assert len(windows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# build_table_summary_text — the parent vector text
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildTableSummaryText:
    def test_summary_includes_caption_columns_and_sample(self):
        table = _make_table(10)
        summary = build_table_summary_text(table)
        assert "Table: Employee Records" in summary
        assert "Columns: ID, Name, Dept, Salary" in summary
        assert "Sample rows:" in summary

    def test_summary_caps_sample_at_three_rows(self):
        table = _make_table(100)
        summary = build_table_summary_text(table)
        # header line + columns + "Sample rows:" + 3 sample lines = 6 lines max
        sample_lines = [ln for ln in summary.splitlines() if ln.startswith("  ")]
        assert len(sample_lines) == 3


# ─────────────────────────────────────────────────────────────────────────────
# chunk_tables — the orchestrator entry point
# ─────────────────────────────────────────────────────────────────────────────

class TestChunkTables:
    def test_returns_children_and_one_summary_per_table(self):
        tables = [_make_table(500, 0), _make_table(2, 1), _make_table(0, 2)]
        children, summaries = chunk_tables(tables, max_tokens=256, max_rows=25)
        # One summary per input table, in order, always.
        assert len(summaries) == 3
        # 500-row table → many children; 2-row → 1; 0-row → 0.
        idx0 = [c for c in children if c.table_index == 0]
        idx1 = [c for c in children if c.table_index == 1]
        idx2 = [c for c in children if c.table_index == 2]
        assert len(idx0) >= 15
        assert len(idx1) == 1
        assert len(idx2) == 0

    def test_children_reference_correct_table_index(self):
        tables = [_make_table(50, 3), _make_table(50, 7)]
        children, _ = chunk_tables(tables, max_tokens=256, max_rows=25)
        seen = {c.table_index for c in children}
        assert seen == {3, 7}

    def test_cost_bound_coarsens_windows_over_cap(self):
        # 500 rows with a tiny cap forces coarsening (more rows per window).
        table = _make_table(500)
        children, _ = chunk_tables(
            [table], max_tokens=256, max_rows=25, max_windows_per_table=5
        )
        assert len(children) <= 5
        assert all(c.chunk_metadata.get("coarsened") for c in children)
        # Coarsened windows still cover every row exactly once.
        covered: list[int] = []
        for c in sorted(children, key=lambda x: x.row_start):
            covered.extend(range(c.row_start, c.row_end + 1))
        assert covered == list(range(500))
