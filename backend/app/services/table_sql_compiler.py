"""Compiles table_condition_parser ASTs into parameterized SQL pushdown
queries against table_cell_store / table_row_store (migration 019).

This is the tier-1 execution engine for filter/aggregation/ranking/GROUP BY
table queries: runs entirely server-side (indexed WHERE/INTERSECT/UNION/
GROUP BY), never loads a full table into Python. table_query_engine.py's
Python/JSONB engine (scans table_store.json_data in memory) remains as the
tier-2 fallback for tables that haven't been backfilled into
table_cell_store yet, or if a pushdown query errors.

Multi-table: unlike a single best-match lookup, every candidate table whose
columns satisfy the query contributes matches — two tables sharing a
"Sector" column both surface rows instead of only the first.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.db.connection import get_db
from app.services.table_condition_parser import (
    BoolNode, Condition, ParsedFilter, RankingClause,
)
from app.services.table_query_engine import (
    _extract_target_column, _fuzzy_find_column, _normalize_label,
)

logger = logging.getLogger(__name__)

_OP_SYMBOLS = {"GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}


# ── Candidate table discovery (cell-store-backed tables only) ──────────────

def _fetch_pushdown_candidate_tables(
    conn, document_id: Optional[str], document_types: Optional[list],
) -> list[dict]:
    clauses = ["dr.status = 'completed'"]
    params: list = []

    if document_id:
        clauses.append("tcs.document_id = %s")
        params.append(document_id)
    if document_types:
        placeholders = ", ".join(["%s"] * len(document_types))
        clauses.append(f"dr.document_type IN ({placeholders})")
        params.extend(document_types)

    where_sql = " AND ".join(clauses)
    sql = f"""
        SELECT DISTINCT tcs.table_id::text, tcs.document_id::text,
               ts.table_title, dr.original_filename
        FROM multi_store_rag_working.table_cell_store tcs
        JOIN multi_store_rag_working.table_store ts ON ts.id = tcs.table_id
        JOIN multi_store_rag_working.document_registry dr ON dr.id = tcs.document_id
        WHERE {where_sql}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {"table_id": r[0], "document_id": r[1], "table_title": r[2], "filename": r[3]}
        for r in rows
    ]


def _table_columns(conn, table_id: str) -> list[str]:
    sql = """
        SELECT column_name, MIN(column_index) AS idx
        FROM multi_store_rag_working.table_cell_store
        WHERE table_id = %s
        GROUP BY column_name
        ORDER BY idx
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_id])
        return [r[0] for r in cur.fetchall()]


def _resolve_column(hint: str, columns: list[str]) -> Optional[str]:
    idx = _fuzzy_find_column(hint, columns)
    return columns[idx] if idx is not None else None


def _resolve_column_by_value(conn, table_id: str, values: list[str]) -> Optional[str]:
    """When a condition names no column at all ("list all companies in
    Chemicals" never says "Sector"), find whichever column's actual cell
    VALUES match best — the same strategy the tier-2 Python engine
    (_run_list_filter) already uses, just pushed into SQL: the column with
    the most rows whose normalized value_text equals one of `values` wins.
    Mirrors table_query_engine._fuzzy_find_column's exact-match preference
    (value_text is already normalized via _normalize_label at both
    ingestion and query time, so this is an exact indexed lookup, not a
    fuzzy scan)."""
    normalized = [_normalize_label(str(v)) for v in values]
    placeholders = ", ".join(["%s"] * len(normalized))
    sql = f"""
        SELECT column_name, COUNT(*) AS matches
        FROM multi_store_rag_working.table_cell_store
        WHERE table_id = %s AND value_text IN ({placeholders})
        GROUP BY column_name
        ORDER BY matches DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_id] + normalized)
        row = cur.fetchone()
    return row[0] if row else None


def _resolve_numeric_column_by_fallback(conn, table_id: str) -> Optional[str]:
    """When a numeric comparison ("invoices above $10,000") names no
    column, fall back to the table's sole numeric column if it has exactly
    one — a reasonable heuristic for the common case of one obvious amount/
    price/total column. Ambiguous (0 or 2+ numeric columns) tables are
    skipped rather than guessing wrong."""
    sql = """
        SELECT column_name
        FROM multi_store_rag_working.table_cell_store
        WHERE table_id = %s AND value_numeric IS NOT NULL
        GROUP BY column_name
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_id])
        numeric_columns = [r[0] for r in cur.fetchall()]
    return numeric_columns[0] if len(numeric_columns) == 1 else None


# ── Condition -> SQL compilation ────────────────────────────────────────

def _compile_condition(conn, cond: Condition, table_id: str, columns: list[str]) -> Optional[tuple]:
    if cond.column_hint is not None:
        column = _resolve_column(cond.column_hint, columns)
    elif cond.op in ("EQ", "IN", "LIKE"):
        values = cond.value if cond.op == "IN" else [cond.value]
        column = _resolve_column_by_value(conn, table_id, values)
    elif cond.op in _OP_SYMBOLS or cond.op == "BETWEEN":
        column = _resolve_numeric_column_by_fallback(conn, table_id)
    else:
        column = None

    if column is None:
        return None

    base = (
        "SELECT row_index FROM multi_store_rag_working.table_cell_store "
        "WHERE table_id = %s AND column_name = %s"
    )
    params: list = [table_id, column]

    if cond.op == "EQ":
        return base + " AND value_text = %s", params + [_normalize_label(str(cond.value))]
    if cond.op == "LIKE":
        return base + " AND value_text ILIKE %s", params + [f"%{_normalize_label(str(cond.value))}%"]
    if cond.op == "IN":
        values = [_normalize_label(str(v)) for v in cond.value]
        placeholders = ", ".join(["%s"] * len(values))
        return base + f" AND value_text IN ({placeholders})", params + values
    if cond.op in _OP_SYMBOLS:
        return (
            base + f" AND value_numeric {_OP_SYMBOLS[cond.op]} %s",
            params + [cond.value],
        )
    if cond.op == "BETWEEN":
        lo, hi = cond.value
        return base + " AND value_numeric BETWEEN %s AND %s", params + [lo, hi]

    return None


def _compile_tree(conn, node, table_id: str, columns: list[str]) -> Optional[tuple]:
    """Returns (sql, params) producing a row_index result set, or None when
    the tree can't be satisfied against this table's columns at all."""
    if isinstance(node, Condition):
        return _compile_condition(conn, node, table_id, columns)

    if isinstance(node, BoolNode):
        compiled = [_compile_tree(conn, c, table_id, columns) for c in node.children]

        if node.op == "AND":
            # Every branch must be satisfiable — if any leaf's column doesn't
            # exist in this table, the whole conjunction is impossible here.
            if any(c is None for c in compiled):
                return None
            sqls = [f"({sql})" for sql, _ in compiled]
            params = [p for _, ps in compiled for p in ps]
            return " INTERSECT ".join(sqls), params

        if node.op == "OR":
            surviving = [c for c in compiled if c is not None]
            if not surviving:
                return None
            sqls = [f"({sql})" for sql, _ in surviving]
            params = [p for _, ps in surviving for p in ps]
            return " UNION ".join(sqls), params

    return None


def _hydrate_rows(conn, table_id: str, row_indices: list[int], order_sql: str = "row_index") -> list[dict]:
    if not row_indices:
        return []
    sql = f"""
        SELECT row_data FROM multi_store_rag_working.table_row_store
        WHERE table_id = %s AND row_index = ANY(%s)
        ORDER BY {order_sql}
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_id, row_indices])
        return [r[0] for r in cur.fetchall()]


# ── Public entry points ─────────────────────────────────────────────────

def run_filter(
    tree,
    document_id: Optional[str] = None,
    document_types: Optional[list] = None,
    ranking: Optional[RankingClause] = None,
    max_rows: int = 200,
) -> Optional[dict]:
    """Exhaustive filter (and optional ranking sort/limit) across every
    cell-store-backed candidate table. Returns None only when no table's
    columns could satisfy the condition tree at all (caller falls back to
    the Python/JSONB tier-2 engine)."""
    try:
        with get_db() as conn:
            tables = _fetch_pushdown_candidate_tables(conn, document_id, document_types)
            if not tables:
                return None

            matched_table_ids: list[str] = []
            all_rows: list[dict] = []
            ranking_column: Optional[str] = None
            row_numeric_values: list[float] = []

            for t in tables:
                table_id = t["table_id"]
                columns = _table_columns(conn, table_id)
                if not columns:
                    continue

                compiled = _compile_tree(conn, tree, table_id, columns) if tree is not None else None
                if tree is not None and compiled is None:
                    continue

                if compiled is not None:
                    sql, params = compiled
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        row_indices = [r[0] for r in cur.fetchall()]
                else:
                    # No condition tree (pure ranking/"list all" with no filter) —
                    # every row in this table is a candidate.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT DISTINCT row_index FROM multi_store_rag_working.table_cell_store "
                            "WHERE table_id = %s",
                            [table_id],
                        )
                        row_indices = [r[0] for r in cur.fetchall()]

                if not row_indices:
                    continue

                if ranking is not None:
                    rc_hint = ranking.column_hint
                    rc = _resolve_column(rc_hint, columns) if rc_hint else None
                    if rc is None:
                        # Fall back to the only numeric column if the hint didn't resolve.
                        continue
                    ranking_column = ranking_column or rc
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT row_index, value_numeric FROM multi_store_rag_working.table_cell_store "
                            "WHERE table_id = %s AND column_name = %s "
                            "AND row_index = ANY(%s) AND value_numeric IS NOT NULL",
                            [table_id, rc, row_indices],
                        )
                        scored = cur.fetchall()
                    if not scored:
                        continue
                    idx_to_score = {r[0]: float(r[1]) for r in scored}
                    row_indices = list(idx_to_score.keys())
                    rows = _hydrate_rows(conn, table_id, row_indices)
                    for ri, row in zip(row_indices, rows):
                        row_numeric_values.append(idx_to_score.get(ri, 0.0))
                        all_rows.append(row)
                else:
                    rows = _hydrate_rows(conn, table_id, row_indices)
                    all_rows.extend(rows)

                matched_table_ids.append(table_id)

            if not all_rows:
                return None

            if ranking is not None and ranking_column is not None:
                paired = list(zip(row_numeric_values, all_rows))
                paired.sort(key=lambda p: p[0], reverse=(ranking.direction == "DESC"))
                paired = paired[: ranking.limit]
                all_rows = [row for _, row in paired]

            matched_row_count = len(all_rows)
            truncated = matched_row_count > max_rows
            shown_rows = all_rows[:max_rows]

            return {
                "operation": "RANKING" if ranking is not None else "FILTER",
                "value": shown_rows,
                "matched_table_ids": matched_table_ids,
                "matched_row_count": matched_row_count,
                "shown_row_count": len(shown_rows),
                "truncated": truncated,
                "table_title": None,
                "document_id": document_id,
                "filename": None,
            }
    except Exception as exc:
        logger.warning("SQL pushdown filter failed (falling back to tier-2): %s", exc)
        return None


_AGG_SQL = {"SUM": "SUM", "AVG": "AVG", "MIN": "MIN", "MAX": "MAX"}


def run_aggregate(
    operation: str,
    query: str,
    tree: Optional[object],
    document_id: Optional[str] = None,
    document_types: Optional[list] = None,
) -> Optional[dict]:
    """SQL-side SUM/AVG/COUNT/MIN/MAX, optionally scoped by a WHERE condition
    tree, across every cell-store-backed candidate table (summed/merged
    across tables that share the aggregated column). Column resolution
    reuses table_query_engine._extract_target_column's sliding-window fuzzy
    match against the raw query text and this table's real headers, the
    same logic the tier-2 Python engine already relies on."""
    try:
        with get_db() as conn:
            tables = _fetch_pushdown_candidate_tables(conn, document_id, document_types)
            if not tables:
                return None

            matched_table_ids: list[str] = []
            values: list[float] = []
            row_count_considered = 0
            resolved_column_name: Optional[str] = None

            for t in tables:
                table_id = t["table_id"]
                columns = _table_columns(conn, table_id)
                if not columns:
                    continue
                col_idx = _extract_target_column(query, columns)
                column = columns[col_idx] if col_idx is not None else None
                if column is None and operation != "COUNT":
                    continue

                row_indices: Optional[list[int]] = None
                if tree is not None:
                    compiled = _compile_tree(conn, tree, table_id, columns)
                    if compiled is None:
                        continue
                    sql, params = compiled
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        row_indices = [r[0] for r in cur.fetchall()]
                    if not row_indices:
                        continue

                if operation == "COUNT":
                    if row_indices is not None:
                        row_count_considered += len(row_indices)
                    else:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT COUNT(DISTINCT row_index) FROM multi_store_rag_working.table_cell_store "
                                "WHERE table_id = %s",
                                [table_id],
                            )
                            row_count_considered += cur.fetchone()[0]
                    matched_table_ids.append(table_id)
                    continue

                resolved_column_name = column
                if row_indices is not None:
                    sql = (
                        "SELECT value_numeric FROM multi_store_rag_working.table_cell_store "
                        "WHERE table_id = %s AND column_name = %s AND row_index = ANY(%s) "
                        "AND value_numeric IS NOT NULL"
                    )
                    params = [table_id, column, row_indices]
                else:
                    sql = (
                        "SELECT value_numeric FROM multi_store_rag_working.table_cell_store "
                        "WHERE table_id = %s AND column_name = %s AND value_numeric IS NOT NULL"
                    )
                    params = [table_id, column]
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    col_values = [float(r[0]) for r in cur.fetchall()]
                if col_values:
                    values.extend(col_values)
                    matched_table_ids.append(table_id)

            if not matched_table_ids:
                return None

            if operation == "COUNT":
                value = row_count_considered
            elif operation == "SUM":
                value = sum(values)
            elif operation == "AVG":
                if not values:
                    return None
                value = sum(values) / len(values)
            elif operation == "MIN":
                if not values:
                    return None
                value = min(values)
            elif operation == "MAX":
                if not values:
                    return None
                value = max(values)
            else:
                return None

            return {
                "operation": operation,
                "column": resolved_column_name or "rows",
                "value": value,
                "matched_table_ids": matched_table_ids,
                "row_count_considered": row_count_considered or len(values),
                "unparseable_count": 0,
                "filter_description": f"{operation}({resolved_column_name or 'rows'}) via indexed pushdown"
                                       f"{' across ' + str(len(matched_table_ids)) + ' table(s)' if len(matched_table_ids) > 1 else ''}",
                "table_title": None,
                "document_id": document_id,
                "filename": None,
            }
    except Exception as exc:
        logger.warning("SQL pushdown aggregate failed (falling back to tier-2): %s", exc)
        return None


def run_group_by(
    group_by_hint: str,
    agg_op: str,
    agg_query_text: str,
    tree: Optional[object],
    document_id: Optional[str] = None,
    document_types: Optional[list] = None,
) -> Optional[dict]:
    """GROUP BY <group_by_hint>, <agg_op>(<column resolved from agg_query_text>),
    optionally scoped by a WHERE condition tree. Merges groups with the same
    key across every candidate table. agg_query_text should be the original
    query with the "by <column>" grouping clause already stripped, so
    _extract_target_column's sliding-window match isn't misled by the group
    column's own name."""
    agg_sql = _AGG_SQL.get(agg_op)
    if agg_sql is None and agg_op != "COUNT":
        return None

    try:
        with get_db() as conn:
            tables = _fetch_pushdown_candidate_tables(conn, document_id, document_types)
            if not tables:
                return None

            merged: dict[str, list[float]] = {}
            matched_table_ids: list[str] = []
            group_col_resolved: Optional[str] = None
            agg_col_resolved: Optional[str] = None

            for t in tables:
                table_id = t["table_id"]
                columns = _table_columns(conn, table_id)
                if not columns:
                    continue
                group_col = _resolve_column(group_by_hint, columns)
                if group_col is None:
                    continue
                agg_col_idx = _extract_target_column(agg_query_text, columns) if agg_op != "COUNT" else None
                agg_col = columns[agg_col_idx] if agg_col_idx is not None else None
                if agg_col is None and agg_op != "COUNT":
                    continue

                row_filter_sql, row_filter_params = "", []
                if tree is not None:
                    compiled = _compile_tree(conn, tree, table_id, columns)
                    if compiled is None:
                        continue
                    sub_sql, sub_params = compiled
                    row_filter_sql = f"AND g.row_index IN ({sub_sql})"
                    row_filter_params = sub_params

                if agg_op == "COUNT":
                    sql = f"""
                        SELECT g.value_text, COUNT(*)
                        FROM multi_store_rag_working.table_cell_store g
                        WHERE g.table_id = %s AND g.column_name = %s {row_filter_sql}
                        GROUP BY g.value_text
                    """
                    params = [table_id, group_col] + row_filter_params
                else:
                    sql = f"""
                        SELECT g.value_text, a.value_numeric
                        FROM multi_store_rag_working.table_cell_store g
                        JOIN multi_store_rag_working.table_cell_store a
                          ON a.table_id = g.table_id AND a.row_index = g.row_index
                        WHERE g.table_id = %s AND g.column_name = %s
                          AND a.column_name = %s AND a.value_numeric IS NOT NULL
                          {row_filter_sql}
                    """
                    params = [table_id, group_col, agg_col] + row_filter_params

                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()

                if not rows:
                    continue

                group_col_resolved = group_col
                agg_col_resolved = agg_col
                matched_table_ids.append(table_id)

                for key, val in rows:
                    if key is None:
                        continue
                    bucket = merged.setdefault(key, [])
                    if agg_op == "COUNT":
                        bucket.append(float(val))
                    elif val is not None:
                        bucket.append(float(val))

            if not merged:
                return None

            groups = []
            for key, vals in merged.items():
                if agg_op == "COUNT":
                    gval = sum(vals)
                elif agg_op == "SUM":
                    gval = sum(vals)
                elif agg_op == "AVG":
                    gval = sum(vals) / len(vals) if vals else 0.0
                elif agg_op == "MIN":
                    gval = min(vals) if vals else None
                elif agg_op == "MAX":
                    gval = max(vals) if vals else None
                else:
                    gval = None
                groups.append({"group": key, "value": gval})

            groups.sort(key=lambda g: (g["value"] is None, g["value"]), reverse=True)

            return {
                "operation": "GROUP_BY",
                "column": f"{agg_op}({agg_col_resolved or 'rows'}) by {group_col_resolved or group_by_hint}",
                "value": groups,
                "matched_table_ids": matched_table_ids,
                "row_count_considered": sum(len(v) for v in merged.values()),
                "filter_description": f"{agg_op}({agg_col_resolved or 'rows'}) grouped by "
                                       f"{group_col_resolved or group_by_hint}",
                "table_title": None,
                "document_id": document_id,
                "filename": None,
            }
    except Exception as exc:
        logger.warning("SQL pushdown GROUP BY failed (falling back to tier-2): %s", exc)
        return None
