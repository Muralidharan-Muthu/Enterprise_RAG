"""Tests for table caption re-association
(document_parser._reassign_table_captions).

Background: Docling resolves each table's caption against the page-batch-local
document it was parsed in (parse_document_chunked splits the PDF into page
ranges). When a caption and its table are split across a batch boundary — or two
independent tables get merged — a table can end up carrying a *neighbouring*
table's title. This pass re-derives the caption from the aggregated text blocks
(global page numbers, immune to batch boundaries).

Pure: no Docling / DB / network — operates on ExtractedTable + TextBlock
dataclasses directly.
"""
from __future__ import annotations

from app.models.document import BoundingBox, ExtractedTable, TextBlock
from app.services.document_parser import (
    _caption_candidates,
    _reassign_table_captions,
    _table_own_number,
)


def _table(table_index, page_number, headers, rows, caption=None, y1=0.0, **kw):
    return ExtractedTable(
        table_index=table_index,
        page_number=page_number,
        headers=headers,
        rows=rows,
        caption=caption,
        bbox=BoundingBox(0, y1, 100, y1 + 50),
        raw_text=kw.pop("raw_text", ""),
        markdown_text=kw.pop("markdown_text", ""),
        **kw,
    )


def _block(text, page_number, y1=0.0, block_type="caption"):
    return TextBlock(
        text=text,
        page_number=page_number,
        block_type=block_type,
        bbox=BoundingBox(0, y1, 100, y1 + 10),
    )


class TestCaptionCandidates:
    def test_matches_caption_lines_only(self):
        blocks = [
            _block("Table 5: Revenue by Segment", 3, y1=10),
            _block("Table 6: Risk Impact Assessment", 4, y1=10),
            _block("Table 6 — Risk Impact Assessment and Mitigation Spend, FY 2025-26", 4, y1=40),
        ]
        cands = _caption_candidates(blocks)
        assert [c["num"] for c in cands] == [5, 6, 6]

    def test_ignores_prose_mention(self):
        # A sentence that merely mentions the table mid-flow must NOT be a caption.
        blocks = [
            _block(
                "The committee assesses risk each year. Table 6 presents this "
                "assessment for FY 2025-26.",
                4,
                block_type="paragraph",
            ),
        ]
        assert _caption_candidates(blocks) == []

    def test_ignores_table_of_contents_and_overlong(self):
        blocks = [_block("Table of Contents", 1), _block("x" * 400, 1)]
        assert _caption_candidates(blocks) == []


class TestOwnNumber:
    def test_reads_number_from_embedded_title_row(self):
        t = _table(
            0, 4, ["Risk Area", "Low", "High"], [["FX", "-", "-"]],
            raw_text="Table 6 — Risk Impact Assessment and Mitigation Spend",
        )
        assert _table_own_number(t) == 6

    def test_ignores_caption_when_reading_own_number(self):
        # own-number must NOT come from the (possibly wrong) caption.
        t = _table(0, 4, ["A", "B"], [["1", "2"]], caption="Table 5: Wrong Title")
        assert _table_own_number(t) is None


class TestReassignReportedBug:
    def test_table_with_neighbours_title_is_corrected(self):
        """The reported failure: Table 6 carries Table 5's title. Its embedded
        number (6) plus the correct 'Table 6:' caption block fix it."""
        blocks = [
            _block("Table 5: Revenue by Segment", 3, y1=10),
            _block("Table 6: Risk Impact Assessment", 4, y1=10),
        ]
        t5 = _table(0, 3, ["Segment", "Revenue"], [["A", "100"]],
                    caption="Table 5: Revenue by Segment", y1=30)
        t6 = _table(
            1, 4, ["Risk Area", "Low"], [["FX", "-"]],
            caption="Table 5: Revenue by Segment",   # WRONG — neighbour's title
            raw_text="Table 6 — Risk Impact Assessment", y1=30,
        )
        _reassign_table_captions([t5, t6], blocks)
        assert t6.caption == "Table 6: Risk Impact Assessment"
        assert t5.caption == "Table 5: Revenue by Segment"

    def test_positional_fallback_when_no_embedded_number(self):
        """Table has no detectable own number → nearest preceding caption wins."""
        blocks = [
            _block("Table 1: First", 1, y1=10),
            _block("Table 2: Second", 2, y1=10),
        ]
        t1 = _table(0, 1, ["a"], [["1"]], caption="Table 2: Second", y1=30)
        t2 = _table(1, 2, ["b"], [["2"]], caption=None, y1=30)
        _reassign_table_captions([t1, t2], blocks)
        assert t1.caption == "Table 1: First"
        assert t2.caption == "Table 2: Second"

    def test_each_caption_consumed_once(self):
        blocks = [_block("Table 1: Only", 1, y1=10)]
        t1 = _table(0, 1, ["a"], [["1"]], y1=20)
        t2 = _table(1, 1, ["b"], [["2"]], y1=50)
        _reassign_table_captions([t1, t2], blocks)
        assert t1.caption == "Table 1: Only"
        # second table must NOT reuse the same caption
        assert t2.caption != "Table 1: Only"


class TestReassignGuards:
    def test_no_candidates_leaves_tables_untouched(self):
        t = _table(0, 1, ["a"], [["1"]], caption="Existing")
        _reassign_table_captions([t], [_block("Some paragraph", 1, block_type="paragraph")])
        assert t.caption == "Existing"

    def test_empty_tables_noop(self):
        assert _reassign_table_captions([], [_block("Table 1: X", 1)]) == []

    def test_caption_below_table_is_not_matched(self):
        # A "Table N:" line that sits BELOW the table (higher y1) is not its caption.
        blocks = [_block("Table 9: Later", 1, y1=90)]
        t = _table(0, 1, ["a"], [["1"]], caption=None, y1=10)
        _reassign_table_captions([t], blocks)
        assert t.caption is None
