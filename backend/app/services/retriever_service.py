import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.config import settings
from app.core import tracing
from app.db.connection import get_db
from app.services.embedding_service import embed_query as _embed_query

logger = logging.getLogger(__name__)

RETRIEVAL_TOP_K_PER_STORE = 15

# image_store is intentionally NOT here: it is a pure extraction repository with
# no embedding column (migration 008). Image-derived content that is searchable
# was cross-stored into vector/table/clause/document by store_image_derived_chunks
# and is retrieved from those stores instead.
ALL_STORE_KEYS = ["vector", "clause", "research", "table"]

# Below this intent confidence we don't trust the narrowed store set — search all
# stores instead (recall-safe). At/above it, honor the intent's minimal set so a
# confident single-content query (e.g. a table question) hits only that store.
INTENT_CONFIDENCE_THRESHOLD = 0.5


def _select_stores(document_types, use_intent: bool, intent: dict | None) -> dict:
    """Decide which stores to search. Explicit document_types win; otherwise a
    confident intent dict ({"stores": [...], "confidence": float}) narrows;
    otherwise search all (recall-safe)."""
    if document_types:
        return {
            "vector": any(dt in document_types for dt in ["policy", "entity", "financial"]),
            "clause": "legal" in document_types,
            "research": "research" in document_types,
            "table": "financial" in document_types,
        }
    if use_intent and intent and intent.get("confidence", 0.0) >= INTENT_CONFIDENCE_THRESHOLD:
        stores = set(intent.get("stores") or ALL_STORE_KEYS)
        return {s: (s in stores) for s in ALL_STORE_KEYS}
    # No types, and intent absent or low-confidence → recall-safe: search all.
    return {s: True for s in ALL_STORE_KEYS}


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    text: str
    store_type: str  # 'vector' | 'clause' | 'research' | 'table'
    distance: float  # cosine distance (lower = more similar)
    document_filename: str = ""
    document_type: str = ""
    relevance_score: float = 0.0  # populated by reranker
    page_number: Optional[int] = None
    # Multi-page continuation tables (Phase 1: document_parser._merge_continued_tables
    # + table_chunker.build_row_windows) record page_start/page_end per child window
    # in table_chunk_store.chunk_metadata. page_number carries page_start (unchanged
    # field/semantics for ordinary single-page chunks); page_number_end is the extra
    # end-of-range page, populated ONLY when it differs from page_number. None for
    # every non-table store and for single-page table windows — fully backward
    # compatible default.
    page_number_end: Optional[int] = None
    section_title: Optional[str] = None
    clause_type: Optional[str] = None
    risk_level: Optional[str] = None
    chunk_type: Optional[str] = None
    source_doi: Optional[str] = None
    table_markdown: Optional[str] = None
    image_storage_path: Optional[str] = None
    caption: Optional[str] = None
    ocr_text: Optional[str] = None
    bbox: Optional[dict] = None          # {x1,y1,x2,y2} region in the source PDF
    pdf_storage_path: Optional[str] = None  # original PDF path in the bucket
    pdf_bucket: Optional[str] = None
    # True when `text` is a specific table_chunk_store row-window match (vs. a
    # table_store parent-summary/full-table match). Only ever True for
    # store_type == "table" chunks built via the child-hit branch of
    # _query_table_store(). Synthesis uses this to render the matched rows
    # instead of a generic head-of-table slice.
    is_child_match: bool = False
    # Extractive context-compression output (context_compression_service): the
    # query-relevant subset of `text`, sentence-selected via the cross-encoder.
    # Populated ONLY for text stores when CONTEXT_COMPRESSION_ENABLED; None for
    # tables/images/graph chunks and whenever compression is off or a no-op.
    # Synthesis prefers this for the LLM prompt; citations always use `text`.
    compressed_text: Optional[str] = None
    # True when this chunk was found via entity-graph reasoning (GraphRAG local
    # search / graph_expanded_chunks), not plain ANN. balanced_pool() exempts
    # these from its per-store raw-cosine-distance cap — a chunk the graph
    # correctly identified as relevant via a shared entity can have arbitrarily
    # poor embedding similarity to the query text (e.g. a transcript aside that
    # never restates the query's wording) and would otherwise be silently
    # dropped before the reranker ever saw it.
    from_graph: bool = False


def retrieve(
    query: str,
    document_types: Optional[list] = None,
    document_id: Optional[str] = None,
    top_k_per_store: int = RETRIEVAL_TOP_K_PER_STORE,
    use_intent: bool = True,
    intent: Optional[dict] = None,
    table_filters: Optional["TableFilters"] = None,
) -> list:
    """Embed query, retrieve from the relevant stores, return sorted by distance.
    Store selection: explicit document_types > query intent > all (recall-safe).

    table_filters (Slice 4, optional): a structured prefilter on table_store
    columns (currency/fiscal_year/table_category/numeric_only/min_quality),
    applied only to the 'table' store BEFORE its vector ANN. None (default)
    is a no-op — identical behavior to before this parameter existed.

    Optionally cached (RETRIEVAL_CACHE_ENABLED + RETRIEVAL_CACHE_RESULTS_ENABLED):
    a cache hit returns results up to RETRIEVAL_CACHE_RESULT_TTL_SECONDS stale
    with respect to the most recent ingestion — a documented, opt-in tradeoff.
    """
    _result_cache_on = settings.RETRIEVAL_CACHE_ENABLED and settings.RETRIEVAL_CACHE_RESULTS_ENABLED
    if _result_cache_on:
        from app.services import retrieval_cache
        cached = retrieval_cache.get_cached_retrieval(
            query, document_types, document_id, top_k_per_store,
            use_intent, intent, table_filters)
        if cached is not None:
            return cached  # already per-object defensive copies

    try:
        query_embedding = _embed_query(query)
    except Exception as e:
        logger.error("Failed to embed query: %s", e)
        raise

    if not document_types and use_intent and intent is None:
        try:
            from app.services.intent_service import classify_intent
            intent = classify_intent(query, query_embedding=query_embedding)
        except Exception as e:
            logger.warning("Intent classification failed (%s) — searching all stores", e)

    flags = _select_stores(document_types, use_intent, intent)

    store_queries = {
        "vector": _query_vector_store,
        "clause": _query_clause_store,
        "research": _query_document_store,
        "table": _query_table_store,
    }

    # Fan out the per-store searches concurrently. They were previously run
    # sequentially on one connection, so each store's network round-trip to
    # Supabase (ap-south-1) added up serially. Each store now gets its own pooled
    # connection (pool maxconn=20, so ≤5 concurrent is safe) and they overlap —
    # total search latency drops to roughly the slowest single store.
    active = [(k, fn) for k, fn in store_queries.items() if flags.get(k)]

    results: list = []
    if active:
        with tracing.stage("vector-store-fanout", stores=",".join(k for k, _ in active)):
            # ThreadPoolExecutor.submit does NOT propagate contextvars into the
            # worker thread (unlike asyncio.to_thread) — capture the calling
            # context explicitly so each per-store span nests under this one.
            # A fresh copy_context() per task (not one shared ctx) is required:
            # Context.run() is not reentrant, so handing the SAME Context object
            # to concurrent submit() calls would raise "reentrant call" errors.
            with ThreadPoolExecutor(max_workers=len(active)) as ex:
                futures = [
                    ex.submit(
                        contextvars.copy_context().run, _run_store_query, k, fn,
                        query_embedding, document_types, document_id, top_k_per_store,
                        table_filters if k == "table" else None,
                    )
                    for k, fn in active
                ]
                for f in futures:
                    results.extend(f.result())

    results.sort(key=lambda c: c.distance)

    if _result_cache_on:
        from app.services import retrieval_cache
        retrieval_cache.put_cached_retrieval(
            query, document_types, document_id, top_k_per_store,
            use_intent, intent, table_filters, results)

    return results


def _run_store_query(store_key, query_fn, query_embedding, document_types, document_id, top_k,
                      table_filters: Optional["TableFilters"] = None):
    """Run a single store query on its OWN pooled connection so stores search in
    parallel. Per-store failures are isolated — one empty/broken store must not
    wipe out results from the others.

    table_filters is only meaningful for the 'table' store; other store query
    functions don't accept it so it's not forwarded to them.
    """
    try:
        with tracing.stage(f"store-query:{store_key}"):
            with get_db() as conn:
                if store_key == "table":
                    return query_fn(conn, query_embedding, document_types, document_id, top_k,
                                     table_filters=table_filters)
                return query_fn(conn, query_embedding, document_types, document_id, top_k)
    except Exception as exc:
        logger.warning("Store '%s' query failed (skipping): %s", store_key, exc)
        return []


def balanced_pool(results: list, per_store_cap: int = 8) -> list:
    """Take the closest `per_store_cap` chunks from EACH store_type so no store
    is starved by another store's (incomparable) raw cosine distances. Dedups
    by chunk_id; returns the union sorted by distance ascending.

    Chunks with `from_graph=True` are exempt from the cap — they were surfaced
    by entity-graph reasoning (a shared entity/relationship connects them to
    the query), which is an independent, often more precise signal than raw
    embedding similarity. Capping purely by cosine distance could silently drop
    a graph-identified chunk before the reranker ever sees it, defeating the
    reason the graph search ran in the first place.
    """
    by_store: dict[str, list] = {}
    for c in results:
        by_store.setdefault(c.store_type, []).append(c)

    pool: list = []
    seen: set = set()
    for chunks in by_store.values():
        graph_chunks = [c for c in chunks if getattr(c, "from_graph", False)]
        ann_chunks = [c for c in chunks if not getattr(c, "from_graph", False)]
        capped = sorted(ann_chunks, key=lambda x: x.distance)[:per_store_cap] + graph_chunks
        for c in sorted(capped, key=lambda x: x.distance):
            if c.chunk_id in seen:
                continue
            seen.add(c.chunk_id)
            pool.append(c)
    pool.sort(key=lambda c: c.distance)
    return pool


def rrf_merge(results: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion across stores: rank each store's chunks by distance,
    score = 1/(k+rank). Makes cross-store ordering fair by RANK, not raw distance.
    Sets relevance_score and returns sorted by it (descending)."""
    by_store: dict[str, list] = {}
    for c in results:
        by_store.setdefault(c.store_type, []).append(c)

    for chunks in by_store.values():
        for rank, c in enumerate(sorted(chunks, key=lambda x: x.distance), start=1):
            c.relevance_score = 1.0 / (k + rank)
    return sorted(results, key=lambda c: c.relevance_score, reverse=True)


def graph_expanded_chunks(
    query: str,
    primary_results: list,
    document_types: Optional[list] = None,
    max_related_docs: Optional[int] = None,
    per_doc_top_k: Optional[int] = None,
    max_hops: Optional[int] = None,
) -> list:
    """Multi-hop expansion to documents CONNECTED via shared entities.

    Each hop mines entities from the query (hop 1 only) AND from the text of
    chunks discovered so far (every hop) — so an entity that only appears in a
    retrieved document's body (e.g. Document A: "Drug X contains Penicillin")
    and never in the user's own question can still pull in a second document
    that mentions it (e.g. Document B: "Penicillin may cause allergic
    reactions"). Repeats until a hop finds no new entities/chunks or
    settings.MULTI_HOP_MAX_HOPS is reached.

    Uses the Neo4j entity graph (graph_service.related_documents) when
    available. When Neo4j is disabled/unreachable, falls back to searching the
    discovered entity names directly across all stores/documents via
    retrieve(), so cross-document connection still works without graph infra.

    Degradation-safe: returns [] when nothing is found or anything errors — the
    caller then behaves as if expansion never ran.
    """
    max_related_docs = settings.MULTI_HOP_MAX_RELATED_DOCS if max_related_docs is None else max_related_docs
    per_doc_top_k = settings.MULTI_HOP_PER_DOC_TOP_K if per_doc_top_k is None else per_doc_top_k
    max_hops = settings.MULTI_HOP_MAX_HOPS if max_hops is None else max_hops
    if max_hops <= 0:
        return []

    from app.services.entity_service import extract_entities, canonicalize
    from app.services import graph_service

    present_docs = {c.document_id for c in primary_results}
    seen_chunk_ids = {c.chunk_id for c in primary_results}
    seen_entity_keys: set = set()
    frontier = list(primary_results)
    extra: list = []

    def _mine_new_entities(texts: list) -> list:
        names: list = []
        for text in texts:
            if not text:
                continue
            for e in extract_entities(text, max_entities=8):
                key = canonicalize(e["name"])
                if key and key not in seen_entity_keys:
                    seen_entity_keys.add(key)
                    names.append(e["name"])
        return names

    try:
        graph_up = graph_service.is_available()
        for hop in range(max_hops):
            texts = [query] if hop == 0 else []
            texts += [c.text for c in frontier[:5]]
            entity_names = _mine_new_entities(texts)
            if not entity_names:
                break

            hop_chunks: list = []

            if graph_up:
                related = [
                    d for d in graph_service.related_documents(entity_names, limit=max_related_docs)
                    if d not in present_docs
                ]
                for doc_id in related:
                    # use_intent=False: we already know which doc to pull from.
                    doc_chunks = retrieve(
                        query,
                        document_types=document_types,
                        document_id=doc_id,
                        top_k_per_store=per_doc_top_k,
                        use_intent=False,
                    )
                    for c in doc_chunks:
                        if c.chunk_id not in seen_chunk_ids:
                            c.from_graph = True
                            seen_chunk_ids.add(c.chunk_id)
                            present_docs.add(c.document_id)
                            hop_chunks.append(c)
            else:
                # No graph — search directly on the discovered entity names so
                # cross-document connections still surface even though the
                # user's query never mentioned them.
                entity_query = " ".join(entity_names[:5])
                doc_chunks = retrieve(
                    entity_query,
                    document_types=document_types,
                    top_k_per_store=per_doc_top_k,
                    use_intent=False,
                )
                for c in doc_chunks:
                    if c.chunk_id not in seen_chunk_ids and c.document_id not in present_docs:
                        c.from_graph = True
                        seen_chunk_ids.add(c.chunk_id)
                        present_docs.add(c.document_id)
                        hop_chunks.append(c)

            if not hop_chunks:
                break

            extra.extend(hop_chunks)
            frontier = hop_chunks
            logger.info(
                "graph expansion hop %d/%d: %d entities → %d new chunk(s) (graph=%s)",
                hop + 1, max_hops, len(entity_names), len(hop_chunks), graph_up,
            )

        return extra
    except Exception as exc:
        logger.warning("graph expansion failed (non-fatal): %s", exc)
        return extra


# Neo4j Chunk.store holds the Postgres table name (graph_build_service writes
# "vector_store" | "clause_store" | "document_store"); RetrievedChunk.store_type
# uses the short keys. table_store is included defensively — the graph builder
# never writes it today, but a future build pass might.
_GRAPH_STORE_TO_STORE_TYPE = {
    "vector_store": "vector",
    "clause_store": "clause",
    "document_store": "research",
    "table_store": "table",
}


def graphrag_local_chunks(
    query: str,
    document_types: Optional[list] = None,
    document_id: Optional[str] = None,
    top_k: int = 10,
    hops: Optional[int] = None,
) -> list:
    """Hydrate GraphRAG local-search hits into full RetrievedChunk objects.

    graphrag_retriever.local_search() returns bare graph records
    ({pg_id, document_id, store, score}) — node identity only, no text. This
    function batch-fetches the actual store rows by primary key (per store,
    one WHERE id = ANY(...) query) so the results carry real chunk text and
    citation metadata, exactly like any other retrieval path.

    `hops` controls the entity-graph traversal depth; None means honor the
    configured GRAPHRAG_LOCAL_HOPS (multi-hop, cross-document).

    score → distance: local_neighborhood scores are discrete (1.0 for chunks
    mentioning a seed entity, 0.5 for 1-hop neighbours). distance = 1.0 - score
    is a rough analog of cosine distance whose only job is to survive
    balanced_pool's per-store cap; the cross-encoder reranker downstream does
    the real relevance ordering against ANN hits.

    Degradation-safe: returns [] on any failure (matching the best-effort
    try/except at the call sites in query.py).
    """
    try:
        from app.services import graphrag_retriever

        hits = graphrag_retriever.local_search(query, document_types, hops=hops)
        if not hits:
            return []

        # Group pg_ids by store, capped at top_k per store (hits arrive
        # best-score-first from local_neighborhood).
        by_store: dict[str, list[str]] = {}
        scores: dict[str, float] = {}  # pg_id → score
        for h in hits:
            store = h.get("store")
            pg_id = h.get("pg_id")
            if store not in _GRAPH_STORE_TO_STORE_TYPE or not pg_id:
                continue
            group = by_store.setdefault(store, [])
            if len(group) < top_k:
                group.append(pg_id)
                scores[pg_id] = float(h.get("score") or 0.5)

        if not by_store:
            return []

        chunks: list = []
        for store, pg_ids in by_store.items():
            try:
                with get_db() as conn:
                    rows = _fetch_chunks_by_ids(conn, store, pg_ids, document_types, document_id)
            except Exception as exc:
                logger.warning("graphrag hydrate for %s failed (skipping): %s", store, exc)
                continue
            for c in rows:
                c.distance = 1.0 - scores.get(c.chunk_id, 0.5)
                c.from_graph = True
                chunks.append(c)

        logger.info("graphrag local: %d graph hits → %d hydrated chunks", len(hits), len(chunks))
        return chunks
    except Exception as exc:
        logger.warning("graphrag_local_chunks failed (non-fatal): %s", exc)
        return []


def _fetch_chunks_by_ids(conn, store: str, pg_ids: list, document_types, document_id) -> list:
    """Batch-fetch rows by primary key from one store, mirroring the SELECT/JOIN
    column shape of the corresponding _query_*_store function so downstream
    consumers (context rendering, citations) see identical fields."""
    type_sql, type_params = _type_filter(document_types)
    store_alias = {"vector_store": "vs", "clause_store": "cs",
                   "document_store": "ds", "table_store": "ts"}[store]
    doc_sql, doc_params = _doc_filter(document_id, store_alias)

    if store == "vector_store":
        sql = f"""
            SELECT
                vs.id::text, vs.document_id::text, vs.chunk_text,
                vs.page_number, vs.section_title,
                dr.original_filename, dr.document_type,
                dr.storage_path, dr.storage_bucket, vs.bbox
            FROM multi_store_rag_working.vector_store vs
            JOIN multi_store_rag_working.document_registry dr ON dr.id = vs.document_id
            WHERE dr.status = 'completed' AND vs.id = ANY(%s::uuid[])
            {type_sql} {doc_sql}
        """
        with conn.cursor() as cur:
            cur.execute(sql, [pg_ids] + type_params + doc_params)
            rows = cur.fetchall()
        return [
            RetrievedChunk(
                chunk_id=r[0], document_id=r[1], text=r[2],
                page_number=r[3], section_title=r[4],
                distance=0.5,  # placeholder; caller overwrites from graph score
                document_filename=r[5], document_type=r[6],
                pdf_storage_path=r[7], pdf_bucket=r[8], bbox=r[9],
                store_type="vector",
            )
            for r in rows
        ]

    if store == "clause_store":
        sql = f"""
            SELECT
                cs.id::text, cs.document_id::text, cs.clause_text,
                cs.page_number, cs.clause_type, cs.risk_level,
                dr.original_filename, dr.document_type,
                dr.storage_path, dr.storage_bucket
            FROM multi_store_rag_working.clause_store cs
            JOIN multi_store_rag_working.document_registry dr ON dr.id = cs.document_id
            WHERE dr.status = 'completed' AND cs.id = ANY(%s::uuid[])
            {type_sql} {doc_sql}
        """
        with conn.cursor() as cur:
            cur.execute(sql, [pg_ids] + type_params + doc_params)
            rows = cur.fetchall()
        return [
            RetrievedChunk(
                chunk_id=r[0], document_id=r[1], text=r[2],
                page_number=r[3], clause_type=r[4], risk_level=r[5],
                distance=0.5,
                document_filename=r[6], document_type=r[7],
                pdf_storage_path=r[8], pdf_bucket=r[9],
                store_type="clause",
            )
            for r in rows
        ]

    if store == "document_store":
        sql = f"""
            SELECT
                ds.id::text, ds.document_id::text, ds.chunk_text,
                ds.page_number, ds.chunk_type, ds.section_title, ds.source_doi,
                dr.original_filename, dr.document_type,
                dr.storage_path, dr.storage_bucket
            FROM multi_store_rag_working.document_store ds
            JOIN multi_store_rag_working.document_registry dr ON dr.id = ds.document_id
            WHERE dr.status = 'completed' AND ds.id = ANY(%s::uuid[])
            {type_sql} {doc_sql}
        """
        with conn.cursor() as cur:
            cur.execute(sql, [pg_ids] + type_params + doc_params)
            rows = cur.fetchall()
        return [
            RetrievedChunk(
                chunk_id=r[0], document_id=r[1], text=r[2],
                page_number=r[3], chunk_type=r[4], section_title=r[5], source_doi=r[6],
                distance=0.5,
                document_filename=r[7], document_type=r[8],
                pdf_storage_path=r[9], pdf_bucket=r[10],
                store_type="research",
            )
            for r in rows
        ]

    if store == "table_store":
        sql = f"""
            SELECT
                ts.id::text, ts.document_id::text,
                COALESCE(ts.table_summary, ts.raw_text, '') AS text,
                ts.page_number, ts.markdown_text,
                dr.original_filename, dr.document_type,
                dr.storage_path, dr.storage_bucket, ts.bbox
            FROM multi_store_rag_working.table_store ts
            JOIN multi_store_rag_working.document_registry dr ON dr.id = ts.document_id
            WHERE dr.status = 'completed' AND ts.id = ANY(%s::uuid[])
            {type_sql} {doc_sql}
        """
        with conn.cursor() as cur:
            cur.execute(sql, [pg_ids] + type_params + doc_params)
            rows = cur.fetchall()
        return [
            RetrievedChunk(
                chunk_id=r[0], document_id=r[1], text=r[2],
                page_number=r[3], table_markdown=r[4],
                distance=0.5,
                document_filename=r[5], document_type=r[6],
                pdf_storage_path=r[7], pdf_bucket=r[8], bbox=r[9],
                store_type="table",
            )
            for r in rows
        ]

    return []


def _emb_str(embedding: np.ndarray) -> str:
    return "[" + ",".join(str(x) for x in embedding.tolist()) + "]"


def _type_filter(document_types: Optional[list], alias: str = "dr") -> tuple:
    if not document_types:
        return "", []
    placeholders = ", ".join(["%s"] * len(document_types))
    return f"AND {alias}.document_type IN ({placeholders})", list(document_types)


def _doc_filter(document_id: Optional[str], table_alias: str) -> tuple:
    if not document_id:
        return "", []
    return f"AND {table_alias}.document_id = %s", [document_id]


# ── Slice 4: hybrid metadata-filtered table retrieval ──────────────────────
# Optional structured PREFILTER on table_store columns (fiscal_year, currency,
# table_category, has_numeric_data, extraction_quality — populated by Slices
# 1-3 / migration 014) applied BEFORE the vector ANN. Purely additive: when no
# filters are supplied, _table_filter_sql() returns ("", []) and the query is
# byte-identical to pre-Slice-4 behavior.
_QUALITY_ORDER = ["low", "medium", "high"]


@dataclass
class TableFilters:
    """Optional structured prefilter for table_store search.

    All fields default to "no constraint" so `TableFilters()` (or None) is a
    no-op — identical SQL/results to not passing filters at all.
    """
    currency: Optional[str] = None
    fiscal_year: Optional[str] = None
    table_category: Optional[str] = None
    numeric_only: bool = False          # maps to has_numeric_data = TRUE
    min_quality: Optional[str] = None   # e.g. 'medium' -> extraction_quality IN ('medium','high')

    def is_empty(self) -> bool:
        return (
            not self.currency
            and not self.fiscal_year
            and not self.table_category
            and not self.numeric_only
            and not self.min_quality
        )


def _table_filter_sql(table_filters: Optional["TableFilters"], alias: str) -> tuple:
    """Build a parameterized SQL fragment (AND-joined) for optional table_store
    prefilter columns, plus its ordered param list. NEVER string-interpolates
    values — every predicate uses a %s placeholder.

    Fail-open: a malformed/unrecognized value (e.g. min_quality not in
    low/medium/high) is logged and skipped rather than raising, so a bad
    filter degrades to "no constraint on that field" instead of crashing
    the query.

    Returns ("", []) when table_filters is None/empty — no extra predicates,
    identical behavior to before Slice 4.
    """
    if not table_filters or table_filters.is_empty():
        return "", []

    clauses: list[str] = []
    params: list = []

    try:
        if table_filters.currency:
            clauses.append(f"{alias}.currency = %s")
            params.append(table_filters.currency)

        if table_filters.fiscal_year:
            clauses.append(f"{alias}.fiscal_year = %s")
            params.append(table_filters.fiscal_year)

        if table_filters.table_category:
            clauses.append(f"{alias}.table_category = %s")
            params.append(table_filters.table_category)

        if table_filters.numeric_only:
            clauses.append(f"{alias}.has_numeric_data = TRUE")

        if table_filters.min_quality:
            q = table_filters.min_quality.lower()
            if q in _QUALITY_ORDER:
                tiers = _QUALITY_ORDER[_QUALITY_ORDER.index(q):]
                placeholders = ", ".join(["%s"] * len(tiers))
                clauses.append(f"{alias}.extraction_quality IN ({placeholders})")
                params.extend(tiers)
            else:
                logger.warning(
                    "TableFilters.min_quality=%r not in %s — ignoring this predicate",
                    table_filters.min_quality, _QUALITY_ORDER,
                )
    except Exception as exc:
        # Fail-open: never let a malformed filter crash retrieval.
        logger.warning("Failed to build table filter SQL (ignoring filters): %s", exc)
        return "", []

    if not clauses:
        return "", []
    return "AND " + " AND ".join(clauses), params


def _query_vector_store(conn, embedding: np.ndarray, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "vs")
    emb = _emb_str(embedding)

    sql = f"""
        SELECT
            vs.id::text, vs.document_id::text, vs.chunk_text,
            vs.page_number, vs.section_title,
            (vs.embedding <=> %s::vector) AS distance,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket, vs.bbox
        FROM multi_store_rag_working.vector_store vs
        JOIN multi_store_rag_working.document_registry dr ON dr.id = vs.document_id
        WHERE dr.status = 'completed'
        {type_sql} {doc_sql}
        ORDER BY vs.embedding <=> %s::vector
        LIMIT %s
    """
    params = [emb] + type_params + doc_params + [emb, top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], section_title=r[4],
            distance=float(r[5]),
            document_filename=r[6], document_type=r[7],
            pdf_storage_path=r[8], pdf_bucket=r[9], bbox=r[10],
            store_type="vector",
        )
        for r in rows
    ]


def _query_clause_store(conn, embedding: np.ndarray, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "cs")
    emb = _emb_str(embedding)

    sql = f"""
        SELECT
            cs.id::text, cs.document_id::text, cs.clause_text,
            cs.page_number, cs.clause_type, cs.risk_level,
            (cs.embedding <=> %s::vector) AS distance,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket
        FROM multi_store_rag_working.clause_store cs
        JOIN multi_store_rag_working.document_registry dr ON dr.id = cs.document_id
        WHERE dr.status = 'completed'
        {type_sql} {doc_sql}
        ORDER BY cs.embedding <=> %s::vector
        LIMIT %s
    """
    params = [emb] + type_params + doc_params + [emb, top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], clause_type=r[4], risk_level=r[5],
            distance=float(r[6]),
            document_filename=r[7], document_type=r[8],
            pdf_storage_path=r[9], pdf_bucket=r[10],
            store_type="clause",
        )
        for r in rows
    ]


def _query_document_store(conn, embedding: np.ndarray, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "ds")
    emb = _emb_str(embedding)

    sql = f"""
        SELECT
            ds.id::text, ds.document_id::text, ds.chunk_text,
            ds.page_number, ds.chunk_type, ds.section_title, ds.source_doi,
            (ds.embedding <=> %s::vector) AS distance,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket
        FROM multi_store_rag_working.document_store ds
        JOIN multi_store_rag_working.document_registry dr ON dr.id = ds.document_id
        WHERE dr.status = 'completed'
        {type_sql} {doc_sql}
        ORDER BY ds.embedding <=> %s::vector
        LIMIT %s
    """
    params = [emb] + type_params + doc_params + [emb, top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], chunk_type=r[4], section_title=r[5], source_doi=r[6],
            distance=float(r[7]),
            document_filename=r[8], document_type=r[9],
            pdf_storage_path=r[10], pdf_bucket=r[11],
            store_type="research",
        )
        for r in rows
    ]


def _resolve_chunk_page_range(page_number, chunk_metadata) -> tuple:
    """Derive (page_number, page_number_end) for a table_chunk_store window.

    Phase 1 (document_parser._merge_continued_tables + table_chunker.
    build_row_windows) stamps chunk_metadata['page_start']/['page_end'] on
    windows of continuation-merged tables that span more than one source page.
    Ordinary single-page chunks have no such keys (or page_start == page_end),
    so this is a no-op for them: page_number is returned unchanged and
    page_number_end is None.

    `chunk_metadata` may already be a dict (psycopg2 can auto-adapt JSONB) or a
    JSON string, or None/missing — handled defensively so a malformed value
    degrades to "no range info" rather than raising.
    """
    if not chunk_metadata:
        return page_number, None

    meta = chunk_metadata
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except Exception:
            return page_number, None

    if not isinstance(meta, dict):
        return page_number, None

    page_start = meta.get("page_start")
    page_end = meta.get("page_end")
    if page_start is None or page_end is None:
        return page_number, None

    resolved_start = page_start if page_number is None else page_number
    page_number_end = page_end if page_end != page_start else None
    return resolved_start, page_number_end


def _query_table_store(conn, embedding: np.ndarray, document_types, document_id, top_k: int,
                        table_filters: Optional["TableFilters"] = None) -> list:
    """Query table content directly via table_store."""
    return _query_table_store_parent_only(conn, embedding, document_types, document_id, top_k,
                                           table_filters=table_filters)

    # ── Child ANN search (table_chunk_store JOIN table_store) ─────────────────
    type_sql_ts, type_params_ts = _type_filter(document_types, alias="dr")
    doc_sql_tcs, doc_params_tcs = _doc_filter(document_id, "tcs")
    filter_sql_ts, filter_params_ts = _table_filter_sql(table_filters, alias="ts")
    emb = _emb_str(embedding)

    # Large tables (migration 018) carry a per-window structured_content JSON slice
    # + its own embedding; prefer both via COALESCE so a big table's windows search
    # and surface the structured view, while every existing chunk falls back to
    # serialized_text / embedding unchanged (no regression).
    child_sql = f"""
        SELECT
            tcs.id::text,
            tcs.document_id::text,
            COALESCE(tcs.structured_content, tcs.serialized_text),
            COALESCE(ts.page_number, tcs.page_number) AS page_number,
            ts.markdown_text,
            (COALESCE(tcs.structured_content_embedding, tcs.embedding) <=> %s::vector) AS distance,
            dr.original_filename,
            dr.document_type,
            dr.storage_path,
            dr.storage_bucket,
            ts.bbox,
            ts.id::text AS table_id,
            tcs.chunk_metadata
        FROM multi_store_rag_working.table_chunk_store tcs
        JOIN multi_store_rag_working.table_store ts
             ON ts.id = tcs.table_id
        JOIN multi_store_rag_working.document_registry dr
             ON dr.id = tcs.document_id
        WHERE dr.status = 'completed'
          AND tcs.embedding IS NOT NULL
        {type_sql_ts} {doc_sql_tcs} {filter_sql_ts}
        ORDER BY COALESCE(tcs.structured_content_embedding, tcs.embedding) <=> %s::vector
        LIMIT %s
    """
    child_params = [emb] + type_params_ts + doc_params_tcs + filter_params_ts + [emb, top_k]

    with conn.cursor() as cur:
        cur.execute(child_sql, child_params)
        child_rows = cur.fetchall()

    # Build child results, then dedup by table_id keeping the top-K best
    # (lowest) distance windows per table, instead of only the single best.
    max_windows_per_table = max(1, _cfg.TABLE_MAX_WINDOWS_PER_QUERY_RESULT)
    windows_by_table_id: dict[str, list[RetrievedChunk]] = {}  # table_id → windows seen so far (unsorted)
    seen_table_ids: set = set()

    for r in child_rows:
        page_number, page_number_end = _resolve_chunk_page_range(r[3], r[12])
        chunk = RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=page_number, table_markdown=r[4],
            distance=float(r[5]),
            document_filename=r[6], document_type=r[7],
            pdf_storage_path=r[8], pdf_bucket=r[9], bbox=r[10],
            store_type="table",
            page_number_end=page_number_end,
            is_child_match=True,
        )
        table_id = r[11]
        seen_table_ids.add(table_id)
        windows_by_table_id.setdefault(table_id, []).append(chunk)

    child_chunks: list[RetrievedChunk] = []
    for table_id, windows in windows_by_table_id.items():
        windows.sort(key=lambda c: c.distance)
        child_chunks.extend(windows[:max_windows_per_table])

    # ── Fallback parent ANN for tables with 0 children ────────────────────────
    # These are tables inserted via image-crop OCR or tiny tables that produced
    # no row windows (0 data rows).  We identify them by querying parent table_store
    # rows that do NOT appear in table_chunk_store for this document (or globally
    # for multi-doc queries).  The simplest efficient approach: run a separate
    # parent-only ANN limited to top_k, then exclude table_ids already seen above.
    # Same table_filters applied here too, so a filtered-out table never sneaks
    # back in via the fallback path.
    parent_results = _query_table_store_parent_only(
        conn, embedding, document_types, document_id, top_k,
        table_filters=table_filters,
    )
    # Only keep parents whose table_id is NOT already covered by a child window
    for p in parent_results:
        if p.chunk_id not in seen_table_ids:
            child_chunks.append(p)

    child_chunks.sort(key=lambda c: c.distance)
    return child_chunks[:top_k]


def _query_table_store_parent_only(
    conn, embedding: np.ndarray, document_types, document_id, top_k: int,
    table_filters: Optional["TableFilters"] = None,
) -> list:
    """Original parent-summary ANN search on table_store.embedding.

    Used as:
    - The primary path when TABLE_CHILD_SEARCH_ENABLED=False.
    - The fallback branch inside _query_table_store() for tables with 0 children.

    table_filters (Slice 4, optional): same structured prefilter as
    _query_table_store, applied to `ts` here directly. None/empty => no extra
    predicates, identical SQL to before Slice 4.
    """
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "ts")
    filter_sql, filter_params = _table_filter_sql(table_filters, alias="ts")
    emb = _emb_str(embedding)

    # Universal VLM pipeline: prefer the structured_content embedding (the VLM's
    # clean, retrieval-ready extraction) when present, falling back to the legacy
    # table_summary-based embedding so pre-migration rows still match. Both are
    # 1024-dim BGE vectors in the same space, so a single query embedding compares
    # against either via COALESCE.
    vexpr = "COALESCE(ts.structured_content_embedding, ts.embedding)"
    sql = f"""
        SELECT
            ts.id::text, ts.document_id::text,
            COALESCE(ts.structured_content, ts.table_summary, ts.raw_text, '') AS text,
            ts.page_number, ts.markdown_text,
            ({vexpr} <=> %s::vector) AS distance,
            dr.original_filename, dr.document_type,
            dr.storage_path, dr.storage_bucket, ts.bbox
        FROM multi_store_rag_working.table_store ts
        JOIN multi_store_rag_working.document_registry dr ON dr.id = ts.document_id
        WHERE dr.status = 'completed' AND {vexpr} IS NOT NULL
        {type_sql} {doc_sql} {filter_sql}
        ORDER BY {vexpr} <=> %s::vector
        LIMIT %s
    """
    params = [emb] + type_params + doc_params + filter_params + [emb, top_k]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        RetrievedChunk(
            chunk_id=r[0], document_id=r[1], text=r[2],
            page_number=r[3], table_markdown=r[4],
            distance=float(r[5]),
            document_filename=r[6], document_type=r[7],
            pdf_storage_path=r[8], pdf_bucket=r[9], bbox=r[10],
            store_type="table",
        )
        for r in rows
    ]


# NOTE: image_store is no longer a searchable store. It has no embedding column
# (migration 008) and is a pure extraction repository. The former
# _query_image_store / _rows_to_image_chunks were removed: image-derived content
# that should be searchable is cross-stored into vector/table/clause/document by
# store_image_derived_chunks and retrieved from those stores.
