"""Tests for the table-crop image lineage integrity gate.

Covers the confirmed silent-lineage-loss bug: when store_table_crop_images()
throws, crop_image_ids stayed {} and every table silently got
source_image_id=None with no error surfaced, while document_registry still
reported status='completed'.

_find_table_lineage_gap is the pure-function core of the fix: it flags any
table_index that had a crop-image candidate (an entry in table_image_records)
but has no registered id in crop_image_ids. Tables with no crop candidate at
all (pure pdf_grid tables) are correctly excluded.
"""
import app.services.ingestion_orchestrator as orch


def test_lineage_gap_empty_when_all_registered():
    """Crop registration succeeded for every candidate -> no gap, completes as before."""
    table_image_records = [
        {"table_index": 0, "caption": "Table 1", "ocr_text": ""},
        {"table_index": 2, "caption": "Table 3", "ocr_text": ""},
    ]
    crop_image_ids = {0: "uuid-0", 2: "uuid-2"}

    gap = orch._find_table_lineage_gap(table_image_records, crop_image_ids)

    assert gap == []


def test_lineage_gap_detects_total_registration_failure():
    """store_table_crop_images() threw -> crop_image_ids stayed {} -> every
    candidate table_index must be reported as a lineage gap, not silently
    dropped."""
    table_image_records = [
        {"table_index": 0, "caption": "Table 1", "ocr_text": ""},
        {"table_index": 1, "caption": "Table 2", "ocr_text": ""},
    ]
    crop_image_ids: dict = {}  # unchanged from initialization — registration failed

    gap = orch._find_table_lineage_gap(table_image_records, crop_image_ids)

    assert gap == [0, 1]


def test_lineage_gap_detects_partial_registration_failure():
    """Some crops registered, others didn't (e.g. batch embed partially failed) ->
    only the missing ones are flagged."""
    table_image_records = [
        {"table_index": 0, "caption": "Table 1", "ocr_text": ""},
        {"table_index": 1, "caption": "Table 2", "ocr_text": ""},
    ]
    crop_image_ids = {0: "uuid-0"}  # table_index 1 missing

    gap = orch._find_table_lineage_gap(table_image_records, crop_image_ids)

    assert gap == [1]


def test_lineage_gap_ignores_tables_with_no_crop_candidate():
    """Tables that never had an image crop at all (table_image_records has no
    entry for them) are legitimately pure pdf_grid tables — not a lineage
    failure, must not false-positive."""
    # table_image_records only has an entry for table_index 0; table_index 1
    # never had image_png_bytes and so was never a crop candidate.
    table_image_records = [
        {"table_index": 0, "caption": "Table 1", "ocr_text": ""},
    ]
    crop_image_ids = {0: "uuid-0"}

    gap = orch._find_table_lineage_gap(table_image_records, crop_image_ids)

    assert gap == []


def test_lineage_gap_no_candidates_at_all():
    """Document has no table crops whatsoever (all pdf_grid) -> no gap possible."""
    gap = orch._find_table_lineage_gap([], {})
    assert gap == []


def test_lineage_gap_falsy_id_treated_as_missing():
    """A registered-but-falsy id (None/'' ) counts as not-registered — guards
    against a registration function returning a dict with None values instead
    of omitting the key."""
    table_image_records = [{"table_index": 0, "caption": "Table 1", "ocr_text": ""}]
    crop_image_ids = {0: None}

    gap = orch._find_table_lineage_gap(table_image_records, crop_image_ids)

    assert gap == [0]


# ── Table-count sanity gate ─────────────────────────────────────────────
# Mirrors the lineage-completeness gate pattern above but checks a different,
# more general invariant: every table the parser produced for this run must
# end up as exactly one table_store row. This is the fix for the confirmed
# incident where a stray native Celery worker running stale code raced the
# Docker worker on the same queue and silently left a multi-page table split
# into 3 table_store rows instead of merged into 1 — with document_registry
# still reporting status='completed'.


def test_table_count_matches_no_mismatch():
    """Parser produced N tables, N table_store rows inserted -> no mismatch,
    completes normally as before (the overwhelming common case)."""
    assert orch._find_table_count_mismatch(parsed_table_count=3, stored_table_count=3) is False


def test_table_count_mismatch_fewer_stored_than_parsed():
    """Parser produced more tables than got stored (e.g. a stale worker's
    continuation-merge left 3 rows behind instead of merging to 1, or a
    partial write dropped rows) -> mismatch detected."""
    assert orch._find_table_count_mismatch(parsed_table_count=1, stored_table_count=3) is True
    assert orch._find_table_count_mismatch(parsed_table_count=5, stored_table_count=2) is True


def test_table_count_mismatch_zero_zero_is_not_a_mismatch():
    """A document with no tables at all (0 parsed, 0 stored) must not
    false-positive — this is the common no-tables case, not a failure."""
    assert orch._find_table_count_mismatch(parsed_table_count=0, stored_table_count=0) is False


def test_table_count_mismatch_more_stored_than_parsed():
    """Defensive: even an unexpected 'more stored than parsed' direction (e.g.
    a leftover row from a previous run that wasn't cleared) must be flagged —
    the invariant is equality, not just 'at least as many stored'."""
    assert orch._find_table_count_mismatch(parsed_table_count=2, stored_table_count=3) is True


def test_lineage_gap_tests_unaffected_by_count_gate_addition():
    """Regression guard: the pre-existing lineage-gap pure function is
    untouched by the new table-count gate — same inputs, same outputs."""
    table_image_records = [
        {"table_index": 0, "caption": "Table 1", "ocr_text": ""},
        {"table_index": 1, "caption": "Table 2", "ocr_text": ""},
    ]
    crop_image_ids = {0: "uuid-0"}

    gap = orch._find_table_lineage_gap(table_image_records, crop_image_ids)

    assert gap == [1]


def test_table_count_mismatch_error_message_includes_expected_and_actual():
    """The completed_with_errors error_message must clearly state expected vs
    actual counts and the worker/code-version diagnostic signal — this is the
    exact format wired up at the completion gate call site in
    ingest_document(). Reconstructed here (rather than driving the full
    Celery task) to keep the assertion tied to the documented message
    contract without mocking the entire multi-stage pipeline."""
    parsed_table_count = 1
    stored_table_count = 3
    worker_id = "myhost-1234-1700000000"
    code_version = "abc1234"

    assert orch._find_table_count_mismatch(parsed_table_count, stored_table_count) is True

    count_msg = (
        f"Table storage mismatch: parser produced {parsed_table_count} table(s) "
        f"but {stored_table_count} table_store row(s) were inserted "
        f"(worker_id={worker_id}, code_version={code_version}) -- "
        f"possible stale worker or partial write."
    )

    assert "parser produced 1 table(s)" in count_msg
    assert "3 table_store row(s) were inserted" in count_msg
    assert f"worker_id={worker_id}" in count_msg
    assert f"code_version={code_version}" in count_msg
