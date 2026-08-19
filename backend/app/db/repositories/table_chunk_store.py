"""table_chunk_store repository (Deprecated / No-op).

Table chunks have been superseded by table_store (macro), table_row_store (micro),
and table_cell_store (atomic facts).
"""
import logging

logger = logging.getLogger(__name__)


def insert_table_chunks(rows: list[tuple]) -> int:
    """No-op stub following migration 022."""
    return len(rows)


def update_table_chunk_counts(counts_by_table_id: dict[str, int]) -> int:
    """No-op stub following migration 022."""
    return len(counts_by_table_id)
