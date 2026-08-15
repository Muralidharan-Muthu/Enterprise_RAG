"""Unit tests for the Slice 2b numeric-faithfulness gate and Docling-vs-VLM
reconciliation (table_reconstruction.reconcile_table / faithfulness_ok /
_numeric_tokens), plus the reconstruct_tables_with_vlm integration that wires
the reconciliation verdict into mutation + the enriched analysis dict.

Pure: no real VLM/OCR/DB is touched (analyze_fn is injected).
"""
import json
from types import SimpleNamespace

from app.services.table_reconstruction import (
    _numeric_tokens,
    _grid_wellformed,
    faithfulness_ok,
    reconcile_table,
    reconstruct_tables_with_vlm,
)


def _vlm_table_json(headers, rows, title="T"):
    return json.dumps({"title": title, "headers": headers, "rows": rows})


def _table(idx=0, img=b"\x89PNG", raw_text="", headers=None, rows=None, table_metadata=None):
    headers = headers if headers is not None else []
    rows = rows if rows is not None else []
    return SimpleNamespace(
        table_index=idx,
        image_png_bytes=img,
        raw_text=raw_text,
        headers=headers,
        rows=rows,
        markdown_text="",
        caption=None,
        table_metadata=table_metadata if table_metadata is not None else {},
    )


def _doc(tables):
    return SimpleNamespace(doc_id="doc-1", tables=tables)


# ── _numeric_tokens ──────────────────────────────────────────────────────────

def test_numeric_tokens_normalizes_thousands_and_currency():
    toks = _numeric_tokens(["1,300", "$1300", "1300.0"])
    # all three normalize to the same comparable token
    assert toks == {"1300"}


def test_numeric_tokens_normalizes_percent_and_symbols():
    toks = _numeric_tokens("Revenue grew 12.5% to €45,000")
    assert "12.5" in toks
    assert "45000" in toks


def test_numeric_tokens_ignores_non_numeric_text():
    toks = _numeric_tokens(["Quarter", "Revenue", "N/A", ""])
    assert toks == set()


def test_numeric_tokens_handles_nested_rows():
    toks = _numeric_tokens([["Quarter One", "100"], ["Quarter Two", "200"]])
    assert toks == {"100", "200"}


def test_numeric_tokens_none_input():
    assert _numeric_tokens(None) == set()


# ── _grid_wellformed ─────────────────────────────────────────────────────────

def test_grid_wellformed_true_for_consistent_grid():
    assert _grid_wellformed(["A", "B"], [["1", "2"], ["3", "4"]]) is True


def test_grid_wellformed_false_when_empty():
    assert _grid_wellformed([], []) is False
    assert _grid_wellformed(["A"], []) is False


def test_grid_wellformed_false_when_no_headers():
    assert _grid_wellformed([], [["1", "2"]]) is False


def test_grid_wellformed_false_when_ragged():
    assert _grid_wellformed(["A", "B"], [["1", "2"], ["3"]]) is False


# ── faithfulness_ok ──────────────────────────────────────────────────────────

def test_faithfulness_passes_when_all_vlm_numbers_seen():
    vlm_rows = [["Q1", "100"], ["Q2", "200"]]
    reference_text = "Q1 100 Q2 200"
    ok, issues = faithfulness_ok(vlm_rows, reference_text, docling_rows=[["100"], ["200"]])
    assert ok is True
    assert issues == []


def test_faithfulness_passes_via_ocr_text_alone():
    vlm_rows = [["Quarter One", "100"]]
    ok, issues = faithfulness_ok(vlm_rows, "the value was 100 last quarter", docling_rows=[])
    assert ok is True
    assert issues == []


def test_faithfulness_fails_when_vlm_hallucinates_number_vs_wellformed_docling():
    # Well-formed Docling grid (consistent column widths) is a strong reference —
    # ANY unseen VLM number fails the gate, even a single one.
    docling_rows = [["Q1", "100"], ["Q2", "150"]]
    vlm_rows = [["Q1", "100"], ["Q2", "999"]]   # 999 is not in Docling or OCR
    ok, issues = faithfulness_ok(vlm_rows, "Q1 100 Q2 150", docling_rows=docling_rows)
    assert ok is False
    assert issues
    assert "999" in issues[0]


def test_faithfulness_tolerates_small_fraction_when_docling_not_wellformed():
    # Docling grid is empty/ragged (no strong reference) — tolerate up to 20%
    # unseen numbers before rejecting.
    vlm_rows = [["100", "200", "300", "400", "500"]]  # 5 numbers, 1 unseen = 20%
    ok, issues = faithfulness_ok(vlm_rows, "100 200 300 400", docling_rows=[])
    assert ok is True
    assert issues == []


def test_faithfulness_rejects_when_unseen_fraction_too_high():
    vlm_rows = [["100", "200", "999"]]   # 2/3 unseen ≈ 67%
    ok, issues = faithfulness_ok(vlm_rows, "100", docling_rows=[])
    assert ok is False
    assert issues


def test_faithfulness_passes_when_vlm_has_no_numbers():
    ok, issues = faithfulness_ok([["Quarter One"]], "some text", docling_rows=[])
    assert ok is True
    assert issues == []


# ── faithfulness_ok: merged-cell middle tier ────────────────────────────────
# Docling's own `grid` property always reports uniform row widths (a spanned
# cell's text is duplicated into every position it covers), so a merged-cell
# table looks "well-formed" to _rows_consistent_width even though it's the
# shape Docling's extraction handles least reliably. has_merged_cells=True
# (from document_parser._detect_merged_cells) should downgrade the strict
# zero-tolerance rule to a middle 8% tolerance — stricter than the 20% loose
# tier, looser than zero.

def test_faithfulness_middle_tier_tolerates_small_fraction_with_merged_cells():
    # Well-formed (uniform) Docling grid + has_merged_cells=True: 1/13 ≈ 7.7%
    # unseen numbers is within the 8% middle tolerance -> passes, whereas the
    # strict rule (no merged cells) would reject on any unseen number.
    docling_rows = [["Q1", "100"], ["Q2", "150"]]
    vlm_rows = [["Q1", "100", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"], ["Q2", "999"]]
    ok, issues = faithfulness_ok(
        vlm_rows, "Q1 100 Q2 150 1 2 3 4 5 6 7 8 9 10 11", docling_rows,
        has_merged_cells=True,
    )
    assert ok is True
    assert issues == []


def test_faithfulness_middle_tier_rejects_when_unseen_fraction_too_high():
    # Same well-formed + merged-cell setup, but unseen fraction (1/2 = 50%)
    # exceeds the 8% middle tolerance -> rejected.
    docling_rows = [["Q1", "100"], ["Q2", "150"]]
    vlm_rows = [["Q1", "100"], ["Q2", "999"]]
    ok, issues = faithfulness_ok(
        vlm_rows, "Q1 100 Q2 150", docling_rows, has_merged_cells=True,
    )
    assert ok is False
    assert issues
    assert "999" in issues[0]
    assert "merged-cell" in issues[0]


def test_faithfulness_middle_tier_stricter_than_loose_tier():
    # Same numeric profile that PASSES the loose (ragged-grid, 20%) tier must
    # FAIL the merged-cell middle (8%) tier when the Docling grid is
    # well-formed -- proves the tiers are ordered strict < middle < loose and
    # the merged-cell branch really does apply tighter scrutiny than loose.
    vlm_rows = [["100", "200", "300", "400", "500"]]  # 1/5 = 20% unseen
    docling_rows_wellformed = [["100"], ["200"], ["300"], ["400"]]

    ok_ragged, _ = faithfulness_ok(vlm_rows, "100 200 300 400", docling_rows=[])
    assert ok_ragged is True  # loose tier (no wellformed grid): 20% <= 20% tolerated

    ok_merged, issues = faithfulness_ok(
        vlm_rows, "100 200 300 400", docling_rows_wellformed, has_merged_cells=True,
    )
    assert ok_merged is False  # middle tier: 20% > 8% tolerance
    assert issues


def test_faithfulness_merged_cells_flag_false_keeps_strict_behavior_unchanged():
    # No regression: has_merged_cells defaults to False, so a well-formed grid
    # still uses the original strict (zero-tolerance) rule.
    docling_rows = [["Q1", "100"], ["Q2", "150"]]
    vlm_rows = [["Q1", "100"], ["Q2", "999"]]
    ok, issues = faithfulness_ok(vlm_rows, "Q1 100 Q2 150", docling_rows)
    assert ok is False
    assert "well-formed Docling grid present" in issues[0]


def test_faithfulness_merged_cells_true_but_grid_not_wellformed_uses_loose_tier():
    # has_merged_cells=True only matters when the Docling grid is well-formed;
    # when the grid itself is empty/ragged, the original loose (20%) tier
    # still applies (no special-casing needed / no double-discount).
    vlm_rows = [["100", "200", "300", "400", "500"]]  # 1/5 = 20% unseen
    ok, issues = faithfulness_ok(
        vlm_rows, "100 200 300 400", docling_rows=[], has_merged_cells=True,
    )
    assert ok is True
    assert issues == []


# ── reconcile_table ──────────────────────────────────────────────────────────

def test_reconcile_no_vlm_table_keeps_pdf_grid():
    result = reconcile_table(["A"], [["1"]], "1", vlm_parsed=None)
    assert result["method"] == "pdf_grid"
    assert result["use_vlm"] is False
    assert result["canonical_headers"] == ["A"]
    assert result["canonical_rows"] == [["1"]]
    assert result["issues"] == []


def test_reconcile_empty_docling_grid_faithful_vlm_chooses_image_vlm():
    vlm_parsed = ("Title", ["Quarter", "Rev"], [["Q1", "100"], ["Q2", "200"]])
    result = reconcile_table(
        docling_headers=[], docling_rows=[], ocr_text="Q1 100 Q2 200", vlm_parsed=vlm_parsed,
    )
    assert result["method"] == "image_vlm"
    assert result["use_vlm"] is True
    assert result["canonical_headers"] == ["Quarter", "Rev"]
    assert result["canonical_rows"] == [["Q1", "100"], ["Q2", "200"]]
    assert result["quality"] == "high"
    assert result["issues"] == []


def test_reconcile_wellformed_docling_faithful_vlm_prefers_docling():
    docling_headers = ["Quarter", "Rev"]
    docling_rows = [["Q1", "100"], ["Q2", "200"]]
    vlm_parsed = ("Title", ["Quarter", "Rev"], [["Q1", "100"], ["Q2", "200"]])
    result = reconcile_table(docling_headers, docling_rows, "Q1 100 Q2 200", vlm_parsed)
    assert result["method"] == "pdf_grid"
    assert result["use_vlm"] is False
    assert result["canonical_headers"] == docling_headers
    assert result["canonical_rows"] == docling_rows
    assert result["quality"] == "high"
    assert result["issues"] == []


def test_reconcile_vlm_hallucination_vs_wellformed_docling_rejects_vlm():
    docling_headers = ["Quarter", "Rev"]
    docling_rows = [["Q1", "100"], ["Q2", "150"]]
    vlm_parsed = ("Title", ["Quarter", "Rev"], [["Q1", "100"], ["Q2", "999"]])
    result = reconcile_table(docling_headers, docling_rows, "Q1 100 Q2 150", vlm_parsed)
    assert result["method"] == "pdf_grid"
    assert result["use_vlm"] is False
    assert result["quality"] == "low"
    assert result["canonical_headers"] == docling_headers
    assert result["canonical_rows"] == docling_rows
    assert result["issues"]
    assert "999" in result["issues"][0]


def test_reconcile_merged_cells_flag_relaxes_strict_rejection():
    # Without has_merged_cells: strict rule rejects on the single unseen "999".
    docling_headers = ["Quarter", "Rev"]
    docling_rows = [["Q1", "100"], ["Q2", "150"]]
    vlm_parsed = ("Title", ["Quarter", "Rev"], [["Q1", "100"], ["Q2", "999"]])

    strict = reconcile_table(docling_headers, docling_rows, "Q1 100 Q2 150", vlm_parsed)
    assert strict["method"] == "pdf_grid"
    assert strict["use_vlm"] is False
    assert strict["issues"]

    # With has_merged_cells=True but unseen fraction (1/2=50%) still exceeds
    # the 8% middle tolerance -> still rejected, just via the middle-tier path
    # (message differs) rather than the strict zero-tolerance path.
    middle = reconcile_table(
        docling_headers, docling_rows, "Q1 100 Q2 150", vlm_parsed, has_merged_cells=True,
    )
    assert middle["method"] == "pdf_grid"
    assert middle["use_vlm"] is False
    assert "merged-cell" in middle["issues"][0]


def test_reconcile_merged_cells_flag_defaults_to_false_no_regression():
    docling_headers = ["Quarter", "Rev"]
    docling_rows = [["Q1", "100"], ["Q2", "200"]]
    vlm_parsed = ("Title", ["Quarter", "Rev"], [["Q1", "100"], ["Q2", "200"]])
    result = reconcile_table(docling_headers, docling_rows, "Q1 100 Q2 200", vlm_parsed)
    assert result["method"] == "pdf_grid"
    assert result["quality"] == "high"
    assert result["issues"] == []


def test_reconcile_quality_buckets():
    # high: well-formed Docling, no VLM
    r1 = reconcile_table(["A"], [["1"]], "1", vlm_parsed=None)
    assert r1["quality"] == "high"

    # low: no headers/rows at all, no VLM
    r2 = reconcile_table([], [], "", vlm_parsed=None)
    assert r2["quality"] == "low"

    # medium: rows present but ragged/no headers, no VLM
    r3 = reconcile_table([], [["1", "2"]], "", vlm_parsed=None)
    assert r3["quality"] == "medium"

    # low: faithfulness failed
    r4 = reconcile_table(
        ["A"], [["100"]], "100",
        vlm_parsed=("T", ["A"], [["999"]]),
    )
    assert r4["quality"] == "low"

    # high: ragged Docling but VLM well-formed + faithful
    r5 = reconcile_table(
        [], [["100"]], "100 200",
        vlm_parsed=("T", ["A", "B"], [["100", "200"]]),
    )
    assert r5["quality"] == "high"
    assert r5["method"] == "image_vlm"


# ── reconstruct_tables_with_vlm integration ──────────────────────────────────

def test_reconstruct_mutates_when_docling_empty_and_vlm_faithful():
    t = _table(rows=[], headers=[], raw_text="Q1 100 Q2 200")
    doc = _doc([t])

    def fake_analyze(png, ocr):
        return {
            "structured_content": _vlm_table_json(["Quarter", "Rev"], [["Q1", "100"], ["Q2", "200"]]),
            "confidence": 0.9,
        }

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert t.headers == ["Quarter", "Rev"]
    assert t.rows == [["Q1", "100"], ["Q2", "200"]]
    assert analyses[0]["method"] == "image_vlm"
    assert analyses[0]["extraction_quality"] == "high"
    assert analyses[0]["confidence"] == 0.9
    assert analyses[0]["provenance"]["use_vlm"] is True


def test_reconstruct_does_not_mutate_when_docling_wellformed_and_vlm_agrees():
    t = _table(
        rows=[["Q1", "100"], ["Q2", "200"]], headers=["Quarter", "Rev"], raw_text="Q1 100 Q2 200",
    )
    original_rows = list(t.rows)
    original_headers = list(t.headers)
    doc = _doc([t])

    def fake_analyze(png, ocr):
        return {
            "structured_content": _vlm_table_json(["Quarter", "Rev"], [["Q1", "100"], ["Q2", "200"]]),
            "confidence": 0.95,
        }

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert t.rows == original_rows          # NOT mutated — Docling stays canonical
    assert t.headers == original_headers
    assert analyses[0]["method"] == "pdf_grid"
    assert analyses[0]["confidence"] is None   # only set when VLM canonical
    assert analyses[0]["provenance"]["vlm_agreed"] is True
    assert analyses[0]["provenance"]["use_vlm"] is False


def test_reconstruct_rejects_hallucinated_vlm_and_records_issue():
    t = _table(
        rows=[["Q1", "100"], ["Q2", "150"]], headers=["Quarter", "Rev"], raw_text="Q1 100 Q2 150",
    )
    doc = _doc([t])

    def fake_analyze(png, ocr):
        # VLM invents "999" instead of the real "150"
        return {"structured_content": _vlm_table_json(["Quarter", "Rev"], [["Q1", "100"], ["Q2", "999"]])}

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert t.rows == [["Q1", "100"], ["Q2", "150"]]   # Docling kept — no hallucinated number leaks in
    assert analyses[0]["method"] == "pdf_grid"
    assert analyses[0]["extraction_quality"] == "low"
    assert analyses[0]["provenance"]["issues"]
    assert "999" in analyses[0]["provenance"]["issues"][0]


# ── reconstruct_tables_with_vlm: merged-cell signal wiring ──────────────────

def test_reconstruct_reads_merged_cells_flag_from_table_metadata():
    # table_metadata carries document_parser._detect_merged_cells' output;
    # reconstruct_tables_with_vlm must read it and apply the middle tolerance
    # instead of outright rejecting a VLM value not seen verbatim elsewhere.
    # raw_text (the OCR reference reconstruct_tables_with_vlm passes to the
    # gate) carries all but one of the VLM's numeric tokens, so only 1/13
    # (~7.7%) is unseen — within the 8% merged-cell tolerance.
    t = _table(
        rows=[["Q1", "100"], ["Q2", "150"]], headers=["Quarter", "Rev"],
        raw_text="Q1 100 Q2 150 1 2 3 4 5 6 7 8 9 10",
        table_metadata={"merged_cells": {"has_merged_cells": True, "max_row_span": 2,
                                          "max_col_span": 1, "spanned_cell_count": 1}},
    )
    doc = _doc([t])

    def fake_analyze(png, ocr):
        return {"structured_content": _vlm_table_json(
            ["Quarter", "Rev"],
            [["Q1", "100", "1", "2", "3", "4", "5", "6"], ["Q2", "150", "7", "8", "9", "10", "11"]],
        )}

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert analyses[0]["provenance"]["issues"] == []


def test_reconstruct_missing_table_metadata_defaults_to_no_merged_cells():
    # A table object with no table_metadata attribute at all (e.g. an older
    # test double / caller) must fail open to has_merged_cells=False, not
    # raise — preserves existing strict-path behavior.
    t = SimpleNamespace(
        table_index=0, image_png_bytes=b"\x89PNG", raw_text="Q1 100 Q2 150",
        headers=["Quarter", "Rev"], rows=[["Q1", "100"], ["Q2", "150"]],
        markdown_text="", caption=None,
        # deliberately no `table_metadata` attribute
    )
    doc = _doc([t])

    def fake_analyze(png, ocr):
        return {"structured_content": _vlm_table_json(["Quarter", "Rev"], [["Q1", "100"], ["Q2", "999"]])}

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert t.rows == [["Q1", "100"], ["Q2", "150"]]
    assert analyses[0]["provenance"]["issues"]
    assert "999" in analyses[0]["provenance"]["issues"][0]
