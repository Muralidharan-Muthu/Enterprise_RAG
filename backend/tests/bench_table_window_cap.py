"""Lightweight before/after benchmark for the table-store multi-window fix.

Not a pytest test (no assertions gate CI) -- run directly:

    cd backend
    .venv\\Scripts\\python.exe tests\\bench_table_window_cap.py

Simulates the dedup step of _query_table_store() with a mocked cursor
(no live DB needed) for a synthetic result set: one wide table with 3
close-distance child windows plus a handful of other tables with a single
relevant window each. Times N repeated calls with TABLE_MAX_WINDOWS_PER_QUERY_RESULT
forced to 1 ("before"/old hard dedup) vs the shipped default of 2 ("after"),
and reports result counts + wall time for each.
"""
import time
from unittest.mock import MagicMock

import numpy as np

import app.services.retriever_service as rs
from app.config import settings


def _child_row(chunk_id, table_id, distance, page=1):
    return (chunk_id, "doc-1", f"text-{chunk_id}", page, "md", distance,
            "file.pdf", "financial", "path", "bucket", None, table_id)


def _make_conn(child_rows):
    cur = MagicMock()
    cur.fetchall.side_effect = lambda: child_rows if not cur.fetchall.call_count % 2 else []
    # Simpler: alternate deterministic side_effect list, refreshed per call below.
    conn = MagicMock()
    return conn


def _run_once(cap: int, n_iters: int = 200):
    # Synthetic result set: table-WIDE has 3 genuinely close windows;
    # 5 other tables have 1 relevant window each (common case).
    child_rows = [
        _child_row("wide-1", "table-WIDE", 0.10),
        _child_row("wide-2", "table-WIDE", 0.11),
        _child_row("wide-3", "table-WIDE", 0.13),
    ]
    for i in range(5):
        child_rows.append(_child_row(f"other-{i}", f"table-{i}", 0.20 + i * 0.01))

    settings.TABLE_MAX_WINDOWS_PER_QUERY_RESULT = cap
    embedding = np.zeros(8, dtype=np.float32)

    total_result_counts = []
    start = time.perf_counter()
    for _ in range(n_iters):
        cur = MagicMock()
        cur.fetchall.side_effect = [child_rows, []]
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cur

        results = rs._query_table_store(conn, embedding, None, None, top_k=15)
        total_result_counts.append(len(results))
    elapsed = time.perf_counter() - start

    wide_table_windows = None
    # Recompute once outside the timing loop to report which chunk_ids survived.
    cur = MagicMock()
    cur.fetchall.side_effect = [child_rows, []]
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    sample_results = rs._query_table_store(conn, embedding, None, None, top_k=15)
    wide_table_windows = sorted(r.chunk_id for r in sample_results if r.chunk_id.startswith("wide-"))

    return {
        "cap": cap,
        "avg_result_count": sum(total_result_counts) / len(total_result_counts),
        "elapsed_sec_for_n_iters": elapsed,
        "n_iters": n_iters,
        "wide_table_surviving_windows": wide_table_windows,
    }


def main():
    print("=== Table-store multi-window benchmark ===")
    print(f"Scenario: 1 wide table w/ 3 close windows (0.10/0.11/0.13) + 5 tables w/ 1 window each\n")

    before = _run_once(cap=1)   # old hard 1-per-table dedup behavior
    after = _run_once(cap=settings.TABLE_MAX_WINDOWS_PER_QUERY_RESULT if False else 2)  # shipped default

    print("BEFORE (cap=1, old behavior):")
    print(f"  avg result count : {before['avg_result_count']}")
    print(f"  wide-table windows returned: {before['wide_table_surviving_windows']}")
    print(f"  time for {before['n_iters']} iters: {before['elapsed_sec_for_n_iters']:.4f}s\n")

    print("AFTER (cap=2, shipped default):")
    print(f"  avg result count : {after['avg_result_count']}")
    print(f"  wide-table windows returned: {after['wide_table_surviving_windows']}")
    print(f"  time for {after['n_iters']} iters: {after['elapsed_sec_for_n_iters']:.4f}s\n")

    delta_pct = (
        (after["elapsed_sec_for_n_iters"] - before["elapsed_sec_for_n_iters"])
        / before["elapsed_sec_for_n_iters"] * 100
    )
    print(f"Result count delta: +{after['avg_result_count'] - before['avg_result_count']:.0f} "
          f"windows/query for the wide table (extra genuinely-relevant window recovered)")
    print(f"Timing delta: {delta_pct:+.1f}% (in-memory dedup change only; no extra SQL round-trip)")


if __name__ == "__main__":
    main()
