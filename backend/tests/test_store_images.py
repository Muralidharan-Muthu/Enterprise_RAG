"""Tests for _image_rows tuple order matching the 21-column contract.

image_store is a PURE repository (migration 008): NO embedding column. The tuple
is 21 columns and _image_rows takes (document_id, records) only. asset_role
(migration 014) is the last column and is always "figure" for _image_rows —
table crops are stamped "table_crop" separately in store_table_crop_images.
"""
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.storage_service import _image_rows


def test_store_tables_writes_lineage_columns():
    """Slice 2a: _store_tables writes extraction_method (+quality/confidence/
    provenance) at insert time from the threaded dicts, and defaults to 'pdf_grid'/
    NULL when a table_index is absent from the dicts.

    source_image_id is always NULL from this function: _store_tables() never sets
    from_image_store (it stays at its column default, FALSE), and source_image_id
    is only meaningful for rows where from_image_store=TRUE (written by the
    image-cross-store pathway in store_router.py instead)."""
    import app.services.storage_service as svc

    captured = {}

    def fake_execute_values(cur, sql, rows, template=None, page_size=None, fetch=False):
        captured["sql"] = sql
        captured["rows"] = rows
        return [(f"uuid-{i}",) for i in range(len(rows))]

    @contextmanager
    def fake_get_db():
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        yield conn

    tables = [
        SimpleNamespace(table_index=0, caption="T0", page_number=1, bbox=None,
                        raw_text="r0", markdown_text="m0", headers=["A"], rows=[["1"]]),
        SimpleNamespace(table_index=1, caption="T1", page_number=2, bbox=None,
                        raw_text="r1", markdown_text="m1", headers=["B"], rows=[["2"]]),
    ]
    parsed = SimpleNamespace(tables=tables)

    with patch("app.services.storage_service.get_db", fake_get_db), \
         patch("app.services.storage_service.psycopg2.extras.execute_values",
               side_effect=fake_execute_values):
        svc._store_tables(
            "doc", parsed,
            table_source_image_ids={0: "img-uuid-0"},          # table 1 omitted -> NULL
            table_extraction={0: {"method": "image_vlm", "confidence": 0.9,
                                  "provenance": {"reconstructed": True}}},
        )

    sql = captured["sql"]
    assert "source_image_id" in sql and "extraction_method" in sql and "provenance" in sql
    r0, r1 = captured["rows"]
    # column order: ..., image_storage_path(14), embedding(15), source_image_id(16),
    # extraction_method(17), extraction_quality(18), source_confidence(19), provenance(20).
    # Slice 3's 6 enrichment columns append AFTER provenance (indices 21-26), so
    # these lineage indices are unaffected by that change.
    assert r0[16] is None and r0[17] == "image_vlm"    # source_image_id always NULL here
    assert r0[18] == "high" and r0[19] == 0.9          # bucket(0.9) == high
    assert r1[16] is None and r1[17] == "pdf_grid"     # omitted table -> defaults
    assert r1[19] is None


def test_store_tables_writes_enrichment_columns_from_threaded_dict():
    """Slice 3: _store_tables writes fiscal_year/reporting_period/currency/
    table_category/detected_units/table_summary from the caller-supplied
    table_enrichment dict, appended after provenance (indices 21-26)."""
    import app.services.storage_service as svc

    captured = {}

    def fake_execute_values(cur, sql, rows, template=None, page_size=None, fetch=False):
        captured["sql"] = sql
        captured["rows"] = rows
        captured["template"] = template
        return [(f"uuid-{i}",) for i in range(len(rows))]

    @contextmanager
    def fake_get_db():
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        yield conn

    tables = [
        SimpleNamespace(table_index=0, caption="T0", page_number=1, bbox=None,
                        raw_text="r0", markdown_text="m0", headers=["A"], rows=[["1"]]),
        SimpleNamespace(table_index=1, caption="T1", page_number=2, bbox=None,
                        raw_text="r1", markdown_text="m1", headers=["B"], rows=[["2"]]),
    ]
    parsed = SimpleNamespace(tables=tables)

    enrichment = {
        0: {
            "fiscal_year": "FY2024", "reporting_period": "Q1 2024",
            "currency": "USD", "table_category": "income_statement",
            "detected_units": ["USD millions"], "table_summary": "T0 summary",
        },
    }

    with patch("app.services.storage_service.get_db", fake_get_db), \
         patch("app.services.storage_service.psycopg2.extras.execute_values",
               side_effect=fake_execute_values):
        svc._store_tables("doc", parsed, table_enrichment=enrichment)

    sql = captured["sql"]
    for col in ("fiscal_year", "reporting_period", "currency", "table_category",
                "detected_units", "table_summary"):
        assert col in sql

    # 30 columns total (21 pre-Slice-3 + 6 enrichment + 1 table_metadata +
    # structured_content + structured_content_embedding); template placeholders
    # and each row tuple must all agree on that count.
    assert captured["template"].count("%s") == 30

    r0, r1 = captured["rows"]
    assert len(r0) == 30 and len(r1) == 30
    # structured_content(28) + structured_content_embedding(29) are last; with no
    # VLM sc threaded and no sc embeddings, they fall back to markdown_text / None.
    assert r0[28] == "m0" and r0[29] is None
    assert r1[28] == "m1" and r1[29] is None

    # Enrichment columns appended AFTER provenance(20): fiscal_year(21),
    # reporting_period(22), currency(23), table_category(24), detected_units(25),
    # table_summary(26). table_metadata(27) is the deferred merged-cell-span
    # capture (document_parser._detect_merged_cells) — empty here since these
    # SimpleNamespace test doubles have no table_metadata attribute.
    assert r0[21] == "FY2024"
    assert r0[22] == "Q1 2024"
    assert r0[23] == "USD"
    assert r0[24] == "income_statement"
    assert r0[25] == ["USD millions"]
    assert r0[26] == "T0 summary"
    assert r0[27] == "{}"

    # Table 1 was omitted from the enrichment dict — _store_tables must fall
    # back to enrich_table() internally rather than write NULLs across the board.
    assert r1[26]              # table_summary always non-empty via the fallback
    assert r1[24] == "other"   # table_category defaults to 'other' when un-derivable


def test_store_tables_enrichment_fallback_when_dict_missing_entirely():
    """A direct _store_tables() call with NO table_enrichment kwarg at all
    (e.g. a caller that bypasses the orchestrator) must still populate
    table_summary via the internal enrich_table() fallback for every table."""
    import app.services.storage_service as svc

    captured = {}

    def fake_execute_values(cur, sql, rows, template=None, page_size=None, fetch=False):
        captured["rows"] = rows
        return [(f"uuid-{i}",) for i in range(len(rows))]

    @contextmanager
    def fake_get_db():
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        yield conn

    tables = [
        SimpleNamespace(table_index=0, caption="Balance Sheet FY2024", page_number=1, bbox=None,
                        raw_text="r0", markdown_text="m0", headers=["Assets", "Liabilities"],
                        rows=[["$100", "$50"]]),
    ]
    parsed = SimpleNamespace(tables=tables)

    with patch("app.services.storage_service.get_db", fake_get_db), \
         patch("app.services.storage_service.psycopg2.extras.execute_values",
               side_effect=fake_execute_values):
        svc._store_tables("doc", parsed)   # no table_enrichment kwarg at all

    (row,) = captured["rows"]
    assert len(row) == 30
    assert row[26]                          # table_summary non-empty
    assert row[24] == "balance_sheet"       # derived from caption keyword
    assert row[27] == "{}"                  # table_metadata: no attribute on this double -> {}
    assert row[23] == "USD"                 # derived from "$" in cell
    assert row[21] == "FY2024"              # derived from caption regex
    assert row[28] == "m0"                  # structured_content falls back to markdown_text
    assert row[29] is None                  # no structured_content_embedding provided


def test_image_rows_shapes_and_serialization():
    """_image_rows must produce the 16-col tuple order:
    (document_id, image_index, page_number, bbox, storage_path, storage_bucket,
     mime_type, width, height, ocr_text, vlm_ocr_text, structured_content,
     image_metadata, content_type, detected_store, stored_in)
    """
    records = [{
        "image_index": 0, "page_number": 3,
        "bbox": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
        "storage_path": "images/doc1/0.png", "storage_bucket": "rag-documents",
        "mime_type": "image/png", "width": 100, "height": 50,
        "ocr_text": "Q1 10",
        "vlm_ocr_text": "Q1 10 (vlm)",
        "structured_content": "Revenue table Q1=10",
        "content_type": "table",
        "detected_store": "table_store",
        "image_metadata": {"confidence": 0.9, "reason_for_store_selection": "table"},
    }]
    rows = _image_rows("doc1", records)
    assert len(rows) == 1
    row = rows[0]

    assert len(row) == 21              # NO embedding; +4 prefilter tracking cols; +asset_role
    assert row[0] == "doc1"           # document_id
    assert row[1] == 0                 # image_index
    assert row[2] == 3                 # page_number
    assert '"x1": 1' in row[3]        # bbox JSON
    assert row[4] == "images/doc1/0.png"   # storage_path
    assert row[5] == "rag-documents"  # storage_bucket
    assert row[6] == "image/png"      # mime_type
    assert row[7] == 100              # width
    assert row[8] == 50               # height
    assert row[9] == "Q1 10"          # ocr_text (index 9)
    assert row[10] == "Q1 10 (vlm)"   # vlm_ocr_text (index 10)
    assert row[11] == "Revenue table Q1=10"  # structured_content (index 11)
    assert '"confidence"' in row[12]  # image_metadata JSON (index 12)
    assert row[13] == "table"         # content_type (index 13)
    assert row[14] == "table_store"   # detected_store (index 14)
    assert row[15] == "image_store"   # stored_in — starts at image_store; flipped to the
                                       # destination store by store_image_derived_chunks when routed
    # ── prefilter tracking columns (default when record omits them) ──
    assert row[16] == "VLM_PROCESSED"  # processing_status (default)
    assert row[17] is None            # skip_reason
    assert row[18] is None            # filter_stage
    assert row[19] is None            # image_type
    assert row[20] == "figure"        # asset_role (migration 014)


def test_image_rows_no_caption_key():
    """Records must not require a caption key; there is no caption in the tuple."""
    records = [{
        "image_index": 1, "page_number": 1,
        "bbox": None,
        "storage_path": "images/doc2/1.png", "storage_bucket": "rag-documents",
        "mime_type": "image/png", "width": 200, "height": 100,
        "ocr_text": "some ocr",
        "vlm_ocr_text": "some ocr vlm",
        "structured_content": "Policy text about data governance",
        "content_type": "text",
        "detected_store": "vector_store",
        "image_metadata": {},
    }]
    rows = _image_rows("doc2", records)
    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 21
    assert row[14] == "vector_store"          # detected_store
    assert row[11] == "Policy text about data governance"  # structured_content
    assert row[20] == "figure"                # asset_role (migration 014)


def test_image_rows_defaults_detected_store_to_image_store():
    """When detected_store is missing from record, tuple defaults to image_store."""
    records = [{
        "image_index": 2, "page_number": 5,
        "bbox": None,
        "storage_path": "images/doc3/2.png", "storage_bucket": "rag-documents",
        "mime_type": "image/png", "width": 50, "height": 50,
        "ocr_text": "",
        "vlm_ocr_text": "",
        "structured_content": "",
        "content_type": "figure",
        "image_metadata": {},
        # no detected_store key
    }]
    rows = _image_rows("doc3", records)
    row = rows[0]
    assert row[14] == "image_store"


def test_store_table_crop_images_asset_role():
    """store_table_crop_images must stamp asset_role="table_crop" as the LAST
    element of every image_store row it inserts (migration 014). Pure/mock —
    no live DB: get_db, insert_images, and the table_store ensure-helper are
    all patched."""
    import app.services.storage_service as svc

    @contextmanager
    def fake_get_db():
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        # SELECT image_index, id FROM image_store ... -> empty is fine, the
        # crop_image_id mapping is not under test here.
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        yield conn

    captured_rows = {}

    def fake_insert_images(rows):
        captured_rows["rows"] = rows
        return len(rows)

    records = [{
        "table_index": 0, "page_number": 2,
        "bbox": {"x1": 0, "y1": 0, "x2": 10, "y2": 10},
        "storage_path": "tables/doc4/0.png", "storage_bucket": "rag-documents",
        "caption": "Table 1", "ocr_text": "Revenue 100",
    }]
    embeddings = [MagicMock()]

    with patch("app.services.storage_service.get_db", fake_get_db), \
         patch("app.db.repositories.image_store.insert_images", side_effect=fake_insert_images):
        # Slice 2a: returns a {table_index: image_store_id} map; no _ensure_ backfill.
        result = svc.store_table_crop_images("doc4", records, embeddings)

    rows = captured_rows["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 21
    assert row[-1] == "table_crop"   # asset_role (migration 014), last column
    assert isinstance(result, dict)  # returns the table_index -> image_store_id map


@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("RUN_DB_TESTS"), reason="needs live DB + a real document_id")
def test_insert_and_query_image():
    doc_id = os.environ["RUN_DB_TESTS"]  # set to an existing document_registry.id
    from app.db.connection import get_db
    from app.services.storage_service import store_images
    rec = [{
        "image_index": 999, "page_number": 1, "bbox": None,
        "storage_path": f"images/{doc_id}/test.png", "storage_bucket": "rag-documents",
        "mime_type": "image/png", "width": 10, "height": 10,
        "ocr_text": "pytest raw ocr",
        "vlm_ocr_text": "pytest vlm ocr",
        "structured_content": "pytest structured content",
        "content_type": "figure",
        "detected_store": "image_store",
        "image_metadata": {},
    }]
    store_images(doc_id, rec)
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT structured_content, ocr_text, vlm_ocr_text, detected_store "
            "FROM multi_store_rag_working.image_store WHERE document_id=%s AND image_index=999",
            (doc_id,),
        )
        row = cur.fetchone()
        assert row[0] == "pytest structured content"
        assert row[3] == "image_store"
        cur.execute(
            "DELETE FROM multi_store_rag_working.image_store WHERE document_id=%s AND image_index=999",
            (doc_id,),
        )
