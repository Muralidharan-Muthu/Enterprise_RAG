"""Tests for automatic multi-page table continuation merging
(document_parser._merge_continued_tables).

Background (empirically confirmed): Docling splits a single logical table
into separate table objects at page boundaries even within one whole-document
convert() call with zero chunking. _merge_continued_tables detects and
collapses these fragments back into one ExtractedTable so downstream chunking
/ retrieval sees the true logical table instead of arbitrary page-sized
slivers.

Heuristic under test: two tables A, B (B immediately after A in page order)
merge when (a) B.page_number == A.page_number + 1, (b) A.headers == B.headers
(exact match), (c) every row in both tables has len(row) == len(headers).
Chains transitively for 3+ page continuations.

Pure: no Docling / DB / network involved — operates directly on
ExtractedTable dataclasses.
"""
from __future__ import annotations

from app.models.document import ExtractedTable
from app.services.document_parser import _merge_continued_tables


def _table(table_index, page_number, headers, rows, **kwargs):
    return ExtractedTable(
        table_index=table_index,
        page_number=page_number,
        headers=headers,
        rows=rows,
        raw_text="",
        markdown_text="",
        **kwargs,
    )


HEADERS = ["Name", "Value"]


class TestTwoPageMerge:
    def test_two_consecutive_pages_same_headers_merge(self):
        a = _table(0, 1, HEADERS, [["r1", "1"], ["r2", "2"]])
        b = _table(1, 2, HEADERS, [["r3", "3"], ["r4", "4"]])

        out = _merge_continued_tables([a, b])

        assert len(out) == 1
        merged = out[0]
        assert merged.rows == [["r1", "1"], ["r2", "2"], ["r3", "3"], ["r4", "4"]]
        assert merged.row_page_numbers == [1, 1, 2, 2]
        assert merged.page_number == 1  # first fragment's page, representative
        assert merged.table_index == 0  # renumbered contiguously

    def test_merge_sets_continuation_metadata(self):
        a = _table(0, 1, HEADERS, [["r1", "1"]])
        b = _table(1, 2, HEADERS, [["r2", "2"]])

        out = _merge_continued_tables([a, b])

        cont = out[0].table_metadata["continuation"]
        assert cont["is_continuation"] is True
        assert cont["fragment_count"] == 2
        assert cont["fragment_pages"] == [1, 2]
        assert cont["fragment_table_indices"] == [0, 1]

    def test_merged_raw_and_markdown_rebuilt_from_combined_rows(self):
        a = _table(0, 1, HEADERS, [["r1", "1"]])
        b = _table(1, 2, HEADERS, [["r2", "2"]])

        out = _merge_continued_tables([a, b])
        merged = out[0]

        assert "r1" in merged.raw_text and "r2" in merged.raw_text
        assert "r1" in merged.markdown_text and "r2" in merged.markdown_text

    def test_merged_table_index_renumbered_with_trailing_table(self):
        a = _table(0, 1, HEADERS, [["r1", "1"]])
        b = _table(1, 2, HEADERS, [["r2", "2"]])
        c = _table(2, 5, ["Other"], [["x"]])  # unrelated, non-adjacent page

        out = _merge_continued_tables([a, b, c])

        assert len(out) == 2
        assert [t.table_index for t in out] == [0, 1]
        assert out[1].headers == ["Other"]
        assert "continuation" not in out[1].table_metadata

    def test_representative_bbox_and_image_from_first_fragment(self):
        from app.models.document import BoundingBox

        bbox_a = BoundingBox(x1=0, y1=0, x2=1, y2=1)
        bbox_b = BoundingBox(x1=9, y1=9, x2=10, y2=10)
        a = _table(0, 1, HEADERS, [["r1", "1"]], bbox=bbox_a, image_png_bytes=b"AAA")
        b = _table(1, 2, HEADERS, [["r2", "2"]], bbox=bbox_b, image_png_bytes=b"BBB")

        out = _merge_continued_tables([a, b])
        merged = out[0]

        assert merged.bbox is bbox_a
        assert merged.image_png_bytes == b"AAA"


class TestThreePageTransitiveMerge:
    def test_three_consecutive_pages_merge_into_one(self):
        a = _table(0, 1, HEADERS, [["r1", "1"]])
        b = _table(1, 2, HEADERS, [["r2", "2"]])
        c = _table(2, 3, HEADERS, [["r3", "3"]])

        out = _merge_continued_tables([a, b, c])

        assert len(out) == 1
        merged = out[0]
        assert merged.rows == [["r1", "1"], ["r2", "2"], ["r3", "3"]]
        assert merged.row_page_numbers == [1, 2, 3]
        cont = merged.table_metadata["continuation"]
        assert cont["fragment_count"] == 3
        assert cont["fragment_pages"] == [1, 2, 3]
        assert cont["fragment_table_indices"] == [0, 1, 2]


class TestNoFalsePositives:
    def test_different_headers_on_consecutive_pages_do_not_merge(self):
        a = _table(0, 1, ["Name", "Value"], [["r1", "1"]])
        b = _table(1, 2, ["Product", "Price"], [["r2", "2"]])

        out = _merge_continued_tables([a, b])

        assert len(out) == 2
        assert "continuation" not in out[0].table_metadata
        assert "continuation" not in out[1].table_metadata
        assert out[0].row_page_numbers is None
        assert out[1].row_page_numbers is None

    def test_non_consecutive_pages_same_headers_do_not_merge(self):
        # page 1 and page 3, nothing on page 2 — not a continuation
        a = _table(0, 1, HEADERS, [["r1", "1"]])
        b = _table(1, 3, HEADERS, [["r2", "2"]])

        out = _merge_continued_tables([a, b])

        assert len(out) == 2
        assert [t.page_number for t in out] == [1, 3]
        assert "continuation" not in out[0].table_metadata
        assert "continuation" not in out[1].table_metadata


class TestSinglePageUnaffected:
    def test_single_table_passes_through_unchanged(self):
        a = _table(0, 1, HEADERS, [["r1", "1"], ["r2", "2"]])

        out = _merge_continued_tables([a])

        assert len(out) == 1
        assert out[0] is a
        assert out[0].row_page_numbers is None
        assert "continuation" not in out[0].table_metadata

    def test_empty_list_returns_empty(self):
        assert _merge_continued_tables([]) == []

    def test_multiple_unrelated_single_page_tables_all_survive(self):
        a = _table(0, 1, ["A"], [["1"]])
        b = _table(1, 5, ["B"], [["2"]])
        c = _table(2, 9, ["C"], [["3"]])

        out = _merge_continued_tables([a, b, c])

        assert len(out) == 3
        assert [t.table_index for t in out] == [0, 1, 2]
        for t in out:
            assert t.row_page_numbers is None
            assert "continuation" not in t.table_metadata
