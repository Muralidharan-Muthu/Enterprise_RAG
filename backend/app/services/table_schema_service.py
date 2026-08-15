"""Per-cell typing for table_cell_store ingestion.

Given a table's headers/rows (the same {"headers": [...], "rows": [[...]]}
shape stored in table_store.json_data), produces one (value_text,
value_numeric, value_raw) triple per cell, ready to insert into
table_cell_store. Reuses the exact same normalization already used at
query time by table_query_engine.py (_normalize_label for text,
_normalize_numeric_token for numbers) so ingestion-time and query-time
values are guaranteed comparable — no separate dtype-inference pass is
needed: whether a cell is "numeric" is simply whether it parses as one.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from app.services.table_query_engine import _normalize_label
from app.services.table_reconstruction import _normalize_numeric_token


class CellValue(NamedTuple):
    row_index: int
    column_name: str
    column_index: int
    value_text: Optional[str]
    value_numeric: Optional[float]
    value_raw: Optional[str]


def build_cell_values(headers: list[str], rows: list[list]) -> list[CellValue]:
    """Flatten a table into per-cell typed values for table_cell_store.

    Rows shorter than headers (ragged extraction) are handled defensively —
    missing trailing cells are simply skipped, not padded with nulls, since
    table_cell_store has no NOT NULL constraint on value_text/value_numeric
    (a cell can legitimately be present-but-unparseable as both).
    """
    if not headers or not rows:
        return []

    cells: list[CellValue] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            continue
        for column_index, header in enumerate(headers):
            if column_index >= len(row):
                continue
            raw = row[column_index]
            if raw is None:
                continue
            raw_str = str(raw)

            norm_text = _normalize_label(raw_str) or None

            numeric_val: Optional[float] = None
            norm_numeric = _normalize_numeric_token(raw_str)
            if norm_numeric is not None:
                try:
                    numeric_val = float(norm_numeric)
                except ValueError:
                    numeric_val = None

            cells.append(CellValue(
                row_index=row_index,
                column_name=str(header),
                column_index=column_index,
                value_text=norm_text,
                value_numeric=numeric_val,
                value_raw=raw_str,
            ))
    return cells


def build_row_objects(headers: list[str], rows: list[list]) -> list[dict]:
    """Build the keyed {header: cell, ...} row objects for table_row_store,
    in the same row order as `rows` (row_index = list position)."""
    if not headers:
        return []
    objects = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            objects.append({})
            continue
        objects.append({
            str(headers[i]): row[i]
            for i in range(len(headers))
            if i < len(row)
        })
    return objects


def build_row_store_rows(document_id: str, table_id: str, headers: list[str], rows: list[list]) -> list[tuple]:
    """DB-ready tuples for table_cell_store.insert_table_rows:
    (document_id, table_id, row_index, row_data_json_str)."""
    import json as _json

    objects = build_row_objects(headers, rows)
    return [
        (document_id, table_id, row_index, _json.dumps(obj, default=str))
        for row_index, obj in enumerate(objects)
    ]


def build_cell_store_rows(document_id: str, table_id: str, headers: list[str], rows: list[list]) -> list[tuple]:
    """DB-ready tuples for table_cell_store.insert_table_cells:
    (document_id, table_id, row_index, column_name, column_index,
    value_text, value_numeric, value_raw)."""
    cells = build_cell_values(headers, rows)
    return [
        (document_id, table_id, c.row_index, c.column_name, c.column_index,
         c.value_text, c.value_numeric, c.value_raw)
        for c in cells
    ]
