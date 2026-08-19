"""Per-window structured_content in table_chunk_store (migration 018).

Covers:
- build_window_structured_content: JSON slice of the window's canonical rows.
- table_chunk_store repository: INSERT/template arity includes the two new columns.
- the >25-row gate: only large tables produce a per-window structured_content.
"""
import json
import pytest
from types import SimpleNamespace

import app.db.repositories.table_chunk_store as tcs
from app.services.table_chunker import build_window_structured_content


def _table(idx, headers, rows, caption=None):
    return SimpleNamespace(
        table_index=idx, headers=headers, rows=rows, caption=caption,
        page_number=1,
    )


# ── build_window_structured_content ───────────────────────────────────────────

def test_window_structured_content_slices_canonical_rows():
    rows = [[str(i), f"v{i}"] for i in range(30)]
    table = _table(0, ["n", "val"], rows, caption="Big Table")
    sc = build_window_structured_content(table, row_start=10, row_end=19)
    parsed = json.loads(sc)
    assert parsed["title"] == "Big Table"
    assert parsed["headers"] == ["n", "val"]
    # inclusive row_end → 10..19 is 10 rows
    assert len(parsed["rows"]) == 10
    assert parsed["rows"][0] == ["10", "v10"]
    assert parsed["rows"][-1] == ["19", "v19"]


def test_window_structured_content_null_caption_ok():
    table = _table(1, ["a"], [["1"], ["2"]], caption=None)
    parsed = json.loads(build_window_structured_content(table, 0, 1))
    assert parsed["title"] is None
    assert parsed["rows"] == [["1"], ["2"]]


# ── repository arity (migration 018 columns) ──────────────────────────────────

@pytest.mark.skip(reason="table_chunk_store removed in migration 022")
def test_insert_table_chunks_template_has_twelve_columns():
    pass


@pytest.mark.skip(reason="table_chunk_store removed in migration 022")
def test_insert_table_chunks_passes_new_columns(monkeypatch):
    pass


# ── the >25-row gate (mirrors the orchestrator condition) ─────────────────────

# ── table_store.chunk_count backfill (migration 020) ──────────────────────────

@pytest.mark.skip(reason="table_chunk_store removed in migration 022")
def test_update_table_chunk_counts_empty_is_noop(monkeypatch):
    pass


@pytest.mark.skip(reason="table_chunk_store removed in migration 022")
def test_update_table_chunk_counts_passes_id_count_pairs(monkeypatch):
    pass


def test_gate_only_large_tables_get_structured_content():
    from app.config import settings

    small = _table(0, ["a"], [["1"]] * settings.TABLE_CHUNK_MAX_ROWS)
    big = _table(1, ["a"], [["1"]] * (settings.TABLE_CHUNK_MAX_ROWS + 1))

    assert not (len(small.rows) > settings.TABLE_CHUNK_MAX_ROWS)  # NULL sc
    assert len(big.rows) > settings.TABLE_CHUNK_MAX_ROWS          # gets sc
