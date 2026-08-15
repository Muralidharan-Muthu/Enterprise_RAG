"""Unit tests for the content-class-driven image routing decision.

Pure logic — no VLM/DB. Covers classify_content (the explicit content class),
route_for_class (class + hint + confidence -> store), and decide_route end to end.
"""
import json

from app.services.image_router import (
    classify_content, validate_content, route_for_class, decide_route,
    is_searchable, content_type_for, RoutingDecision, ContentValidation,
    SEARCHABLE_MIN_CHARS, MIN_CONFIDENCE_FOR_SPECIALIZED,
    QUALITY_HIGH, QUALITY_MEDIUM, QUALITY_LOW,
    CLASS_STRUCTURED_TABLE, CLASS_STRUCTURED_CHART, CLASS_LEGAL,
    CLASS_DOCUMENT_TEXT, CLASS_MIXED, CLASS_DECORATIVE, CLASS_UNKNOWN,
)

_PROSE = "quarterly revenue grew across every region and product line this year"
_TINY = "n/a"
_TABLE_JSON = json.dumps({"headers": ["Q", "Rev"], "rows": [["Q1", "100"], ["Q2", "200"]]})
_CHART_JSON = json.dumps({"title": "Spend", "categories": ["A", "B"], "percentages": [60, 40]})


# ── helpers ──────────────────────────────────────────────────────────────────

def _cls(canonical, sc="", ocr="", vlm=True):
    return classify_content(canonical_store=canonical, structured_content=sc,
                            ocr_text=ocr, vlm_succeeded=vlm)


# ── is_searchable / content_type_for ─────────────────────────────────────────

def test_is_searchable_threshold():
    assert is_searchable("x" * SEARCHABLE_MIN_CHARS) is True
    assert is_searchable("x" * (SEARCHABLE_MIN_CHARS - 1)) is False
    assert is_searchable("") is False and is_searchable(None) is False


def test_content_type_for():
    assert content_type_for("table_store") == "table"
    assert content_type_for("image_store") == "figure"
    assert content_type_for("vector_store") == "text"


# ── classify_content ─────────────────────────────────────────────────────────

def test_classify_json_rows_is_structured_table():
    assert _cls("image_store", sc=_TABLE_JSON) == CLASS_STRUCTURED_TABLE      # store hint ignored
    assert _cls("table_store", sc=_TABLE_JSON) == CLASS_STRUCTURED_TABLE


def test_classify_json_chart_keys_is_structured_chart():
    assert _cls("image_store", sc=_CHART_JSON) == CLASS_STRUCTURED_CHART


def test_classify_clause_hint_is_legal():
    assert _cls("clause_store", sc=_PROSE) == CLASS_LEGAL


def test_classify_document_hint_is_document_text():
    assert _cls("document_store", sc=_PROSE) == CLASS_DOCUMENT_TEXT


def test_classify_vector_hint_is_document_text():
    assert _cls("vector_store", sc=_PROSE) == CLASS_DOCUMENT_TEXT


def test_classify_table_hint_without_rows_is_mixed():
    assert _cls("table_store", sc=_PROSE) == CLASS_MIXED


def test_classify_image_hint_with_prose_is_unknown():
    # VLM said image_store but there IS searchable prose -> unknown (never decorative)
    assert _cls("image_store", sc=_PROSE) == CLASS_UNKNOWN


def test_classify_no_content_is_decorative():
    assert _cls("image_store", sc=_TINY, ocr=_TINY) == CLASS_DECORATIVE
    assert _cls("table_store", sc="", ocr="") == CLASS_DECORATIVE


def test_classify_failed_vlm_ignores_store_hint():
    # failed VLM: store hint not trusted; searchable OCR -> unknown, empty -> decorative
    assert _cls("table_store", sc="", ocr=_PROSE, vlm=False) == CLASS_UNKNOWN
    assert _cls("clause_store", sc="", ocr="", vlm=False) == CLASS_DECORATIVE


# ── route_for_class ──────────────────────────────────────────────────────────

def test_route_structured_to_table():
    assert route_for_class(CLASS_STRUCTURED_TABLE, "image_store", 0.9) == "table_store"
    assert route_for_class(CLASS_STRUCTURED_CHART, "image_store", 0.9) == "table_store"


def test_route_legal_to_clause():
    assert route_for_class(CLASS_LEGAL, "clause_store", 0.9) == "clause_store"


def test_route_document_text_research_vs_general():
    assert route_for_class(CLASS_DOCUMENT_TEXT, "document_store", 0.9) == "document_store"
    assert route_for_class(CLASS_DOCUMENT_TEXT, "vector_store", 0.9) == "vector_store"


def test_route_decorative_to_image():
    assert route_for_class(CLASS_DECORATIVE, "image_store", 0.9) == "image_store"


def test_route_mixed_and_unknown_to_vector():
    assert route_for_class(CLASS_MIXED, "table_store", 0.9) == "vector_store"
    assert route_for_class(CLASS_UNKNOWN, "image_store", 0.9) == "vector_store"


def test_low_confidence_specialized_degrades_to_vector():
    lo = MIN_CONFIDENCE_FOR_SPECIALIZED - 0.01
    assert route_for_class(CLASS_STRUCTURED_TABLE, "image_store", lo) == "vector_store"
    assert route_for_class(CLASS_LEGAL, "clause_store", lo) == "vector_store"


# ── decide_route (end to end) ────────────────────────────────────────────────

def _route(canonical, sc="", ocr="", conf=0.9, vlm=True):
    return decide_route(canonical_store=canonical, structured_content=sc, ocr_text=ocr,
                        confidence=conf, vlm_succeeded=vlm, base_reason="r")


def test_decide_returns_decision_with_class():
    d = _route("table_store", sc=_TABLE_JSON)
    assert isinstance(d, RoutingDecision)
    assert d.destination_store == "table_store" and d.content_class == CLASS_STRUCTURED_TABLE
    assert d.content_type == "table" and d.confidence == 0.9 and "class=" in d.reason


def test_decide_pie_chart_style_prose_from_image_store_goes_vector():
    # The real-PDF pie-chart case: VLM said image_store, emitted descriptive prose.
    d = _route("image_store", sc=_PROSE)
    assert d.destination_store == "vector_store" and d.content_class == CLASS_UNKNOWN


def test_decide_decorative_stays_image():
    d = _route("image_store", sc="", ocr="")
    assert d.destination_store == "image_store" and d.content_class == CLASS_DECORATIVE


def test_decide_low_confidence_table_degrades():
    d = _route("table_store", sc=_TABLE_JSON, conf=0.1)
    assert d.destination_store == "vector_store"      # still searchable, not a shaky table


# ── validate_content ─────────────────────────────────────────────────────────

def _valtable(rows, headers=None):
    obj = {"rows": rows}
    if headers is not None:
        obj["headers"] = headers
    return validate_content(CLASS_STRUCTURED_TABLE, json.dumps(obj))


def test_validate_good_table_is_high_quality():
    v = _valtable([["Q1", "100"], ["Q2", "200"]], headers=["Q", "Rev"])
    assert isinstance(v, ContentValidation)
    assert v.is_valid and v.quality == QUALITY_HIGH and v.issues == ()


def test_validate_single_row_is_valid_medium():
    v = _valtable([["Q1", "100"]], headers=["Q", "Rev"])
    assert v.is_valid and v.quality == QUALITY_MEDIUM


def test_validate_inconsistent_columns_is_invalid():
    v = _valtable([["Q1", "100"], ["Q2"]])          # ragged
    assert not v.is_valid and v.quality == QUALITY_LOW
    assert "inconsistent column counts" in v.issues


def test_validate_no_rows_is_invalid():
    v = _valtable([], headers=["Q", "Rev"])
    assert not v.is_valid and "no data rows" in v.issues


def test_validate_duplicate_headers_flagged():
    v = _valtable([["1", "2"], ["3", "4"]], headers=["Q", "Q"])
    assert "duplicate headers" in v.issues and v.quality != QUALITY_HIGH


def test_validate_row_width_mismatch_flagged():
    v = _valtable([["1", "2", "3"], ["4", "5", "6"]], headers=["A", "B"])
    assert "row width != header count" in v.issues


def test_validate_decorative_invalid_low():
    v = validate_content(CLASS_DECORATIVE, "")
    assert not v.is_valid and v.quality == QUALITY_LOW


def test_validate_prose_quality_by_length():
    assert validate_content(CLASS_DOCUMENT_TEXT, "x" * 250).quality == QUALITY_HIGH
    assert validate_content(CLASS_DOCUMENT_TEXT, "x" * 80).quality == QUALITY_MEDIUM
    assert validate_content(CLASS_DOCUMENT_TEXT, "x" * 20).quality == QUALITY_LOW
    assert validate_content(CLASS_DOCUMENT_TEXT, "x" * 20).is_valid is True


# ── decide_route: validation demotes malformed structured output ─────────────

def test_malformed_table_demoted_to_vector():
    bad = json.dumps({"headers": ["A", "B"], "rows": [["1", "2"], ["3"]]})   # ragged
    d = _route("table_store", sc=bad)
    assert d.destination_store == "vector_store"       # not polluting table_store
    assert d.content_class == CLASS_STRUCTURED_TABLE    # classification unchanged
    assert d.extraction_quality == QUALITY_LOW
    assert "demoted" in d.reason


def test_valid_table_routes_and_reports_quality():
    d = _route("table_store", sc=_TABLE_JSON)          # 2 rows + headers
    assert d.destination_store == "table_store"
    assert d.extraction_quality == QUALITY_HIGH
    assert "demoted" not in d.reason
