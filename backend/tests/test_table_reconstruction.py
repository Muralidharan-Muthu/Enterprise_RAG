"""Unit tests for VLM table-crop reconstruction (Task 1).

Pure: uses a lightweight fake ExtractedTable/ParsedDocument and an INJECTED
analyze_fn so no real VLM/OCR/DB is touched.
"""
import json
from types import SimpleNamespace

from app.services.table_reconstruction import (
    parse_vlm_table, rows_to_markdown, reconstruct_tables_with_vlm,
)


def _table(idx=0, img=b"\x89PNG", raw_text="Q1 100", headers=None, rows=None):
    return SimpleNamespace(
        table_index=idx,
        image_png_bytes=img,
        raw_text=raw_text,
        headers=headers if headers is not None else ["A"],
        rows=rows if rows is not None else [["docling"]],
        markdown_text="| A |\n| --- |\n| docling |",
        caption=None,
    )


def _doc(tables):
    return SimpleNamespace(doc_id="doc-1", tables=tables)


def _vlm_table_json(headers, rows, title="T"):
    return json.dumps({"title": title, "headers": headers, "rows": rows})


# ── parse_vlm_table ──────────────────────────────────────────────────────────

def test_parse_valid_json():
    out = parse_vlm_table(_vlm_table_json(["Q", "Rev"], [["Q1", "100"], ["Q2", "200"]]))
    assert out == ("T", ["Q", "Rev"], [["Q1", "100"], ["Q2", "200"]])


def test_parse_strips_code_fence():
    fenced = "```json\n" + _vlm_table_json(["A"], [["1"]]) + "\n```"
    assert parse_vlm_table(fenced) == ("T", ["A"], [["1"]])


def test_parse_coerces_cells_to_str():
    out = parse_vlm_table(json.dumps({"headers": ["n"], "rows": [[1], [2.5]]}))
    assert out == (None, ["n"], [["1"], ["2.5"]])


def test_parse_non_json_returns_none():
    assert parse_vlm_table("just some prose, not a table") is None


def test_parse_empty_returns_none():
    assert parse_vlm_table("") is None
    assert parse_vlm_table("   ") is None


def test_parse_dict_without_rows_returns_none():
    assert parse_vlm_table(json.dumps({"headers": ["a"], "rows": []})) is None
    assert parse_vlm_table(json.dumps({"headers": ["a"]})) is None


def test_parse_rows_not_list_returns_none():
    assert parse_vlm_table(json.dumps({"rows": "nope"})) is None


# ── rows_to_markdown ─────────────────────────────────────────────────────────

def test_markdown_with_headers():
    md = rows_to_markdown(["A", "B"], [["1", "2"], ["3", "4"]])
    assert md.splitlines()[0] == "| A | B |"
    assert md.splitlines()[1] == "| --- | --- |"
    assert "| 1 | 2 |" in md


def test_markdown_without_headers():
    md = rows_to_markdown([], [["x", "y"]])
    assert md == "| x | y |"


# ── reconstruct_tables_with_vlm ──────────────────────────────────────────────

def test_reconstruct_replaces_table_from_vlm():
    # Slice 2b: the VLM only becomes canonical when the Docling grid is
    # empty/ragged AND the VLM's numbers are faithful to the reference
    # (Docling rows + crop OCR text). Empty Docling grid here (no native
    # extraction) so a faithful VLM table wins.
    t = _table(rows=[], headers=[], raw_text="Q1 100 Q2 200")
    doc = _doc([t])

    def fake_analyze(png, ocr):
        return {"structured_content": _vlm_table_json(["Quarter", "Rev"], [["Q1", "100"], ["Q2", "200"]]),
                "vlm_ocr_text": "vlm text"}

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert t.headers == ["Quarter", "Rev"]
    assert t.rows == [["Q1", "100"], ["Q2", "200"]]
    assert "Quarter" in t.markdown_text and "Q1" in t.markdown_text
    assert t.raw_text == "Q1 100 Q2 200"   # original Docling text preserved for audit
    assert analyses[0]["vlm_ocr_text"] == "vlm text"
    assert analyses[0]["method"] == "image_vlm"


def test_reconstruct_keeps_docling_when_vlm_not_a_table():
    t = _table(rows=[["docling"]], headers=["A"])
    doc = _doc([t])

    def fake_analyze(png, ocr):
        return {"structured_content": "This is prose, not a table."}

    reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert t.rows == [["docling"]]         # unchanged — no regression
    assert t.headers == ["A"]


def test_reconstruct_keeps_docling_when_vlm_raises():
    t = _table(rows=[["docling"]])
    doc = _doc([t])

    def boom(png, ocr):
        raise RuntimeError("VLM down")

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=boom)
    assert t.rows == [["docling"]]
    # analyze_fn raised -> analysis starts empty, but reconciliation still runs
    # (no VLM table -> keep Docling) and enriches it with the verdict.
    assert analyses[0]["method"] == "pdf_grid"
    assert "structured_content" not in analyses[0]
    assert analyses[0]["provenance"]["use_vlm"] is False


def test_reconstruct_skips_tables_without_image():
    t = _table(idx=0)
    t_noimg = _table(idx=1, img=None)
    doc = _doc([t, t_noimg])

    def fake_analyze(png, ocr):
        return {"structured_content": _vlm_table_json(["A"], [["1"]])}

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze)
    assert 0 in analyses and 1 not in analyses      # table without image bytes skipped


def test_reconstruct_parallel_processes_all():
    # Empty Docling grid on every table so a faithful VLM table is canonical
    # for each — this isolates the thing under test (per-table VLM results
    # stay correctly aligned under concurrency) from the faithfulness gate.
    tables = [_table(idx=i, raw_text=f"t{i}", headers=[], rows=[]) for i in range(5)]
    doc = _doc(tables)

    def fake_analyze(png, ocr):
        return {"structured_content": _vlm_table_json(["A"], [[ocr]])}

    analyses = reconstruct_tables_with_vlm(doc, analyze_fn=fake_analyze, max_workers=4)
    assert set(analyses.keys()) == {0, 1, 2, 3, 4}
    for i, t in enumerate(tables):
        assert t.rows == [[f"t{i}"]]        # each table got its own VLM result, aligned
        assert analyses[i]["method"] == "image_vlm"
