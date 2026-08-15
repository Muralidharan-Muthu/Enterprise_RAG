"""Phase 2 — exact structured table query engine.

table_query_engine.try_structured_query() is additive alongside the existing
semantic retrieval pipeline: it returns None when a query isn't recognized as
an exact aggregate/lookup request (falling through to semantic search
unchanged), and a structured result dict when it IS recognized.

These tests patch `_fetch_candidate_tables` to return synthetic
table_store-shaped data (headers/rows exactly like the real
json_data = {"headers": [...], "rows": [[...]]} column), so no live DB is
needed — same spirit as test_table_hybrid_retrieval.py's mocked conn/cursor
approach, just one level higher (mocking the fetch step directly since
try_structured_query owns its own DB access, mirrored from
retriever_service's filter pattern rather than importing it).
"""
from unittest.mock import patch, MagicMock

import pytest

from app.services import table_query_engine as tqe


# ── Synthetic table_store-shaped fixtures ───────────────────────────────────

def _sales_table():
    return {
        "table_id": "table-1",
        "document_id": "doc-1",
        "headers": ["Month", "Region", "Actual USD", "Budget USD"],
        "rows": [
            ["Month-01", "Marketing", "$12,842", "10000"],
            ["Month-02", "Marketing", "$9,300", "10000"],
            ["Month-03", "Marketing", "(542)", "10000"],
            ["Month-04", "Sales", "15000", "12000"],
            ["Month-05", "Marketing", "20000", "15000"],
        ],
        "table_title": "Regional Actuals",
        "table_category": "kpi",
        "currency": "USD",
        "fiscal_year": "FY2024",
        "filename": "budget.pdf",
    }


def _patch_tables(tables):
    return patch.object(tqe, "_fetch_candidate_tables", return_value=tables)


# ── (a) SUM ──────────────────────────────────────────────────────────────

def test_sum_query_returns_correct_total():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query("What is the total Actual USD?", None, None)

    assert result is not None
    assert result["operation"] == "SUM"
    assert result["column"] == "Actual USD"
    # 12842 + 9300 - 542 + 15000 + 20000 = 56600
    assert result["value"] == pytest.approx(56600)
    assert result["matched_table_ids"] == ["table-1"]
    assert result["row_count_considered"] == 5
    assert "filter_description" in result


# ── (b) AVG / COUNT / MIN / MAX ─────────────────────────────────────────────

def test_avg_query_returns_correct_average():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query("What is the average Actual USD?", None, None)

    assert result["operation"] == "AVG"
    assert result["value"] == pytest.approx(56600 / 5)


def test_count_query_returns_row_count():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query("How many rows are in the Month table?", None, None)

    assert result["operation"] == "COUNT"
    assert result["value"] == 5


def test_min_query_returns_lowest_value():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query("What is the lowest Actual USD?", None, None)

    assert result["operation"] == "MIN"
    assert result["value"] == pytest.approx(-542)


def test_max_query_returns_highest_value():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query("What is the highest Actual USD?", None, None)

    assert result["operation"] == "MAX"
    assert result["value"] == pytest.approx(20000)


# ── (c) exact row filtering + column lookup ─────────────────────────────────

def test_row_lookup_returns_correct_single_value():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query(
            "What is the Actual USD value for Month-05 in Marketing?", None, None
        )

    assert result is not None
    assert result["operation"] == "LOOKUP"
    assert result["column"] == "Actual USD"
    assert result["value"] == pytest.approx(20000)
    assert result["matched_table_ids"] == ["table-1"]


def test_row_lookup_matches_row_identifier_only():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query(
            "What is the Budget USD for Month-04?", None, None
        )

    assert result is not None
    assert result["value"] == pytest.approx(12000)


# ── (d) no aggregate/lookup intent -> None (falls through to semantic path) ─

def test_non_structured_query_returns_none():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query(
            "Summarize the risks mentioned in this contract.", None, None
        )
    assert result is None


def test_empty_query_returns_none():
    with _patch_tables([_sales_table()]):
        assert tqe.try_structured_query("", None, None) is None
        assert tqe.try_structured_query("   ", None, None) is None


# ── (e) unknown column name -> None gracefully (no crash) ──────────────────

def test_unknown_column_returns_none_without_crash():
    table = _sales_table()
    with _patch_tables([table]):
        result = tqe.try_structured_query(
            "What is the total Zorbnaxian Quotient?", None, None
        )
    assert result is None


def test_no_candidate_tables_returns_none():
    with _patch_tables([]):
        result = tqe.try_structured_query("What is the total Actual USD?", None, None)
    assert result is None


def test_fetch_failure_returns_none_gracefully():
    with patch.object(tqe, "_fetch_candidate_tables", side_effect=RuntimeError("db down")):
        result = tqe.try_structured_query("What is the total Actual USD?", None, None)
    assert result is None


# ── (f) currency-formatted numbers parse correctly ──────────────────────────

def test_currency_and_parenthesized_negative_parse_correctly():
    assert tqe._parse_cell_numeric("$12,842") == pytest.approx(12842)
    assert tqe._parse_cell_numeric("(542)") == pytest.approx(-542)
    assert tqe._parse_cell_numeric("12.5%") == pytest.approx(12.5)
    assert tqe._parse_cell_numeric("not a number") is None
    assert tqe._parse_cell_numeric(None) is None


def test_sum_excludes_unparseable_cells_and_counts_them():
    table = {
        "table_id": "t2",
        "document_id": "doc-2",
        "headers": ["Item", "Amount"],
        "rows": [
            ["A", "100"],
            ["B", "N/A"],
            ["C", "200"],
        ],
        "table_title": "Mixed",
        "filename": "mixed.pdf",
    }
    with _patch_tables([table]):
        result = tqe.try_structured_query("What is the sum of Amount?", None, None)

    assert result["value"] == pytest.approx(300)
    assert result["unparseable_count"] == 1
    assert result["row_count_considered"] == 3


# ── Fuzzy column matching ────────────────────────────────────────────────────

def test_fuzzy_find_column_case_and_punctuation_insensitive():
    headers = ["Actual USD ($)", "Budget %"]
    assert tqe._fuzzy_find_column("actual usd", headers) == 0
    assert tqe._fuzzy_find_column("BUDGET", headers) == 1
    assert tqe._fuzzy_find_column("nonexistent thing", headers) is None


# ── Document filters mirror retriever_service's table_filters pattern ──────

def test_fetch_candidate_tables_applies_document_id_and_types_filters():
    """_fetch_candidate_tables builds SQL filtered by document_id/document_types,
    mirroring retriever_service's _doc_filter/_type_filter pattern (parameterized,
    never string-interpolated)."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    with patch("app.db.connection.get_db", return_value=conn):
        tqe._fetch_candidate_tables(document_id="doc-123", document_types=["financial"])

    sql, params = cur.execute.call_args[0]
    assert "ts.document_id = %s" in sql
    assert "dr.document_type IN (%s)" in sql
    assert "doc-123" in params
    assert "financial" in params


# ── Integration: STRUCTURED_QUERY_ENABLED gating in the query route ─────────

def test_structured_query_disabled_short_circuits_route_helper():
    """When STRUCTURED_QUERY_ENABLED=False, the route's _try_structured_query
    helper must not even call table_query_engine.try_structured_query."""
    from app.config import settings
    from app.api.routes.query import _try_structured_query, QueryRequest

    original = settings.STRUCTURED_QUERY_ENABLED
    settings.STRUCTURED_QUERY_ENABLED = False
    try:
        req = QueryRequest(query="What is the total Actual USD?")
        with patch("app.services.table_query_engine.try_structured_query") as mock_fn:
            result = _try_structured_query(req)
        mock_fn.assert_not_called()
        assert result is None
    finally:
        settings.STRUCTURED_QUERY_ENABLED = original


def test_structured_query_enabled_calls_engine():
    from app.config import settings
    from app.api.routes.query import _try_structured_query, QueryRequest

    original = settings.STRUCTURED_QUERY_ENABLED
    settings.STRUCTURED_QUERY_ENABLED = True
    try:
        req = QueryRequest(query="What is the total Actual USD?", document_id="doc-1")
        with patch(
            "app.services.table_query_engine.try_structured_query",
            return_value={"operation": "SUM", "column": "Actual USD", "value": 1.0,
                          "matched_table_ids": ["t"], "row_count_considered": 1,
                          "filter_description": "x"},
        ) as mock_fn:
            result = _try_structured_query(req)
        mock_fn.assert_called_once()
        assert result is not None
        assert result["operation"] == "SUM"
    finally:
        settings.STRUCTURED_QUERY_ENABLED = original
