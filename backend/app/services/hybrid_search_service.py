"""
Hybrid Search Service — keyword (full-text) + semantic fusion for Feature 1.1.

Public API:
  keyword_search(query, document_types, document_id, top_k_per_store)
      -> list[RetrievedChunk]
  rrf_fuse_lists(lists, k) -> list[RetrievedChunk]
  hybrid_retrieve(query, document_types, document_id, top_k_per_store)
      -> list[RetrievedChunk]

Design:
- keyword_search mirrors the per-store ThreadPool fan-out in retriever_service,
  reusing _run_store_query, _type_filter, _doc_filter, and RetrievedChunk so
  downstream code (balanced_pool, rrf_merge, _rank_chunks) is unchanged.
- Full-text uses websearch_to_tsquery (never to_tsquery) so arbitrary user text
  never raises a Postgres error.
- Rank normalization: ts_rank_cd returns [0, 1] per Postgres docs; we set
  RetrievedChunk.distance = 1.0 - rank so lower-distance = more relevant,
  matching the convention the rest of the pipeline expects.
- rrf_fuse_lists: deduplicates by chunk_id, sums RRF scores across modalities,
  and retains the richer RetrievedChunk instance (the one with more non-None fields).
- hybrid_retrieve: gates keyword half on settings.HYBRID_SEARCH_ENABLED; when
  disabled, falls straight through to semantic-only retrieve().
"""
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.config import settings
from app.core import tracing
from app.db.connection import get_db
from app.services.retriever_service import (
    RetrievedChunk,
    _run_store_query,
    _type_filter,
    _doc_filter,
    _select_stores,
    retrieve,
    ALL_STORE_KEYS,
    INTENT_CONFIDENCE_THRESHOLD,
)

logger = logging.getLogger(__name__)

# ── Per-store keyword search functions ────────────────────────────────────────


def _kw_vector_store(conn, query: str, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "vs")

    sql = f"""
        SELECT
            vs.id::text, vs.document_id::text, vs.chunk_text,
            vs.page_number, vs.section_title,
            ts_rank_cd(vs.chunk_text_tsv, query) AS rank,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket, vs.bbox
        FROM multi_store_rag_working.vector_store vs
        JOIN multi_store_rag_working.document_registry dr ON dr.id = vs.document_id,
        websearch_to_tsquery('english', %s) query
        WHERE dr.status = 'completed'
          AND vs.chunk_text_tsv @@ query
        {type_sql} {doc_sql}
        ORDER BY rank DESC
        LIMIT %s
    """
    params = [query] + type_params + doc_params + [top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    max_rank = max((float(r[5]) for r in rows), default=1.0) or 1.0
    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], section_title=r[4],
            distance=1.0 - (float(r[5]) / max_rank),
            document_filename=r[6], document_type=r[7],
            pdf_storage_path=r[8], pdf_bucket=r[9], bbox=r[10],
            store_type="vector",
        )
        for r in rows
    ]


def _kw_clause_store(conn, query: str, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "cs")

    sql = f"""
        SELECT
            cs.id::text, cs.document_id::text, cs.clause_text,
            cs.page_number, cs.clause_type, cs.risk_level,
            ts_rank_cd(cs.clause_text_tsv, query) AS rank,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket
        FROM multi_store_rag_working.clause_store cs
        JOIN multi_store_rag_working.document_registry dr ON dr.id = cs.document_id,
        websearch_to_tsquery('english', %s) query
        WHERE dr.status = 'completed'
          AND cs.clause_text_tsv @@ query
        {type_sql} {doc_sql}
        ORDER BY rank DESC
        LIMIT %s
    """
    params = [query] + type_params + doc_params + [top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    max_rank = max((float(r[6]) for r in rows), default=1.0) or 1.0
    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], clause_type=r[4], risk_level=r[5],
            distance=1.0 - (float(r[6]) / max_rank),
            document_filename=r[7], document_type=r[8],
            pdf_storage_path=r[9], pdf_bucket=r[10],
            store_type="clause",
        )
        for r in rows
    ]


def _kw_table_store(conn, query: str, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "ts")

    sql = f"""
        SELECT
            ts.id::text, ts.document_id::text,
            COALESCE(ts.table_summary, ts.raw_text, '') AS text,
            ts.page_number, ts.markdown_text,
            ts_rank_cd(ts.table_text_tsv, query) AS rank,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket, ts.bbox
        FROM multi_store_rag_working.table_store ts
        JOIN multi_store_rag_working.document_registry dr ON dr.id = ts.document_id,
        websearch_to_tsquery('english', %s) query
        WHERE dr.status = 'completed'
          AND ts.table_text_tsv @@ query
        {type_sql} {doc_sql}
        ORDER BY rank DESC
        LIMIT %s
    """
    params = [query] + type_params + doc_params + [top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    max_rank = max((float(r[5]) for r in rows), default=1.0) or 1.0
    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], table_markdown=r[4],
            distance=1.0 - (float(r[5]) / max_rank),
            document_filename=r[6], document_type=r[7],
            pdf_storage_path=r[8], pdf_bucket=r[9], bbox=r[10],
            store_type="table",
        )
        for r in rows
    ]


_KW_STORE_FNS = {
    "vector": _kw_vector_store,
    "clause": _kw_clause_store,
    "table": _kw_table_store,
}


def _run_kw_store_query(store_key: str, query_fn, query: str, document_types, document_id, top_k: int) -> list:
    """Run a single keyword store query on its own pooled connection.
    Per-store failures are isolated — a missing migration (no tsv column) returns
    [] rather than causing a 500."""
    try:
        with tracing.stage(f"kw-store-query:{store_key}"):
            with get_db() as conn:
                return query_fn(conn, query, document_types, document_id, top_k)
    except Exception as exc:
        logger.warning("Keyword store '%s' query failed (skipping): %s", store_key, exc)
        return []


# ── Startup verification ─────────────────────────────────────────────────────

# store_type -> (table, tsvector column) expected by the keyword_search SQL above.
_EXPECTED_TSV_COLUMNS = {
    "vector_store": "chunk_text_tsv",
    "clause_store": "clause_text_tsv",
    "table_store": "table_text_tsv",
}


def verify_fulltext_columns() -> list:
    """Return the list of expected tsvector columns MISSING from the schema.

    The keyword half of hybrid search depends on the GENERATED tsvector columns
    created by migration 011_fulltext_search.sql. If that migration hasn't been
    applied, keyword_search() silently returns [] per store (each _kw_* query
    raises 'column does not exist' and is swallowed by _run_kw_store_query), so
    hybrid retrieval degrades to semantic-only with NO error surfaced anywhere.

    This check makes that degradation loud at startup. Returns a list of
    "table.column" strings that are absent; empty list means fully provisioned.
    Raises nothing it can't turn into a result — DB errors propagate to the
    caller, which logs them as a non-fatal warning.
    """
    schema = settings.SUPABASE_SCHEMA
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND column_name LIKE '%%_tsv'
                """,
                (schema,),
            )
            present = {(t, c) for t, c in cur.fetchall()}

    missing = [
        f"{table}.{col}"
        for table, col in _EXPECTED_TSV_COLUMNS.items()
        if (table, col) not in present
    ]
    return missing


# ── Public API ─────────────────────────────────────────────────────────────────


def keyword_search(
    query: str,
    document_types: Optional[list] = None,
    document_id: Optional[str] = None,
    top_k_per_store: int = 15,
    intent: Optional[dict] = None,
) -> list:
    """Full-text (tsvector GIN) keyword search across all relevant stores.

    Store selection mirrors retriever_service._select_stores so the same intent
    hint narrows keyword search to the same stores as semantic search.
    image_store is excluded — semantic-only.

    Returns list[RetrievedChunk] sorted by distance ascending (lower = better).
    """
    flags = _select_stores(document_types, use_intent=bool(intent), intent=intent)
    active = [(k, fn) for k, fn in _KW_STORE_FNS.items() if flags.get(k)]
    if not active:
        return []

    results: list = []
    with tracing.stage("keyword-store-fanout", stores=",".join(k for k, _ in active)):
        # Fresh copy_context() per task (not one shared ctx) — Context.run() is
        # not reentrant, so concurrent submit() calls need independent copies.
        with ThreadPoolExecutor(max_workers=len(active)) as ex:
            futures = [
                ex.submit(
                    contextvars.copy_context().run, _run_kw_store_query, k, fn,
                    query, document_types, document_id, top_k_per_store,
                )
                for k, fn in active
            ]
            for f in futures:
                results.extend(f.result())

    results.sort(key=lambda c: c.distance)
    return results


def _richness(chunk: RetrievedChunk) -> int:
    """Count non-None optional fields — used to keep the richer instance on dedup."""
    fields = (
        chunk.document_filename, chunk.document_type, chunk.page_number,
        chunk.section_title, chunk.clause_type, chunk.risk_level,
        chunk.chunk_type, chunk.source_doi, chunk.table_markdown,
        chunk.image_storage_path, chunk.caption, chunk.ocr_text,
        chunk.bbox, chunk.pdf_storage_path, chunk.pdf_bucket,
    )
    return sum(1 for f in fields if f is not None)


def rrf_fuse_lists(lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion across two or more ranked lists of RetrievedChunk.

    - Deduplicates by chunk_id, SUMMING RRF scores from each list.
    - Retains the richer RetrievedChunk instance (more non-None fields) to
      preserve citation metadata.
    - Returns list sorted by combined RRF score descending, with relevance_score
      set to the combined score.
    """
    scores: dict[str, float] = {}
    chunks: dict[str, RetrievedChunk] = {}

    for ranked_list in lists:
        for rank, chunk in enumerate(ranked_list, start=1):
            rrf_score = 1.0 / (k + rank)
            cid = chunk.chunk_id
            if cid not in scores:
                scores[cid] = 0.0
                chunks[cid] = chunk
            scores[cid] += rrf_score
            # keep the richer instance
            if _richness(chunk) > _richness(chunks[cid]):
                chunks[cid] = chunk

    fused = list(chunks.values())
    for c in fused:
        c.relevance_score = scores[c.chunk_id]

    fused.sort(key=lambda c: c.relevance_score, reverse=True)
    return fused


def hybrid_retrieve(
    query: str,
    document_types: Optional[list] = None,
    document_id: Optional[str] = None,
    top_k_per_store: int = 15,
    intent: Optional[dict] = None,
    table_filters=None,
) -> list:
    """Hybrid semantic + keyword retrieval, fused via RRF.

    When HYBRID_SEARCH_ENABLED=False, falls through to pure semantic retrieve().
    The keyword half is gated on the same flag so a missing migration (no tsv
    columns yet) degrades to semantic-only rather than failing.

    table_filters (optional TableFilters): forwarded to the SEMANTIC side only.
    The keyword side doesn't apply it — a table row excluded by the filter can
    still surface via a keyword-only match (narrow precision gap, flagged as a
    follow-up); the filter is never bypassed on the semantic side.

    Returns list[RetrievedChunk] sorted by RRF relevance_score descending
    (ready for balanced_pool / _rank_chunks).
    """
    with tracing.stage("hybrid-semantic"):
        semantic_results = retrieve(
            query=query,
            document_types=document_types,
            document_id=document_id,
            top_k_per_store=top_k_per_store,
            use_intent=True,
            intent=intent,
            table_filters=table_filters,
        )

    if not settings.HYBRID_SEARCH_ENABLED:
        logger.debug("HYBRID_SEARCH_ENABLED=False — returning semantic-only results")
        return semantic_results

    with tracing.stage("hybrid-keyword"):
        kw_results = keyword_search(
            query=query,
            document_types=document_types,
            document_id=document_id,
            top_k_per_store=top_k_per_store,
            intent=intent,
        )

    if not kw_results:
        logger.debug("Keyword search returned no results — using semantic-only")
        return semantic_results

    with tracing.stage("hybrid-rrf-fuse"):
        fused = rrf_fuse_lists(
            [semantic_results, kw_results],
            k=settings.HYBRID_RRF_K,
        )
    logger.info(
        "Hybrid retrieve: semantic=%d keyword=%d fused=%d",
        len(semantic_results), len(kw_results), len(fused),
    )
    return fused
