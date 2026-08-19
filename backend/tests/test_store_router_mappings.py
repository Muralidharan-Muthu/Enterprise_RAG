"""Regression tests for the schema-completeness fixes in store_router.

Locks in the three "missing mapping" gaps fixed on 2026-06-30:
  - table_store: VLM `notes` -> context_after (was parsed but never written)
  - clause_store: clause_number + clause_subtype (were absent)
  - document_store: contains_hypothesis/finding/method (were never populated)

Pure-Python: a fake cursor records the INSERT SQL + params; no live DB.
"""
import json

import pytest

from app.services.store_router import ImageCtx, get_handler


class _FakeCursor:
    def __init__(self):
        self.sql = None
        self.params = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        if sql.strip().upper().startswith("INSERT"):
            self.sql, self.params = sql, params

    def fetchone(self):
        return ("non-empty", True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur


def _ctx(store):
    return ImageCtx(
        document_id="doc-1", image_id="img-1", image_index=3, page_number=5,
        bbox_json=None, storage_path="images/doc-1/3.png",
        ocr_text="ocr", vlm_ocr_text="vlm", detected_store=store,
        confidence=0.9, reason="r",
    )


EMB = [0.0] * 1024


def _payload(store, vlm_obj):
    handler = get_handler(store)
    ctx = _ctx(store)
    parsed = handler.parse(json.dumps(vlm_obj), ctx)
    conn = _FakeConn()
    n = handler.insert(conn, parsed, EMB, ctx)
    assert n == 1
    cols = conn.cur.sql.split("(", 1)[1].split(")")[0]
    return cols, conn.cur.params, parsed


def test_table_notes_written_to_context_after():
    cols, params, parsed = _payload("table_store", {
        "title": "T", "headers": ["A"], "rows": [["1"]],
        "units": "USD", "notes": "see footnote",
    })
    assert "context_after" in cols
    assert "context_before" in cols
    assert "see footnote" in params           # notes landed in the payload
    assert parsed["notes"] == "see footnote"
    # full grid populated
    assert "detected_units" in cols
    assert ["USD"] in params                  # detected_units TEXT[]


def test_clause_number_and_subtype_written():
    cols, params, _ = _payload("clause_store", {
        "clause_title": "LoL", "clause_number": "12.3.1",
        "clause_subtype": "cap", "clause_text": "x", "clause_type": "liability",
    })
    assert "clause_number" in cols and "clause_subtype" in cols
    assert "12.3.1" in params
    assert "cap" in params


@pytest.mark.skip(reason="document_store deprecated and removed in migration 022")
def test_document_research_flags_written():
    cols, params, _ = _payload("document_store", {
        "chunk_text": "we hypothesise X", "chunk_type": "methodology",
        "contains_hypothesis": True, "contains_finding": False, "contains_method": True,
    })


def test_table_plaintext_fallback_has_no_notes():
    # Plain text (non-JSON) -> notes None, context_after None
    cols, params, parsed = _payload("table_store", "just some text")  # not a dict
    # _payload json.dumps a string -> handler parses the JSON string, not a dict -> fallback
    assert parsed.get("notes") is None
