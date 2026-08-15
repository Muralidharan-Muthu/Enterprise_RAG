"""Universal VLM table pipeline — structured_content persistence + crop fallback."""
from types import SimpleNamespace
from contextlib import contextmanager

import numpy as np

import app.services.storage_service as ss
import app.services.document_parser as dp


def _table(idx, headers, rows, markdown="| a |\n| - |\n| 1 |", raw="a 1"):
    return SimpleNamespace(
        table_index=idx, headers=headers, rows=rows, page_number=1,
        markdown_text=markdown, raw_text=raw, caption=None, bbox=None,
        table_metadata={}, row_page_numbers=None,
    )


def _capture_store_tables(monkeypatch, parsed_doc, **kwargs):
    """Call _store_tables with get_db + execute_values mocked; return the row
    tuples that would have been inserted."""
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    @contextmanager
    def fake_get_db():
        conn = SimpleNamespace(cursor=lambda: FakeCursor())
        yield conn

    def fake_execute_values(cur, sql, rows, template=None, page_size=None, fetch=None):
        captured["rows"] = rows
        return [(f"id-{i}",) for i in range(len(rows))]

    monkeypatch.setattr(ss, "get_db", fake_get_db)
    monkeypatch.setattr(ss.psycopg2.extras, "execute_values", fake_execute_values)

    # avoid any LLM/rules enrichment cost: supply an all-None enrichment per table
    enr = {t.table_index: {} for t in parsed_doc.tables}
    ss._store_tables(document_id="doc1", parsed_doc=parsed_doc,
                     table_enrichment=enr, **kwargs)
    return captured["rows"]


def test_structured_content_persisted_from_vlm(monkeypatch):
    parsed = SimpleNamespace(tables=[_table(0, ["a"], [["1"]])])
    sc = {0: '{"rows": [{"a": 1}]}'}
    emb = np.ones((1, 1024), dtype="float32")
    rows = _capture_store_tables(
        monkeypatch, parsed,
        table_structured_content=sc, table_sc_embeddings=emb,
    )
    # structured_content and its embedding are the last two tuple elements.
    assert rows[0][-2] == '{"rows": [{"a": 1}]}'
    assert rows[0][-1] == emb[0].tolist()


def test_structured_content_falls_back_to_markdown(monkeypatch):
    parsed = SimpleNamespace(tables=[_table(0, ["a"], [["1"]], markdown="MD-FALLBACK")])
    rows = _capture_store_tables(monkeypatch, parsed)  # no VLM sc, no sc embeddings
    assert rows[0][-2] == "MD-FALLBACK"
    assert rows[0][-1] is None  # no embedding provided → NULL


def test_render_crop_fitz_failopen_on_bad_input():
    # No pdf_path → None; bad table object → None (never raises).
    assert dp._render_table_crop_fitz(None, object(), object()) is None
    bad_table = SimpleNamespace(prov=[])
    assert dp._render_table_crop_fitz("/nonexistent.pdf", bad_table, object()) is None
