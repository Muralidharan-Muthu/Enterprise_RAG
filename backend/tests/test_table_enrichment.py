"""Unit tests for table_enrichment.enrich_table (Slice 3).

Pure: no DB, no network, no LLM calls. Exercises rules-based derivation and
VLM-meta precedence directly.
"""
from app.services.table_enrichment import enrich_table


# ── currency ─────────────────────────────────────────────────────────────────

def test_currency_symbol_dollar():
    out = enrich_table(["Revenue"], [["$100"]])
    assert out["currency"] == "USD"


def test_currency_symbol_euro():
    out = enrich_table(["Revenue"], [["€100"]])
    assert out["currency"] == "EUR"


def test_currency_symbol_pound():
    out = enrich_table(["Revenue"], [["£100"]])
    assert out["currency"] == "GBP"


def test_currency_symbol_rupee():
    out = enrich_table(["Revenue"], [["₹100"]])
    assert out["currency"] == "INR"


def test_currency_code_in_header():
    out = enrich_table(["Revenue (USD)"], [["100"]])
    assert out["currency"] == "USD"


def test_currency_code_eur_in_cell():
    out = enrich_table(["Revenue"], [["100 EUR"]])
    assert out["currency"] == "EUR"


def test_currency_none_when_absent():
    out = enrich_table(["Revenue"], [["100"]])
    assert out["currency"] is None


# ── fiscal_year ──────────────────────────────────────────────────────────────

def test_fiscal_year_fy_four_digit():
    out = enrich_table(["Metric"], [["1"]], caption="Revenue FY2024")
    assert out["fiscal_year"] == "FY2024"


def test_fiscal_year_fy_two_digit():
    out = enrich_table(["Metric"], [["1"]], caption="Revenue FY24")
    assert out["fiscal_year"] == "FY2024"


def test_fiscal_year_fy_no_space():
    out = enrich_table(["Metric"], [["1"]], caption="Summary(FY2023)")
    assert out["fiscal_year"] == "FY2023"


def test_fiscal_year_bare_year_near_fiscal_word():
    out = enrich_table(["Metric"], [["1"]], caption="Fiscal year 2022 summary")
    assert out["fiscal_year"] == "FY2022"


def test_fiscal_year_none_when_absent():
    out = enrich_table(["Metric"], [["1"]], caption="Summary table")
    assert out["fiscal_year"] is None


# ── reporting_period ─────────────────────────────────────────────────────────

def test_reporting_period_quarter_with_year():
    out = enrich_table(["Metric"], [["1"]], caption="Results Q3 2024")
    assert out["reporting_period"] == "Q3 2024"


def test_reporting_period_quarter_no_year():
    out = enrich_table(["Metric"], [["1"]], caption="Results Q1")
    assert out["reporting_period"] == "Q1"


def test_reporting_period_half():
    out = enrich_table(["Metric"], [["1"]], caption="Results H1 2024")
    assert out["reporting_period"] == "H1 2024"


def test_reporting_period_month():
    out = enrich_table(["Metric"], [["1"]], caption="Sales for March")
    assert out["reporting_period"] == "March"


def test_reporting_period_ytd():
    out = enrich_table(["Metric"], [["1"]], caption="YTD Performance")
    assert out["reporting_period"] == "YTD"


def test_reporting_period_annual():
    out = enrich_table(["Metric"], [["1"]], caption="Annual Report Summary")
    assert out["reporting_period"] == "annual"


def test_reporting_period_quarter_word():
    out = enrich_table(["Metric"], [["1"]], caption="Quarterly performance overview")
    assert out["reporting_period"] == "quarterly"


def test_reporting_period_none_when_absent():
    out = enrich_table(["Metric"], [["1"]], caption="Overview")
    assert out["reporting_period"] is None


# ── table_category ───────────────────────────────────────────────────────────

def test_category_balance_sheet():
    out = enrich_table(["Assets", "Liabilities"], [["100", "50"]], caption="Balance Sheet")
    assert out["table_category"] == "balance_sheet"


def test_category_income_statement():
    out = enrich_table(["Revenue", "Expenses"], [["100", "50"]], caption="Income Statement")
    assert out["table_category"] == "income_statement"


def test_category_cash_flow():
    out = enrich_table(["Metric"], [["1"]], caption="Cash Flow Statement")
    assert out["table_category"] == "cash_flow"


def test_category_kpi():
    out = enrich_table(["Margin", "Ratio"], [["10%", "1.2"]], caption="Key Performance Indicators")
    assert out["table_category"] == "kpi"


def test_category_comparison():
    out = enrich_table(["Budget", "Actual", "Variance"], [["100", "90", "-10"]], caption="Budget vs Actual")
    assert out["table_category"] == "comparison"


def test_category_other_when_no_keywords_match():
    out = enrich_table(["Name", "Value"], [["a", "1"]], caption="Miscellaneous data")
    assert out["table_category"] == "other"


# ── detected_units ───────────────────────────────────────────────────────────

def test_units_percent():
    out = enrich_table(["Growth"], [["10%"]])
    assert out["detected_units"] == ["%"]


def test_units_millions():
    out = enrich_table(["Revenue (USD millions)"], [["100"]])
    assert out["detected_units"] is not None
    assert "usd millions" in out["detected_units"]


def test_units_per_share():
    out = enrich_table(["EPS"], [["1.23 per share"]])
    assert out["detected_units"] == ["per share"]


def test_units_none_when_absent():
    out = enrich_table(["Name"], [["a"]])
    assert out["detected_units"] is None


# ── table_summary ─────────────────────────────────────────────────────────────

def test_table_summary_always_nonempty_with_data():
    out = enrich_table(["A", "B"], [["1", "2"]], caption="My Table")
    assert out["table_summary"]
    assert "My Table" in out["table_summary"]
    assert "1 rows" in out["table_summary"]
    assert "2 columns" in out["table_summary"]
    assert "A" in out["table_summary"] and "B" in out["table_summary"]


def test_table_summary_always_nonempty_with_no_caption():
    out = enrich_table(["A"], [["1"]], caption=None)
    assert out["table_summary"]
    assert out["table_summary"].startswith("Table —") or out["table_summary"].startswith("Table")


def test_table_summary_nonempty_when_headers_and_rows_empty():
    out = enrich_table([], [], caption=None)
    assert out["table_summary"]
    assert "0 rows" in out["table_summary"]
    assert "0 columns" in out["table_summary"]


def test_table_summary_truncates_columns_preview_to_12():
    headers = [f"Col{i}" for i in range(20)]
    out = enrich_table(headers, [["x"] * 20], caption="Wide table")
    # Only the first 12 headers should be listed in the summary preview
    assert "Col11" in out["table_summary"]
    assert "Col12" not in out["table_summary"]


# ── vlm_meta precedence ──────────────────────────────────────────────────────

def test_vlm_meta_currency_takes_precedence_over_rules():
    # Cell says "$100" (would rule-derive USD) but VLM says EUR
    out = enrich_table(["Revenue"], [["$100"]], vlm_meta={"currency": "EUR"})
    assert out["currency"] == "EUR"


def test_vlm_meta_fiscal_year_takes_precedence():
    out = enrich_table(["Metric"], [["1"]], caption="FY2020", vlm_meta={"fiscal_year": "FY2099"})
    assert out["fiscal_year"] == "FY2099"


def test_vlm_meta_reporting_period_takes_precedence():
    out = enrich_table(["Metric"], [["1"]], caption="Q1 2024", vlm_meta={"reporting_period": "H2 2025"})
    assert out["reporting_period"] == "H2 2025"


def test_vlm_meta_table_category_takes_precedence():
    out = enrich_table(["Assets"], [["1"]], caption="Balance Sheet", vlm_meta={"table_category": "kpi"})
    assert out["table_category"] == "kpi"


def test_vlm_meta_invalid_table_category_falls_back_to_rules():
    out = enrich_table(["Assets"], [["1"]], caption="Balance Sheet", vlm_meta={"table_category": "not_a_real_category"})
    assert out["table_category"] == "balance_sheet"


def test_vlm_meta_units_string_wrapped_in_list():
    out = enrich_table(["Revenue"], [["100"]], vlm_meta={"units": "USD millions"})
    assert out["detected_units"] == ["USD millions"]


def test_vlm_meta_units_list_passed_through():
    out = enrich_table(["Revenue"], [["100"]], vlm_meta={"units": ["USD millions", "%"]})
    assert out["detected_units"] == ["USD millions", "%"]


def test_vlm_meta_empty_values_fall_back_to_rules():
    # Empty-string / falsy VLM values should not suppress the rules fallback.
    out = enrich_table(["Revenue"], [["$100"]], vlm_meta={"currency": "", "fiscal_year": None})
    assert out["currency"] == "USD"


def test_vlm_meta_none_is_safe():
    out = enrich_table(["Revenue"], [["$100"]], vlm_meta=None)
    assert out["currency"] == "USD"


# ── robustness: empty headers/rows ───────────────────────────────────────────

def test_empty_headers_and_rows_returns_safe_defaults():
    out = enrich_table([], [], caption=None)
    assert out["fiscal_year"] is None
    assert out["reporting_period"] is None
    assert out["currency"] is None
    assert out["table_category"] == "other"
    assert out["detected_units"] is None
    assert out["table_summary"]


def test_none_headers_and_rows_are_treated_as_empty():
    out = enrich_table(None, None, caption="Untitled")
    assert out["table_summary"]
    assert out["table_category"] == "other"


def test_rows_with_none_cells_are_safe():
    out = enrich_table(["A", "B"], [[None, "100"]], caption="T")
    assert out["table_summary"]


def test_returns_all_required_keys():
    out = enrich_table(["A"], [["1"]], caption="T")
    expected_keys = {
        "fiscal_year", "reporting_period", "currency",
        "table_category", "detected_units", "table_summary",
    }
    assert set(out.keys()) == expected_keys
