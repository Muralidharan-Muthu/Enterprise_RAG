"""Slice 4 — hybrid metadata-filtered table retrieval.

table_store now has populated fiscal_year, currency, table_category,
has_numeric_data, extraction_quality (migration 014 + Slices 1-3). This adds
an OPTIONAL structured prefilter on those columns, applied BEFORE the vector
ANN in _query_table_store / _query_table_store_parent_only.

These tests:
  1. Unit-test the WHERE-fragment builder (_table_filter_sql) in isolation.
  2. Verify _query_table_store / _query_table_store_parent_only issue
     byte-identical SQL shape when table_filters is None (no extra predicate),
     and include the predicate + params when filters are supplied.

No live DB: conn/cursor are mocked (same pattern as test_store_router.py).
No embeddings needed: a plain numpy vector stands in for a real query embedding.
"""
from unittest.mock import MagicMock

import numpy as np

import app.services.retriever_service as rs
from app.services.retriever_service import TableFilters, _table_filter_sql


# ── 1. WHERE-fragment builder ───────────────────────────────────────────────

def test_empty_filters_produce_no_predicate():
    sql, params = _table_filter_sql(None, alias="ts")
    assert sql == ""
    assert params == []


def test_default_table_filters_is_empty_noop():
    sql, params = _table_filter_sql(TableFilters(), alias="ts")
    assert sql == ""
    assert params == []


def test_currency_predicate():
    sql, params = _table_filter_sql(TableFilters(currency="USD"), alias="ts")
    assert sql == "AND ts.currency = %s"
    assert params == ["USD"]


def test_fiscal_year_predicate():
    sql, params = _table_filter_sql(TableFilters(fiscal_year="FY2024"), alias="ts")
    assert sql == "AND ts.fiscal_year = %s"
    assert params == ["FY2024"]


def test_table_category_predicate():
    sql, params = _table_filter_sql(TableFilters(table_category="income_statement"), alias="ts")
    assert sql == "AND ts.table_category = %s"
    assert params == ["income_statement"]


def test_numeric_only_predicate_has_no_param():
    sql, params = _table_filter_sql(TableFilters(numeric_only=True), alias="ts")
    assert sql == "AND ts.has_numeric_data = TRUE"
    assert params == []


def test_min_quality_medium_expands_to_medium_and_high():
    sql, params = _table_filter_sql(TableFilters(min_quality="medium"), alias="ts")
    assert sql == "AND ts.extraction_quality IN (%s, %s)"
    assert params == ["medium", "high"]


def test_min_quality_high_is_single_tier():
    sql, params = _table_filter_sql(TableFilters(min_quality="high"), alias="ts")
    assert sql == "AND ts.extraction_quality IN (%s)"
    assert params == ["high"]


def test_min_quality_low_includes_all_tiers():
    sql, params = _table_filter_sql(TableFilters(min_quality="low"), alias="ts")
    assert sql == "AND ts.extraction_quality IN (%s, %s, %s)"
    assert params == ["low", "medium", "high"]


def test_min_quality_case_insensitive():
    sql, params = _table_filter_sql(TableFilters(min_quality="MEDIUM"), alias="ts")
    assert sql == "AND ts.extraction_quality IN (%s, %s)"
    assert params == ["medium", "high"]


def test_invalid_min_quality_is_ignored_fail_open():
    # Malformed value → dropped, not raised. No predicate, no params.
    sql, params = _table_filter_sql(TableFilters(min_quality="platinum"), alias="ts")
    assert sql == ""
    assert params == []


def test_combined_filters_join_with_and_and_preserve_param_order():
    filters = TableFilters(
        currency="USD", fiscal_year="FY2024", table_category="income_statement",
        numeric_only=True, min_quality="medium",
    )
    sql, params = _table_filter_sql(filters, alias="ts")
    assert sql == (
        "AND ts.currency = %s AND ts.fiscal_year = %s AND ts.table_category = %s "
        "AND ts.has_numeric_data = TRUE AND ts.extraction_quality IN (%s, %s)"
    )
    assert params == ["USD", "FY2024", "income_statement", "medium", "high"]


def test_alias_is_respected():
    sql, _ = _table_filter_sql(TableFilters(currency="USD"), alias="table_store")
    assert sql == "AND table_store.currency = %s"


def test_table_filters_is_empty_helper():
    assert TableFilters().is_empty() is True
    assert TableFilters(currency="USD").is_empty() is False
    assert TableFilters(numeric_only=True).is_empty() is False
    assert TableFilters(min_quality="high").is_empty() is False


# ── 2. _query_table_store / _query_table_store_parent_only SQL shape ───────

def _make_conn():
    """Mock conn whose cursor() supports `with conn.cursor() as cur:` and
    records every executed (sql, params) pair. fetchall() returns [] so the
    RetrievedChunk-building loop is a no-op — we only care about the SQL sent."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _embedding():
    return np.zeros(8, dtype=np.float32)


def test_parent_only_no_filters_matches_pre_slice4_sql(monkeypatch):
    """With table_filters=None, the parent-only query must NOT contain any of
    the Slice 4 filter columns — i.e. it's identical to pre-Slice-4 SQL."""
    conn, cur = _make_conn()
    rs._query_table_store_parent_only(conn, _embedding(), None, None, 15)

    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]

    for col in ("currency", "fiscal_year", "table_category", "has_numeric_data", "extraction_quality"):
        assert col not in sql
    # emb, emb, top_k only (no type/doc/filter params supplied)
    assert params[-1] == 15


def test_parent_only_with_filters_adds_predicate_and_params(monkeypatch):
    conn, cur = _make_conn()
    filters = TableFilters(currency="USD", fiscal_year="FY2024")
    rs._query_table_store_parent_only(conn, _embedding(), None, None, 15, table_filters=filters)

    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]

    assert "ts.currency = %s" in sql
    assert "ts.fiscal_year = %s" in sql
    assert "USD" in params
    assert "FY2024" in params
    assert params[-1] == 15  # LIMIT is still the last param


def test_query_table_store_child_path_no_filters_matches_pre_slice4_sql(monkeypatch):
    """TABLE_CHILD_SEARCH_ENABLED path (default True): no filters => the child
    JOIN query has no Slice 4 filter columns either."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

    conn, cur = _make_conn()
    rs._query_table_store(conn, _embedding(), None, None, 15)

    # Two SELECTs are issued: the child JOIN query, then the parent-only fallback.
    all_sqls = [call.args[0] for call in cur.execute.call_args_list]
    assert len(all_sqls) == 2
    for sql in all_sqls:
        for col in ("has_numeric_data", "extraction_quality", "table_category"):
            assert col not in sql


def test_query_table_store_child_path_with_filters_applies_to_both_queries(monkeypatch):
    """Filters must reach BOTH the child-join query and the parent-only
    fallback, so filtered tables stay consistent across both paths."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

    conn, cur = _make_conn()
    filters = TableFilters(table_category="income_statement", numeric_only=True)
    rs._query_table_store(conn, _embedding(), None, None, 15, table_filters=filters)

    all_calls = cur.execute.call_args_list
    assert len(all_calls) == 2
    for call in all_calls:
        sql, params = call.args[0], call.args[1]
        assert "ts.table_category = %s" in sql
        assert "ts.has_numeric_data = TRUE" in sql
        assert "income_statement" in params


def test_query_table_store_disabled_child_search_delegates_with_filters(monkeypatch):
    """TABLE_CHILD_SEARCH_ENABLED=False → single parent-only call, filters
    still forwarded."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", False)

    conn, cur = _make_conn()
    filters = TableFilters(currency="INR")
    rs._query_table_store(conn, _embedding(), None, None, 15, table_filters=filters)

    assert cur.execute.call_count == 1
    sql, params = cur.execute.call_args[0]
    assert "ts.currency = %s" in sql
    assert "INR" in params


def test_query_table_store_disabled_child_search_no_filters_unchanged(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", False)

    conn, cur = _make_conn()
    rs._query_table_store(conn, _embedding(), None, None, 15)

    assert cur.execute.call_count == 1
    sql = cur.execute.call_args[0][0]
    for col in ("currency", "fiscal_year", "table_category", "has_numeric_data", "extraction_quality"):
        assert col not in sql


# ── 3. Multi-window-per-table dedup (top-K instead of top-1) ───────────────
#
# child_sql SELECT list (see _query_table_store):
#   0 chunk_id, 1 document_id, 2 serialized_text, 3 page_number, 4 markdown_text,
#   5 distance, 6 filename, 7 document_type, 8 storage_path, 9 storage_bucket,
#   10 bbox, 11 table_id, 12 chunk_metadata

def _child_row(chunk_id, table_id, distance, page=1, chunk_metadata=None):
    return (chunk_id, "doc-1", f"text-{chunk_id}", page, "md", distance,
            "file.pdf", "financial", "path", "bucket", None, table_id, chunk_metadata)


def _make_conn_with_child_rows(child_rows):
    """First cursor.execute()/fetchall() call (child JOIN) returns child_rows;
    the second call (parent-only fallback) returns no rows, so no parent
    windows get appended and we can assert purely on child-window dedup
    behaviour."""
    cur = MagicMock()
    cur.fetchall.side_effect = [child_rows, []]
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_table_with_multiple_close_windows_returns_up_to_cap(monkeypatch):
    """A table with 3 genuinely close-distance windows should return up to the
    configured cap (default 2), not just the single best one."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TABLE_MAX_WINDOWS_PER_QUERY_RESULT", 2)

    child_rows = [
        _child_row("c1", "table-A", 0.10),
        _child_row("c2", "table-A", 0.12),
        _child_row("c3", "table-A", 0.15),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    table_a_results = [r for r in results if r.chunk_id in ("c1", "c2", "c3")]
    assert len(table_a_results) == 2
    # Keeps the two lowest-distance windows (c1, c2), not c3.
    assert {r.chunk_id for r in table_a_results} == {"c1", "c2"}


def test_table_with_configured_cap_of_three(monkeypatch):
    """Cap is configurable — with cap=3, all 3 close windows survive."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TABLE_MAX_WINDOWS_PER_QUERY_RESULT", 3)

    child_rows = [
        _child_row("c1", "table-A", 0.10),
        _child_row("c2", "table-A", 0.12),
        _child_row("c3", "table-A", 0.15),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    assert {r.chunk_id for r in results} == {"c1", "c2", "c3"}


def test_table_with_one_relevant_window_no_padding_with_far_windows(monkeypatch):
    """Backward compatibility: a table with 1 close window + others far away
    still returns only what survives top-K-by-distance — this is not a
    'pad up to cap regardless of relevance' behavior, just top-K per table.
    Confirms the common case (only one truly good window) is unaffected in
    spirit: the closest windows win, ordering by distance is preserved."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TABLE_MAX_WINDOWS_PER_QUERY_RESULT", 2)

    child_rows = [
        _child_row("c1", "table-B", 0.05),   # only genuinely close window
        _child_row("c2", "table-B", 0.90),   # far, but still top-2 by distance
        _child_row("c3", "table-B", 0.95),   # far, excluded by cap
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    # Cap keeps 2 (c1, c2) — c3 dropped. No 3rd "padding" window appears.
    assert len(results) == 2
    assert {r.chunk_id for r in results} == {"c1", "c2"}
    # Still sorted best-first.
    assert results[0].chunk_id == "c1"


def test_single_window_table_behavior_identical_to_before(monkeypatch):
    """A table with exactly one window returns exactly that one window,
    regardless of cap — no behavior change from the pre-fix single-window
    dedup for the common case."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TABLE_MAX_WINDOWS_PER_QUERY_RESULT", 2)

    child_rows = [_child_row("only1", "table-C", 0.20)]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    assert len(results) == 1
    assert results[0].chunk_id == "only1"


def test_overall_top_k_cap_respected_across_multiple_tables(monkeypatch):
    """Even though multiple tables may each contribute up to cap windows, the
    final result list must still be truncated to top_k overall, sorted by
    distance."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TABLE_MAX_WINDOWS_PER_QUERY_RESULT", 2)

    child_rows = [
        _child_row("a1", "table-A", 0.05),
        _child_row("a2", "table-A", 0.06),
        _child_row("b1", "table-B", 0.07),
        _child_row("b2", "table-B", 0.08),
        _child_row("c1", "table-C", 0.09),
        _child_row("c2", "table-C", 0.10),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    top_k = 3
    results = rs._query_table_store(conn, _embedding(), None, None, top_k)

    assert len(results) == top_k
    # Must be the 3 globally closest windows, in ascending distance order.
    assert [r.chunk_id for r in results] == ["a1", "a2", "b1"]
    assert results == sorted(results, key=lambda r: r.distance)


def test_default_cap_is_two(monkeypatch):
    """Sanity check on the shipped default: TABLE_MAX_WINDOWS_PER_QUERY_RESULT
    defaults to 2 (not reverting to the old hard 1-per-table behavior, and not
    left unbounded)."""
    from app.config import settings
    assert settings.TABLE_MAX_WINDOWS_PER_QUERY_RESULT == 2


# ── 4. Phase 2 — page_number_end threading for continuation-merged tables ──
#
# Phase 1 (document_parser._merge_continued_tables + table_chunker.
# build_row_windows) stamps chunk_metadata={'page_start': N, 'page_end': M} on
# table_chunk_store windows that span more than one source page. These tests
# confirm the retriever exposes both ends via RetrievedChunk.page_number /
# page_number_end, and that the citation layer threads it through.

def test_continuation_merged_window_exposes_page_start_and_end(monkeypatch):
    """A window whose chunk_metadata carries page_start != page_end must
    surface page_number=page_start and page_number_end=page_end."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

    child_rows = [
        _child_row("c1", "table-multi", 0.10, page=1,
                   chunk_metadata={"page_start": 1, "page_end": 3}),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    assert len(results) == 1
    r = results[0]
    assert r.page_number == 1
    assert r.page_number_end == 3


def test_single_page_window_has_no_page_number_end(monkeypatch):
    """Ordinary single-page table windows (no chunk_metadata, or
    page_start == page_end) must NOT get a page_number_end — regression guard
    for backward compatibility."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

    child_rows = [
        _child_row("c1", "table-single", 0.10, page=2, chunk_metadata=None),
        _child_row("c2", "table-single-2", 0.11, page=5,
                   chunk_metadata={"page_start": 5, "page_end": 5}),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    by_id = {r.chunk_id: r for r in results}
    assert by_id["c1"].page_number == 2
    assert by_id["c1"].page_number_end is None
    assert by_id["c2"].page_number == 5
    assert by_id["c2"].page_number_end is None


def test_chunk_metadata_as_json_string_is_handled(monkeypatch):
    """chunk_metadata may come back as a raw JSON string in some driver
    configurations — must be parsed defensively, not raise or silently drop."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

    child_rows = [
        _child_row("c1", "table-str-meta", 0.10, page=4,
                   chunk_metadata='{"page_start": 4, "page_end": 6}'),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    assert results[0].page_number == 4
    assert results[0].page_number_end == 6


def test_malformed_chunk_metadata_degrades_gracefully(monkeypatch):
    """A malformed chunk_metadata value must not raise — degrades to 'no page
    range info' (page_number_end=None) instead of crashing retrieval."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

    child_rows = [
        _child_row("c1", "table-bad-meta", 0.10, page=7, chunk_metadata="not-json"),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    assert results[0].page_number == 7
    assert results[0].page_number_end is None


def test_multi_window_cap_respected_on_continuation_merged_table(monkeypatch):
    """A continuation-merged table (many rows -> many windows, each carrying a
    page_start/page_end range) must still respect
    TABLE_MAX_WINDOWS_PER_QUERY_RESULT exactly as a normal table does — the cap
    operates generically per table_id regardless of how many source pages the
    underlying table_store row was merged from."""
    from app.config import settings
    monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TABLE_MAX_WINDOWS_PER_QUERY_RESULT", 2)

    # Simulate a 70-row, 3-page continuation-merged table chunked into many
    # windows, each stamped with its own page_start/page_end sub-range.
    child_rows = [
        _child_row("w1", "table-merged", 0.05, chunk_metadata={"page_start": 1, "page_end": 1}),
        _child_row("w2", "table-merged", 0.06, chunk_metadata={"page_start": 1, "page_end": 2}),
        _child_row("w3", "table-merged", 0.07, chunk_metadata={"page_start": 2, "page_end": 2}),
        _child_row("w4", "table-merged", 0.08, chunk_metadata={"page_start": 2, "page_end": 3}),
        _child_row("w5", "table-merged", 0.09, chunk_metadata={"page_start": 3, "page_end": 3}),
    ]
    conn, cur = _make_conn_with_child_rows(child_rows)

    results = rs._query_table_store(conn, _embedding(), None, None, 15)

    # Cap still applies: only the 2 closest windows survive, regardless of the
    # much larger candidate pool a continuation-merged table can produce.
    assert len(results) == 2
    assert {r.chunk_id for r in results} == {"w1", "w2"}
    # And each surviving window still carries its own correct page range.
    by_id = {r.chunk_id: r for r in results}
    assert by_id["w1"].page_number == 1 and by_id["w1"].page_number_end is None
    assert by_id["w2"].page_number == 1 and by_id["w2"].page_number_end == 2


# ── 5. Citation layer — page_number_end threading (query.py) ───────────────

def test_citation_exposes_page_number_end_for_continuation_merged_chunk():
    from app.api.routes.query import _citation_from_chunk
    from app.services.retriever_service import RetrievedChunk

    chunk = RetrievedChunk(
        chunk_id="c1", document_id="doc-1", text="row data",
        store_type="table", distance=0.1,
        document_filename="report.pdf", document_type="financial",
        page_number=1, page_number_end=3,
    )
    citation = _citation_from_chunk(chunk, bucket="documents")
    assert citation.page_number == 1
    assert citation.page_number_end == 3


def test_citation_page_number_end_none_for_ordinary_chunk_no_regression():
    """An ordinary (non-continuation) chunk must have page_number_end=None in
    its citation — confirms the new field is fully backward compatible."""
    from app.api.routes.query import _citation_from_chunk
    from app.services.retriever_service import RetrievedChunk

    chunk = RetrievedChunk(
        chunk_id="c2", document_id="doc-1", text="a normal paragraph",
        store_type="vector", distance=0.2,
        document_filename="policy.pdf", document_type="policy",
        page_number=4,
    )
    citation = _citation_from_chunk(chunk, bucket="documents")
    assert citation.page_number == 4
    assert citation.page_number_end is None


# ── 6. source_image_id lineage on continuation-merged tables ───────────────
#
# Per Phase 1's design, source_image_id lives on the table_store row itself
# (pointing at the FIRST fragment's crop image for a merged table) and is set
# once at storage time. The retriever never touches or derives it — it isn't
# even selected in the table SELECT lists above. This test simply documents
# that RetrievedChunk has no source_image_id field to keep in sync (nothing
# for the retriever to break here), matching the integration report's Task 4.

def test_retrieved_chunk_has_no_source_image_id_field_by_design():
    from app.services.retriever_service import RetrievedChunk
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(RetrievedChunk)}
    assert "source_image_id" not in field_names
