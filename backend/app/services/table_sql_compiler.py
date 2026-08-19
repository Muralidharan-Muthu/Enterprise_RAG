"""Compiles table_condition_parser ASTs into parameterized SQL pushdown
queries against unified table_row_store (migration 023).

This is the tier-1 execution engine for filter/aggregation/ranking/GROUP BY
table queries: runs entirely server-side (indexed WHERE/INTERSECT/UNION/
GROUP BY) against table_row_store (row_data + row_numeric + row_text).
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


# ── Candidate table discovery ──────────────────────────────────────────────

def _fetch_pushdown_candidate_tables(
    conn, document_id: Optional[str], document_types: Optional[list],
) -> list[dict]:
    clauses = ["dr.status = 'completed'"]
    params: list = []

    if document_id:
        clauses.append("trs.document_id = %s")
        params.append(document_id)
    if document_types:
        placeholders = ", ".join(["%s"] * len(document_types))
        clauses.append(f"dr.document_type IN ({placeholders})")
        params.extend(document_types)

    where_sql = " AND ".join(clauses)
    sql = f"""
        SELECT DISTINCT trs.table_id::text, trs.document_id::text,
               ts.table_title, dr.original_filename
        FROM multi_store_rag_working.table_row_store trs
        JOIN multi_store_rag_working.table_store ts ON ts.id = trs.table_id
        JOIN multi_store_rag_working.document_registry dr ON dr.id = trs.document_id
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
        SELECT DISTINCT jsonb_object_keys(row_data)
        FROM multi_store_rag_working.table_row_store
        WHERE table_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_id])
        return [r[0] for r in cur.fetchall()]


def _resolve_column(hint: str, columns: list[str]) -> Optional[str]:
    idx = _fuzzy_find_column(hint, columns)
    return columns[idx] if idx is not None else None


def _resolve_column_by_value(conn, table_id: str, values: list[str]) -> Optional[str]:
    normalized = [_normalize_label(str(v)) for v in values]
    placeholders = ", ".join(["%s"] * len(normalized))
    sql = f"""
        SELECT key, COUNT(*) AS matches
        FROM multi_store_rag_working.table_row_store,
             jsonb_each_text(row_data)
        WHERE table_id = %s AND LOWER(TRIM(value)) IN ({placeholders})
        GROUP BY key
        ORDER BY matches DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_id] + normalized)
        row = cur.fetchone()
    return row[0] if row else None


def _resolve_numeric_column_by_fallback(conn, table_id: str) -> Optional[str]:
    sql = """
        SELECT DISTINCT jsonb_object_keys(row_numeric)
        FROM multi_store_rag_working.table_row_store
        WHERE table_id = %s
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
        "SELECT row_index FROM multi_store_rag_working.table_row_store "
        "WHERE table_id = %s"
    )
    params: list = [table_id]

    if cond.op == "EQ":
        return (
            base + " AND LOWER(TRIM(row_data->>%s)) = %s",
            params + [column, _normalize_label(str(cond.value))],
        )
    if cond.op == "LIKE":
        return (
            base + " AND LOWER(TRIM(row_data->>%s)) ILIKE %s",
            params + [column, f"%{_normalize_label(str(cond.value))}%"],
        )
    if cond.op == "IN":
        values = [_normalize_label(str(v)) for v in cond.value]
        placeholders = ", ".join(["%s"] * len(values))
        return (
            base + f" AND LOWER(TRIM(row_data->>%s)) IN ({placeholders})",
            params + [column] + values,
        )
    if cond.op in _OP_SYMBOLS:
        return (
            base + f" AND (row_numeric->>%s)::numeric {_OP_SYMBOLS[cond.op]} %s",
            params + [column, cond.value],
        )
    if cond.op == "BETWEEN":
        lo, hi = cond.value
        return (
            base + " AND (row_numeric->>%s)::numeric BETWEEN %s AND %s",
            params + [column, lo, hi],
        )

    return None


def _compile_tree(conn, node, table_id: str, columns: list[str]) -> Optional[tuple]:
    """Returns (sql, params) producing a row_index result set, or None when
    the tree can't be satisfied against this table's columns at all."""
    if isinstance(node, Condition):
        return _compile_condition(conn, node, table_id, columns)

    if isinstance(node, BoolNode):
        compiled = [_compile_tree(conn, c, table_id, columns) for c in node.children]

        if node.op == "AND":
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
    """Exhaustive filter and ranking across table_row_store candidate tables."""
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
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT DISTINCT row_index FROM multi_store_rag_working.table_row_store "
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
                        continue
                    ranking_column = ranking_column or rc
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT row_index, (row_numeric->>%s)::numeric FROM multi_store_rag_working.table_row_store "
                            "WHERE table_id = %s AND row_index = ANY(%s) AND (row_numeric->>%s) IS NOT NULL",
                            [rc, table_id, row_indices, rc],
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
    """SQL-side SUM/AVG/COUNT/MIN/MAX against table_row_store."""
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
                                "SELECT COUNT(DISTINCT row_index) FROM multi_store_rag_working.table_row_store "
                                "WHERE table_id = %s",
                                [table_id],
                            )
                            row_count_considered += cur.fetchone()[0]
                    matched_table_ids.append(table_id)
                    continue

                resolved_column_name = column
                if row_indices is not None:
                    sql = (
                        "SELECT (row_numeric->>%s)::numeric FROM multi_store_rag_working.table_row_store "
                        "WHERE table_id = %s AND row_index = ANY(%s) "
                        "AND (row_numeric->>%s) IS NOT NULL"
                    )
                    params = [column, table_id, row_indices, column]
                else:
                    sql = (
                        "SELECT (row_numeric->>%s)::numeric FROM multi_store_rag_working.table_row_store "
                        "WHERE table_id = %s AND (row_numeric->>%s) IS NOT NULL"
                    )
                    params = [column, table_id, column]
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
    """GROUP BY <group_by_hint>, <agg_op>(<column>) directly on table_row_store."""
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
                    row_filter_sql = f"AND row_index IN ({sub_sql})"
                    row_filter_params = sub_params

                if agg_op == "COUNT":
                    sql = f"""
                        SELECT row_data->>%s, COUNT(*)
                        FROM multi_store_rag_working.table_row_store
                        WHERE table_id = %s AND row_data->>%s IS NOT NULL {row_filter_sql}
                        GROUP BY row_data->>%s
                    """
                    params = [group_col, table_id, group_col] + row_filter_params + [group_col]
                else:
                    sql = f"""
                        SELECT row_data->>%s, (row_numeric->>%s)::numeric
                        FROM multi_store_rag_working.table_row_store
                        WHERE table_id = %s AND row_data->>%s IS NOT NULL
                          AND (row_numeric->>%s) IS NOT NULL
                          {row_filter_sql}
                    """
                    params = [group_col, agg_col, table_id, group_col, agg_col] + row_filter_params

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
