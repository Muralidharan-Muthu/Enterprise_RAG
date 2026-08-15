"""Unit tests for document_parser._detect_merged_cells (full per-cell span map
+ multi-row header detection) and the _parse_table_data multi-row-header fix.

Pure: uses lightweight fakes for Docling's table.data.table_cells shape (each
cell exposes row_span/col_span/text/offset indices/column_header) — no real
Docling model or PDF needed. Fake shapes match the verified docling_core
2.74.0 TableCell field names: row_span, col_span, start_row_offset_idx,
end_row_offset_idx (EXCLUSIVE), start_col_offset_idx, end_col_offset_idx
(EXCLUSIVE), text, column_header.
"""
from types import SimpleNamespace

from app.services.document_parser import _detect_merged_cells, _extract_tables, _parse_table_data


def _cell(
    text="x",
    row_span=1,
    col_span=1,
    row_start=0,
    row_end=None,
    col_start=0,
    col_end=None,
    column_header=False,
):
    """Build a fake Docling TableCell. Offset indices default to a simple,
    non-spanned 1x1 cell at (row_start, col_start) unless overridden — old
    tests that only cared about row_span/col_span still get valid (if
    arbitrary) offsets so the new offset-reading code path never crashes."""
    safe_row_span = row_span if isinstance(row_span, int) else 1
    safe_col_span = col_span if isinstance(col_span, int) else 1
    return SimpleNamespace(
        text=text,
        row_span=row_span,
        col_span=col_span,
        start_row_offset_idx=row_start,
        end_row_offset_idx=row_end if row_end is not None else row_start + safe_row_span,
        start_col_offset_idx=col_start,
        end_col_offset_idx=col_end if col_end is not None else col_start + safe_col_span,
        column_header=column_header,
    )


def _table(cells):
    return SimpleNamespace(data=SimpleNamespace(table_cells=cells))


def _bare_defaults():
    """The full-default result shape for a table with no merges/headers."""
    return {
        "has_merged_cells": False,
        "max_row_span": 1,
        "max_col_span": 1,
        "spanned_cell_count": 0,
        "header_row_count": 0,
        "cells": [],
    }


def test_no_merged_cells_when_all_spans_are_one():
    table = _table([_cell(), _cell(), _cell(row_span=1, col_span=1)])
    result = _detect_merged_cells(table)
    assert result == _bare_defaults()


def test_detects_row_span_merge():
    table = _table([_cell(), _cell(row_span=3), _cell()])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is True
    assert result["max_row_span"] == 3
    assert result["max_col_span"] == 1
    assert result["spanned_cell_count"] == 1


def test_detects_col_span_merge():
    table = _table([_cell(), _cell(col_span=2), _cell(col_span=4)])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is True
    assert result["max_col_span"] == 4
    assert result["spanned_cell_count"] == 2


def test_detects_mixed_row_and_col_spans_counts_each_once():
    table = _table([_cell(row_span=2, col_span=3), _cell(), _cell(row_span=2)])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is True
    assert result["max_row_span"] == 2
    assert result["max_col_span"] == 3
    assert result["spanned_cell_count"] == 2   # two distinct cells with span > 1


def test_empty_table_cells_yields_no_merge():
    table = _table([])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is False


def test_missing_table_cells_attribute_fails_open():
    table = SimpleNamespace(data=SimpleNamespace())  # no table_cells at all
    result = _detect_merged_cells(table)
    assert result == _bare_defaults()


def test_missing_data_attribute_fails_open():
    table = SimpleNamespace()  # no .data at all
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is False


def test_cells_missing_span_attrs_default_to_one():
    # A cell object that simply doesn't expose row_span/col_span (e.g. a
    # different Docling version) must not crash — treated as span 1.
    bare_cell = SimpleNamespace(text="bare")
    table = _table([bare_cell])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is False


def test_none_span_values_default_to_one():
    table = _table([_cell(row_span=None, col_span=None)])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is False
    assert result["max_row_span"] == 1
    assert result["max_col_span"] == 1


# ── Full per-cell span map ───────────────────────────────────────────────

def test_single_2x2_merged_region_produces_correct_cells_entry():
    # A 3x3 grid where the top-left 2x2 region is one merged cell:
    #   [ M  M  c ]
    #   [ M  M  c ]
    #   [ c  c  c ]
    merged = _cell(text="merged", row_span=2, col_span=2, row_start=0, col_start=0)
    others = [
        _cell(text="c1", row_start=0, col_start=2),
        _cell(text="c2", row_start=1, col_start=2),
        _cell(text="c3", row_start=2, col_start=0),
        _cell(text="c4", row_start=2, col_start=1),
        _cell(text="c5", row_start=2, col_start=2),
    ]
    table = _table([merged] + others)
    result = _detect_merged_cells(table)

    assert result["has_merged_cells"] is True
    assert result["spanned_cell_count"] == 1
    assert result["max_row_span"] == 2
    assert result["max_col_span"] == 2
    assert len(result["cells"]) == 1

    entry = result["cells"][0]
    assert entry["row_start"] == 0
    assert entry["row_end"] == 2      # exclusive end, per Docling's own convention
    assert entry["col_start"] == 0
    assert entry["col_end"] == 2
    assert entry["text"] == "merged"
    assert entry["is_header"] is False


def test_merged_header_cell_is_flagged_is_header_true():
    merged_header = _cell(
        text="Q1 2024", row_span=1, col_span=2, row_start=0, col_start=0, column_header=True,
    )
    table = _table([merged_header])
    result = _detect_merged_cells(table)
    assert result["cells"][0]["is_header"] is True


def test_non_spanned_cells_are_not_enumerated_in_cells_list():
    # Only genuinely merged cells (span > 1) show up in `cells`; a table with
    # merges plus plenty of ordinary 1x1 cells must not bloat the list.
    merged = _cell(text="m", row_span=2, row_start=0, col_start=0)
    plain = [_cell(text=f"p{i}", row_start=i, col_start=1) for i in range(2)]
    table = _table([merged] + plain)
    result = _detect_merged_cells(table)
    assert len(result["cells"]) == 1
    assert result["cells"][0]["text"] == "m"


# ── Multi-row header detection ───────────────────────────────────────────

def test_two_row_header_detected_via_column_header_flag():
    # Row 0 and row 1 are both header rows (Docling's own column_header=True);
    # row 2+ is data. 2 columns.
    cells = [
        _cell(text="A", row_start=0, col_start=0, column_header=True),
        _cell(text="B", row_start=0, col_start=1, column_header=True),
        _cell(text="C", row_start=1, col_start=0, column_header=True),
        _cell(text="D", row_start=1, col_start=1, column_header=True),
        _cell(text="1", row_start=2, col_start=0, column_header=False),
        _cell(text="2", row_start=2, col_start=1, column_header=False),
    ]
    table = _table(cells)
    result = _detect_merged_cells(table)
    assert result["header_row_count"] == 2


def test_single_header_row_gives_header_row_count_one():
    cells = [
        _cell(text="A", row_start=0, col_start=0, column_header=True),
        _cell(text="B", row_start=0, col_start=1, column_header=True),
        _cell(text="1", row_start=1, col_start=0, column_header=False),
        _cell(text="2", row_start=1, col_start=1, column_header=False),
    ]
    table = _table(cells)
    result = _detect_merged_cells(table)
    assert result["header_row_count"] == 1


def test_no_header_flags_gives_header_row_count_zero():
    cells = [
        _cell(text="A", row_start=0, col_start=0, column_header=False),
        _cell(text="1", row_start=1, col_start=0, column_header=False),
    ]
    table = _table(cells)
    result = _detect_merged_cells(table)
    assert result["header_row_count"] == 0


def test_header_row_count_only_counts_contiguous_leading_block():
    # A stray header-flagged cell deep in the data (row 5) must not inflate
    # header_row_count beyond the actual leading contiguous block (rows 0-1).
    cells = [
        _cell(text="A", row_start=0, col_start=0, column_header=True),
        _cell(text="B", row_start=1, col_start=0, column_header=True),
        _cell(text="C", row_start=2, col_start=0, column_header=False),
        _cell(text="stray", row_start=5, col_start=0, column_header=True),
    ]
    table = _table(cells)
    result = _detect_merged_cells(table)
    assert result["header_row_count"] == 2


def test_merged_header_cell_spanning_two_rows_counts_both_rows():
    # A single header cell with row_span=2 covering rows 0-1 should mark BOTH
    # rows as header rows even though only one TableCell object carries the flag.
    cells = [
        _cell(text="Spanning Header", row_span=2, row_start=0, col_start=0, column_header=True),
        _cell(text="1", row_start=2, col_start=0, column_header=False),
    ]
    table = _table(cells)
    result = _detect_merged_cells(table)
    assert result["header_row_count"] == 2


# ── _parse_table_data multi-row-header combination fix ───────────────────

class _FakeGridCell:
    def __init__(self, text):
        self.text = text


def _grid_table(grid_rows):
    """grid_rows: list[list[str]] -> fake table with table.data.grid."""
    grid = [[_FakeGridCell(t) for t in row] for row in grid_rows]
    return SimpleNamespace(data=SimpleNamespace(grid=grid))


def test_parse_table_data_unaffected_when_header_row_count_zero_or_one():
    # header_row_count omitted (0/default) — identical to legacy behaviour:
    # row 0 is header, rest is data.
    table = _grid_table([
        ["Name", "Score"],
        ["Alice", "90"],
        ["Bob", "85"],
    ])
    headers, rows = _parse_table_data(table)
    assert headers == ["Name", "Score"]
    assert rows == [["Alice", "90"], ["Bob", "85"]]

    # Explicit header_row_count=1 must behave identically.
    headers2, rows2 = _parse_table_data(table, header_row_count=1)
    assert headers2 == headers
    assert rows2 == rows


def test_parse_table_data_combines_genuine_two_row_header():
    # Row 0: 'Q1 2024' spanning two sub-columns (duplicated by Docling's grid
    # into both columns), row 1: 'Budget' / 'Actual' sub-headers, row 2+: data.
    table = _grid_table([
        ["Q1 2024", "Q1 2024"],
        ["Budget", "Actual"],
        ["100", "90"],
    ])
    headers, rows = _parse_table_data(table, header_row_count=2)
    assert headers == ["Q1 2024 - Budget", "Q1 2024 - Actual"]
    assert rows == [["100", "90"]]
    # The 2-row header block must be fully excluded from data rows.
    assert len(rows) == 1


def test_parse_table_data_three_row_header():
    table = _grid_table([
        ["Region", "Sales"],
        ["", "2024"],
        ["", "Q1"],
        ["East", "10"],
    ])
    headers, rows = _parse_table_data(table, header_row_count=3)
    assert headers == ["Region", "Sales - 2024 - Q1"]
    assert rows == [["East", "10"]]


# ── Regression: no-merge, single-row-header table completely unaffected ──

def test_no_merged_cells_and_normal_header_matches_prior_round_behavior():
    # A table with no spanned cells and a normal single header row: `cells`
    # must be empty and has_merged_cells/max spans must be exactly the prior
    # round's values (no regression for the faithfulness-gate integration).
    cells = [
        _cell(text="Name", row_start=0, col_start=0, column_header=True),
        _cell(text="Score", row_start=0, col_start=1, column_header=True),
        _cell(text="Alice", row_start=1, col_start=0, column_header=False),
        _cell(text="90", row_start=1, col_start=1, column_header=False),
    ]
    table = _table(cells)
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is False
    assert result["max_row_span"] == 1
    assert result["max_col_span"] == 1
    assert result["spanned_cell_count"] == 0
    assert result["cells"] == []
    assert result["header_row_count"] == 1


def test_has_merged_cells_keys_present_alongside_new_cells_list():
    # Faithfulness-gate integration (table_reconstruction.py) reads
    # table_metadata['merged_cells']['has_merged_cells'] as a boolean — this
    # must keep working exactly as before, with the new keys purely additive.
    merged = _cell(text="m", row_span=2, col_span=2, row_start=0, col_start=0)
    table = _table([merged])
    result = _detect_merged_cells(table)
    assert result["has_merged_cells"] is True
    assert result["max_row_span"] == 2
    assert result["max_col_span"] == 2
    assert result["spanned_cell_count"] == 1
    assert isinstance(result["cells"], list) and len(result["cells"]) == 1
    assert "header_row_count" in result


# ── End-to-end: _extract_tables with a fake Docling `doc` ────────────────

def _fake_full_table(grid_rows, cells, page_no=1, caption_text=""):
    """A fake Docling `table` object exposing everything _extract_tables /
    _get_table_page / _get_table_bbox / _parse_table_data / _detect_merged_cells
    touch. get_image raises (fails open to table_png=None), no prov/bbox
    (fails open to page 1 / bbox None) unless page_no given.

    Real Docling TableItem has NO `.caption` attribute — only a
    caption_text(doc) method that resolves caption ref(s) against the parent
    document (see document_parser._extract_tables). Mirror that here so this
    fixture reflects the actual API instead of the wrong attribute name."""
    grid = [[_FakeGridCell(t) for t in row] for row in grid_rows]

    def _get_image(_doc):
        raise RuntimeError("no image in fake table")

    prov = [SimpleNamespace(page_no=page_no, bbox=None)]
    return SimpleNamespace(
        data=SimpleNamespace(grid=grid, table_cells=cells),
        prov=prov,
        get_image=_get_image,
        caption_text=lambda _doc: caption_text,
    )


def _fake_doc(tables):
    return SimpleNamespace(tables=tables)


def test_extract_tables_two_row_header_end_to_end():
    # Q1 2024 spans two sub-columns (Budget/Actual) in row 0; row 1 carries
    # the sub-header text; row 2 is the only real data row.
    grid_rows = [
        ["Q1 2024", "Q1 2024"],
        ["Budget", "Actual"],
        ["100", "90"],
    ]
    cells = [
        _cell(text="Q1 2024", row_span=1, col_span=2, row_start=0, col_start=0, column_header=True),
        _cell(text="Budget", row_start=1, col_start=0, column_header=True),
        _cell(text="Actual", row_start=1, col_start=1, column_header=True),
        _cell(text="100", row_start=2, col_start=0, column_header=False),
        _cell(text="90", row_start=2, col_start=1, column_header=False),
    ]
    table = _fake_full_table(grid_rows, cells)
    doc = _fake_doc([table])

    tables = _extract_tables(doc)
    assert len(tables) == 1
    extracted = tables[0]

    # Before this fix, row 1 ("Budget"/"Actual") would have been misread as a
    # DATA row, giving row_count=2 and headers=["Q1 2024", "Q1 2024"].
    assert extracted.headers == ["Q1 2024 - Budget", "Q1 2024 - Actual"]
    assert extracted.rows == [["100", "90"]]
    assert len(extracted.rows) == 1

    span = extracted.table_metadata["merged_cells"]
    assert span["header_row_count"] == 2
    assert span["has_merged_cells"] is True  # the col_span=2 header cell
    assert len(span["cells"]) == 1
    assert span["cells"][0]["is_header"] is True


def test_extract_tables_single_row_header_identical_to_before():
    grid_rows = [
        ["Name", "Score"],
        ["Alice", "90"],
        ["Bob", "85"],
    ]
    cells = [
        _cell(text="Name", row_start=0, col_start=0, column_header=True),
        _cell(text="Score", row_start=0, col_start=1, column_header=True),
        _cell(text="Alice", row_start=1, col_start=0, column_header=False),
        _cell(text="90", row_start=1, col_start=1, column_header=False),
        _cell(text="Bob", row_start=2, col_start=0, column_header=False),
        _cell(text="85", row_start=2, col_start=1, column_header=False),
    ]
    table = _fake_full_table(grid_rows, cells)
    doc = _fake_doc([table])

    tables = _extract_tables(doc)
    extracted = tables[0]
    assert extracted.headers == ["Name", "Score"]
    assert extracted.rows == [["Alice", "90"], ["Bob", "85"]]
    # No merges, single-row header -> no table_metadata written at all
    # (matches prior-round behaviour: empty dict when has_merged_cells is
    # False and there's nothing else noteworthy to record).
    assert extracted.table_metadata == {}


def test_extract_tables_resolves_caption_via_caption_text_method():
    """Docling's TableItem has no `.caption` attribute — only a caption_text(doc)
    method. getattr(table, "caption", None) silently returned None for every
    table (AttributeError swallowed by the default) even when Docling had
    correctly detected a caption. _extract_tables must call caption_text(doc)."""
    grid_rows = [["Name", "Score"], ["Alice", "90"]]
    cells = [
        _cell(text="Name", row_start=0, col_start=0, column_header=True),
        _cell(text="Score", row_start=0, col_start=1, column_header=True),
        _cell(text="Alice", row_start=1, col_start=0, column_header=False),
        _cell(text="90", row_start=1, col_start=1, column_header=False),
    ]
    table = _fake_full_table(grid_rows, cells, caption_text="Table 1: Player Scores")
    doc = _fake_doc([table])

    tables = _extract_tables(doc)
    assert tables[0].caption == "Table 1: Player Scores"


def test_extract_tables_no_caption_yields_none_not_empty_string():
    """An empty caption_text() result (no captions at all) must store None, not
    "" — downstream code (`table.caption or f"Table {i}"`) relies on falsy-but-
    not-empty-string None to trigger its fallback consistently."""
    grid_rows = [["Name", "Score"], ["Alice", "90"]]
    cells = [
        _cell(text="Name", row_start=0, col_start=0, column_header=True),
        _cell(text="Score", row_start=0, col_start=1, column_header=True),
        _cell(text="Alice", row_start=1, col_start=0, column_header=False),
        _cell(text="90", row_start=1, col_start=1, column_header=False),
    ]
    table = _fake_full_table(grid_rows, cells)  # caption_text defaults to ""
    doc = _fake_doc([table])

    tables = _extract_tables(doc)
    assert tables[0].caption is None
