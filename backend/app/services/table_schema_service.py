"""Row-level structured extraction and typing for table_row_store ingestion.

Produces unified (row_data, row_numeric, row_text) tuples for table_row_store.
Reuses the exact same normalization used at query time by table_query_engine.py
(_normalize_label for text, _normalize_numeric_token for numbers).
"""
from __future__ import annotations

import json
from typing import NamedTuple, Optional

from app.services.table_query_engine import _normalize_label
from app.services.table_reconstruction import _normalize_numeric_token


class UnifiedRow(NamedTuple):
    row_index: int
    row_data: dict
    row_numeric: dict
    row_text: str


def build_unified_rows(headers: list[str], rows: list[list]) -> list[UnifiedRow]:
    """Build unified row representations: full row dict, numeric value map, and text line."""
    if not headers or not rows:
        return []

    unified = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            continue

        row_data: dict[str, str] = {}
        row_numeric: dict[str, float] = {}
        text_parts: list[str] = []

        for column_index, header in enumerate(headers):
            h_str = str(header)
            if column_index >= len(row):
                continue
            raw = row[column_index]
            if raw is None:
                continue
            raw_str = str(raw).strip()
            if not raw_str:
                continue

            row_data[h_str] = raw_str
            text_parts.append(f"{h_str}: {raw_str}")

            norm_numeric = _normalize_numeric_token(raw_str)
            if norm_numeric is not None:
                try:
                    row_numeric[h_str] = float(norm_numeric)
                except ValueError:
                    pass

        row_text = " | ".join(text_parts)
        unified.append(UnifiedRow(
            row_index=row_index,
            row_data=row_data,
            row_numeric=row_numeric,
            row_text=row_text,
        ))

    return unified


def build_row_objects(headers: list[str], rows: list[list]) -> list[dict]:
    """Build keyed {header: cell, ...} row objects."""
    return [r.row_data for r in build_unified_rows(headers, rows)]


def build_row_store_rows(
    document_id: str,
    table_id: str,
    headers: list[str],
    rows: list[list],
    embeddings: Optional[list | object] = None,
) -> list[tuple]:
    """DB-ready tuples for table_cell_store.insert_table_rows:
    (document_id, table_id, row_index, row_data_json, row_numeric_json, row_text[, embedding_str])."""
    unified_rows = build_unified_rows(headers, rows)
    out: list[tuple] = []
    for idx, r in enumerate(unified_rows):
        emb_str = None
        if embeddings is not None and idx < len(embeddings):
            emb_val = embeddings[idx]
            if emb_val is not None and hasattr(emb_val, "__iter__"):
                emb_str = f"[{','.join(f'{x:.8f}' for x in emb_val)}]"

        if emb_str is not None:
            out.append((
                document_id,
                table_id,
                r.row_index,
                json.dumps(r.row_data, default=str),
                json.dumps(r.row_numeric),
                r.row_text,
                emb_str,
            ))
        else:
            out.append((
                document_id,
                table_id,
                r.row_index,
                json.dumps(r.row_data, default=str),
                json.dumps(r.row_numeric),
                r.row_text,
            ))
    return out


def build_cell_store_rows(document_id: str, table_id: str, headers: list[str], rows: list[list]) -> list[tuple]:
    """Deprecated / No-op backward compatibility stub."""
    return []
