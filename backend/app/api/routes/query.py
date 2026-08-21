import asyncio
import json as _json
import time
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core import tracing
from app.services import retriever_service, reranker_service, synthesis_service
from app.services import hybrid_search_service


def _classic_retrieve_fn():
    """Retrieval entry point for the classic path. HYBRID_IN_CLASSIC_PATH=True
    swaps in hybrid semantic+keyword retrieval (same signature, same
    list[RetrievedChunk] shape) without the RAVEN/SPYDER agentic loop."""
    if settings.HYBRID_IN_CLASSIC_PATH:
        return hybrid_search_service.hybrid_retrieve
    return retriever_service.retrieve
from app.services.synthesis_service import retrieval_confidence as _retrieval_confidence
from app.services.synthesis_service import retrieval_confidence_breakdown as _retrieval_confidence_breakdown
from app.services.supabase_storage import create_signed_url

_blended_confidence_for_stream = synthesis_service.blended_confidence_for_stream

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_DOC_TYPES = {"policy", "financial", "legal", "entity", "research"}


class TableFilterRequest(BaseModel):
    """Optional structured prefilter for table_store search (Slice 4 â€” hybrid
    metadata-filtered table retrieval). All fields optional; omitting the whole
    object (or leaving every field None/False) is a no-op â€” identical
    retrieval behavior to before this model existed."""
    currency: Optional[str] = None
    fiscal_year: Optional[str] = None
    table_category: Optional[str] = None
    numeric_only: bool = False
    min_quality: Optional[str] = None


def _to_table_filters(req_filters: Optional[TableFilterRequest]) -> Optional[retriever_service.TableFilters]:
    """Convert the API-layer TableFilterRequest into the service-layer
    TableFilters dataclass. None in => None out (no-op, unchanged behavior)."""
    if req_filters is None:
        return None
    return retriever_service.TableFilters(
        currency=req_filters.currency,
        fiscal_year=req_filters.fiscal_year,
        table_category=req_filters.table_category,
        numeric_only=req_filters.numeric_only,
        min_quality=req_filters.min_quality,
    )


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    document_types: Optional[list] = None
    document_id: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=20)
    use_reranker: bool = True
    # Slice 4 (optional, additive): structured table_store prefilter. Consumed
    # by both the classic path (retriever_service.retrieve) and the agentic path
    # (agentic_pipeline.run) â€” threaded through both call sites below.
    table_filters: Optional[TableFilterRequest] = None
    enable_hybrid: Optional[bool] = None
    enable_graphrag: Optional[bool] = None


class CitationItem(BaseModel):
    document_id: str
    filename: str
    chunk_text: str
    store_type: str
    relevance_score: float
    page_number: Optional[int] = None
    # Multi-page continuation tables (Phase 1 merge + hybrid-retrieval Phase 2
    # threading): the last page a table row-window spans, when different from
    # page_number (the start page). None for every non-table citation and for
    # ordinary single-page table windows â€” backward-compatible default.
    page_number_end: Optional[int] = None
    section_title: Optional[str] = None
    clause_type: Optional[str] = None
    risk_level: Optional[str] = None
    chunk_type: Optional[str] = None
    source_doi: Optional[str] = None
    table_markdown: Optional[str] = None
    image_url: Optional[str] = None
    caption: Optional[str] = None
    ocr_text: Optional[str] = None
    pdf_url: Optional[str] = None
    bbox: Optional[dict] = None


RERANK_PER_STORE_CAP = 8


def _rank_chunks(query: str, retrieved: list, top_k: int, use_reranker: bool,
                  per_store_cap: int = RERANK_PER_STORE_CAP):
    """Build a balanced per-store pool, then rank it. Cross-encoder reranks the
    whole pool (fair cross-store fusion); RRF is used when the reranker is off or
    errors. Returns (final_chunks, pool_size)."""
    pool = retriever_service.balanced_pool(retrieved, per_store_cap=per_store_cap)
    if use_reranker:
        try:
            return reranker_service.rerank(query, pool, top_k=top_k), len(pool)
        except Exception as e:
            logger.warning("Reranker error, falling back to RRF: %s", e)
    return retriever_service.rrf_merge(pool)[:top_k], len(pool)


# â”€â”€ Adaptive retrieval planning â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Cheap string heuristic (no extra embedding/LLM call) that scales retrieval
# depth to query complexity: a short, single-fact question doesn't need the
# same top_k_per_store as "compare X and Y across all documents".
_COMPLEXITY_CUES = {
    "compare", "comparison", "difference between", "versus", " vs ",
    "all of", "every", "list all", "summarize across", "across all",
}


def _is_complex_query(query: str) -> bool:
    q = query.lower()
    if len(query.split()) >= settings.ADAPTIVE_COMPLEXITY_WORD_THRESHOLD:
        return True
    return any(cue in q for cue in _COMPLEXITY_CUES)


def _resolve_retrieval_params(query: str) -> tuple[int, int]:
    """Returns (top_k_per_store, rerank_per_store_cap) for this query. When
    ADAPTIVE_RETRIEVAL_ENABLED=False, returns the old hardcoded constants
    (10, RERANK_PER_STORE_CAP) unchanged."""
    if not settings.ADAPTIVE_RETRIEVAL_ENABLED:
        return 10, RERANK_PER_STORE_CAP
    if _is_complex_query(query):
        return settings.ADAPTIVE_TOP_K_COMPLEX, settings.ADAPTIVE_RERANK_CAP_COMPLEX
    return settings.ADAPTIVE_TOP_K_SIMPLE, settings.ADAPTIVE_RERANK_CAP_SIMPLE


async def _resolve_graph_mode(query: str, document_id: Optional[str]) -> str:
    """GraphRAG availability+routing decision, independent of retrieval â€” has
    no data dependency on `retrieved`, so callers run this concurrently with
    retrieval via asyncio.gather() instead of awaiting it afterward."""
    if not settings.GRAPHRAG_ENABLED or document_id:
        return "none"

    def _decide():
        from app.services import graph_service as _gs
        if not _gs.is_available():
            return "none"
        from app.services.graphrag_retriever import route_graphrag
        return route_graphrag(query)

    try:
        return await asyncio.to_thread(_decide)
    except Exception as exc:
        logger.warning("GraphRAG routing failed (non-fatal): %s", exc)
        return "none"


def _citation_from_chunk(c, bucket: str) -> "CitationItem":
    image_url = None
    if getattr(c, "image_storage_path", None):
        try:
            image_url = create_signed_url(bucket, c.image_storage_path)
        except Exception as exc:
            logger.warning("Signed URL mint failed for %s: %s", c.image_storage_path, exc)

    pdf_url = None
    pdf_path = getattr(c, "pdf_storage_path", None)
    if pdf_path:
        pdf_bucket = getattr(c, "pdf_bucket", None) or bucket
        try:
            pdf_url = create_signed_url(pdf_bucket, pdf_path)
            # Keep the page fragment on the signed URL so PDF viewers open at
            # the cited page (page 1 is valid too; a truthiness check would
            # silently drop page 0 from legacy records).
            if c.page_number is not None:
                pdf_url = f"{pdf_url}#page={c.page_number}"
        except Exception as exc:
            logger.warning("PDF signed URL mint failed for %s: %s", pdf_path, exc)

    return CitationItem(
        document_id=c.document_id,
        filename=c.document_filename,
        chunk_text=c.text,
        store_type=c.store_type,
        relevance_score=round(c.relevance_score, 4),
        page_number=c.page_number,
        page_number_end=getattr(c, "page_number_end", None),
        section_title=c.section_title,
        clause_type=c.clause_type,
        risk_level=c.risk_level,
        chunk_type=c.chunk_type,
        source_doi=c.source_doi,
        table_markdown=c.table_markdown,
        image_url=image_url,
        caption=getattr(c, "caption", None),
        ocr_text=getattr(c, "ocr_text", None),
        pdf_url=pdf_url,
        bbox=getattr(c, "bbox", None),
    )


def _chunk_preview_for_stream(c) -> dict:
    """Compact evidence preview for the live Chat UI pipeline inspector."""
    return {
        "chunk_id": getattr(c, "chunk_id", None),
        "document_filename": getattr(c, "document_filename", None) or "Document",
        "store_type": getattr(c, "store_type", None) or "document",
        "relevance_score": round(float(getattr(c, "relevance_score", 0.0)), 4),
        "text": str(getattr(c, "text", ""))[:1200],
    }


class QueryResponse(BaseModel):
    answer: str
    confidence: float
    # Additive: breaks "confidence" down into its weighted components (retrieval
    # signal vs. Groq's self-rating, or just retrieval when no Groq rating
    # exists â€” see synthesis_service.retrieval_confidence_breakdown /
    # _blended_breakdown / blended_confidence_for_stream). None only if
    # synthesis raised before a breakdown could be built.
    confidence_breakdown: Optional[dict] = None
    citations: list[CitationItem]
    retrieval_stats: dict
    query: str
    processing_time_seconds: float
    notes: Optional[str] = None
    agentic_stats: Optional[dict] = None
    # Phase 2 (additive, optional): exact structured table query result
    # (SUM/AVG/COUNT/MIN/MAX or exact row/column lookup), populated only when
    # STRUCTURED_QUERY_ENABLED and the query was recognized as such by
    # table_query_engine.try_structured_query(). None/absent for every
    # ordinary semantic query â€” no behavior change otherwise.
    structured_result: Optional[dict] = None
    # Additive: per-stage latency breakdown (routing/vector/keyword/RRF/
    # rerank/synthesis, whichever ran) populated from app.core.tracing â€”
    # None only for the early conversational short-circuit, where no
    # pipeline stages ran.
    timings: Optional[dict] = None


def _try_structured_query(req: "QueryRequest") -> Optional[dict]:
    """Gate + call table_query_engine.try_structured_query(). Returns None
    (and never raises) when the feature is disabled or nothing is recognized,
    so callers can treat this as a pure additive best-effort step."""
    if not settings.STRUCTURED_QUERY_ENABLED:
        return None
    try:
        from app.services.table_query_engine import try_structured_query
        return try_structured_query(
            req.query,
            document_id=req.document_id,
            document_types=req.document_types,
        )
    except Exception as exc:
        logger.warning("Structured query engine failed (non-fatal): %s", exc)
        return None


def _format_structured_fact(structured: dict) -> str:
    """Render the structured result as an authoritative computed-fact prefix
    for the synthesis prompt, so Groq can cite it directly instead of
    estimating from retrieved text."""
    op = structured.get("operation")
    column = structured.get("column")
    value = structured.get("value")
    filter_desc = structured.get("filter_description") or ""
    row_count = structured.get("row_count_considered")

    if op == "LOOKUP":
        summary = f"{column} = {value} ({filter_desc})"
    elif op in ("LIST", "FILTER", "RANKING"):
        # value is list[dict] of matched rows â€” enumerate them ALL explicitly
        # so synthesis can't silently drop any (unlike relying on whatever
        # chunks a top-k semantic retrieval happened to surface). FILTER/
        # RANKING (tier-1 SQL pushdown) additionally carry shown_row_count/
        # truncated when the match set exceeded TABLE_STRUCTURED_MAX_ROWS_
        # INJECTED â€” that gets surfaced as an explicit note rather than
        # silently dropping the excess.
        rows = value if isinstance(value, list) else []
        matched_count = structured.get("matched_row_count", len(rows))
        shown_count = structured.get("shown_row_count", len(rows))
        truncated = structured.get("truncated", False)
        lines = [
            "; ".join(f"{k}: {v}" for k, v in row.items())
            for row in rows
            if isinstance(row, dict)
        ]
        label = "ranked result" if op == "RANKING" else "row(s) match"
        completeness = (
            f"This is the COMPLETE and EXACT list â€” do not omit any entry or add others:"
            if not truncated else
            f"Showing the first {shown_count} of {matched_count} total matches (truncated to fit "
            f"the response budget â€” tell the user only a subset is shown and they should narrow "
            f"the query, e.g. by document or a tighter filter, to see the rest):"
        )
        summary = (
            f"{matched_count} {label} ({filter_desc}). {completeness}\n"
            + "\n".join(f"- {line}" for line in lines)
        )
    elif op == "GROUP_BY":
        groups = value if isinstance(value, list) else []
        lines = [f"- {g.get('group')}: {g.get('value')}" for g in groups if isinstance(g, dict)]
        summary = (
            f"{column} ({filter_desc}), {len(groups)} group(s) â€” exact computed values, "
            f"report every group:\n" + "\n".join(lines)
        )
    else:
        summary = f"{op}({column}) = {value} across {row_count} rows ({filter_desc})"

    return f"Exact computed result (structured query): {summary}"


def _synthetic_chunks_from_structured(structured: dict) -> list:
    """Wrap a structured-query result as synthetic RetrievedChunk(s) so the
    EXISTING, well-tested synthesize()/synthesize_stream() machinery
    (context assembly, confidence scoring, citation building) can be reused
    unchanged for the table-query shortcut, instead of writing a parallel
    synthesis path. The actual exact answer content lives in the
    synthesis_query text (via _format_structured_fact), not in these
    chunks' `text` â€” these exist purely so `chunks` is non-empty (bypassing
    synthesize()'s "no relevant documents" empty-chunks branch) and so
    citations point back at the real source table(s).

    relevance_score=1.0 / distance=0.0: this is a deterministic, exact
    computed result, not a similarity estimate â€” it should read as maximally
    confident, not hedge like an ANN match would.
    """
    from app.services.retriever_service import RetrievedChunk

    table_ids = structured.get("matched_table_ids") or [None]
    document_id = structured.get("document_id") or ""
    filename = structured.get("filename") or "Source table"
    op = structured.get("operation") or "structured"

    return [
        RetrievedChunk(
            chunk_id=str(tid) if tid else "structured-result",
            document_id=str(document_id) if document_id else "",
            text=f"Exact {op} result computed directly from this table's data.",
            store_type="table",
            distance=0.0,
            relevance_score=1.0,
            document_filename=filename,
            document_type="financial",
            table_markdown=structured.get("table_title"),
            pdf_storage_path=structured.get("pdf_storage_path"),
            pdf_bucket=structured.get("pdf_bucket"),
            page_number=structured.get("page_number"),
        )
        for tid in table_ids
    ]


async def _try_table_query_shortcut(req: "QueryRequest") -> Optional[dict]:
    """Classify + short-circuit for filter/aggregation/ranking/lookup/mixed
    table queries. These have a deterministic, exhaustive answer from the
    Structured Query Engine (table_query_engine.try_structured_query, tiered
    SQL-pushdown-then-Python-JSONB) and should never depend on ANN top_k
    truncation, nor burn RAVEN/hybrid-loop/SPYDER time on a query shape that
    engine can't answer exhaustively in the first place â€” e.g. "list all
    companies in the Chemicals sector" needs every matching row, not "the
    top_k=3 most semantically similar chunks".

    Returns None (falls through to the existing agentic/classic pipeline,
    completely unchanged) when the query isn't table-shaped, or the
    structured engine found nothing at all â€” the fallback chain is always
    SQL pushdown -> Python/JSONB scan -> semantic retrieval, never a
    silent empty answer.

    On a hit, returns {"structured_result", "synthesis_query", "chunks"}
    ready for synthesis_service.synthesize()/synthesize_stream().
    """
    if not settings.STRUCTURED_QUERY_ENABLED:
        return None
    try:
        from app.services.table_intent_classifier import classify_table_intent
        intent = classify_table_intent(req.query)
        if intent == "semantic_qa":
            return None

        with tracing.stage("structured-shortcut", intent=intent):
            structured = await asyncio.to_thread(_try_structured_query, req)
        if structured is None:
            return None

        return {
            "structured_result": structured,
            "synthesis_query": f"{_format_structured_fact(structured)}\n\n{req.query}",
            "chunks": _synthetic_chunks_from_structured(structured),
        }
    except Exception as exc:
        logger.warning("Table query shortcut failed (falling back to normal pipeline): %s", exc)
        return None


_CONVERSATIONAL_PATTERNS = {
    "hi", "hello", "hey", "hiya", "howdy",
    "thanks", "thank you", "thank you!", "thanks!", "ty",
    "ok", "okay", "ok!", "okay!", "cool", "got it", "alright",
    "bye", "goodbye", "see you", "good morning", "good afternoon", "good evening",
    "how are you", "how are you?", "what can you do", "what can you do?",
    "who are you", "who are you?", "help", "help me",
}


def _is_conversational(query: str) -> bool:
    """Return True if the query is a greeting, small talk, or otherwise unrelated
    to document retrieval. Checked before any retrieval to avoid injecting document
    context into conversational responses."""
    q = query.strip().lower().rstrip("!?.,")
    if q in _CONVERSATIONAL_PATTERNS:
        return True
    # Very short queries (<= 3 words) with no domain-specific words
    words = q.split()
    if len(words) <= 2 and not any(c.isdigit() for c in q):
        # Allow through if it contains typical question starters that suggest doc intent
        doc_starters = {"what", "which", "when", "where", "who", "how", "why", "list", "show", "find", "summarize", "explain"}
        if not any(w in doc_starters for w in words):
            return True
    return False


@router.post("/query", response_model=QueryResponse)
async def query_documents(req: QueryRequest):
    """Async handler â€” blocking DB/rerank work runs in a thread pool so the
    event loop stays free to accept new connections from other users. The Groq
    call uses an AsyncClient + semaphore so concurrent requests queue gracefully
    instead of exhausting threads or flooding the CDAC endpoint.

    When AGENTIC_RAG_ENABLED=True the retrieveâ†’graph_expandâ†’_rank_chunks block
    is replaced by agentic_pipeline.run() which runs RAVEN + bounded hybrid loop
    + SPYDER. The synthesis and response shape are unchanged."""
    start = time.time()
    tracing.reset_timings()

    # Short-circuit: conversational / off-topic queries must not trigger retrieval.
    if _is_conversational(req.query):
        conv_answer = await synthesis_service.synthesize_conversational(req.query)
        return QueryResponse(
            answer=conv_answer,
            confidence=1.0,
            citations=[],
            retrieval_stats={"total_retrieved": 0, "after_reranking": 0, "stores_searched": []},
            query=req.query,
            processing_time_seconds=round(time.time() - start, 3),
        )

    if req.document_types:
        invalid = set(req.document_types) - _VALID_DOC_TYPES
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid document_types: {sorted(invalid)}")

    # â”€â”€ Table query shortcut: filter/aggregation/ranking/lookup/mixed â”€â”€â”€â”€â”€â”€â”€â”€
    # Bypasses RAVEN/hybrid-loop/SPYDER (or classic retrieve+rerank) entirely
    # for query shapes the Structured Query Engine already answers exactly
    # and exhaustively â€” see _try_table_query_shortcut's docstring.
    shortcut = await _try_table_query_shortcut(req)
    if shortcut is not None:
        with tracing.stage("synthesize"):
            synthesis = await synthesis_service.synthesize(shortcut["synthesis_query"], shortcut["chunks"])
        bucket = settings.SUPABASE_STORAGE_BUCKET
        shortcut_citations = [_citation_from_chunk(c, bucket) for c in shortcut["chunks"]]
        return QueryResponse(
            answer=synthesis["answer"],
            confidence=synthesis["confidence"],
            confidence_breakdown=synthesis.get("confidence_breakdown"),
            citations=shortcut_citations,
            retrieval_stats={
                "total_retrieved": len(shortcut["chunks"]),
                "after_reranking": len(shortcut["chunks"]),
                "stores_searched": list({c.store_type for c in shortcut["chunks"]}),
            },
            query=req.query,
            processing_time_seconds=round(time.time() - start, 2),
            notes=synthesis.get("notes"),
            structured_result=shortcut["structured_result"],
            timings=tracing.get_timings(),
        )

    agentic_stats: Optional[dict] = None

    if settings.AGENTIC_RAG_ENABLED:
        # â”€â”€ Agentic path: RAVEN â†’ hybrid loop â†’ SPYDER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        from app.services.agents import agentic_pipeline
        try:
            _t = time.time()
            final_chunks, agentic_stats = await agentic_pipeline.run(
                query=req.query,
                document_types=req.document_types,
                document_id=req.document_id,
                top_k=req.top_k,
                use_reranker=req.use_reranker,
                table_filters=_to_table_filters(req.table_filters),
                enable_hybrid=req.enable_hybrid,
                enable_graphrag=req.enable_graphrag,
            )
            logger.info(
                "STAGE agentic: %.2fs (loops=%d chunks=%d)",
                time.time() - _t,
                agentic_stats.get("loops", 0),
                len(final_chunks),
            )
        except Exception as e:
            logger.exception("Agentic pipeline failed")
            raise HTTPException(status_code=500, detail=f"Agentic pipeline failed: {e}")

        # agentic_pipeline.run() short-circuits to ([], {"graph_mode": "global",
        # "global_answer": {...}}) when route_graphrag picked global community
        # search â€” mirror the classic path's early-return here, otherwise this
        # falls through to "No relevant documents found" below and the real
        # graph-synthesized answer is silently discarded.
        if agentic_stats.get("graph_mode") == "global":
            _global_answer = agentic_stats.get("global_answer") or {}
            if _global_answer.get("answer"):
                return QueryResponse(
                    answer=_global_answer["answer"],
                    confidence=0.6,
                    citations=[],
                    retrieval_stats={
                        "total_retrieved": 0,
                        "pool_size": 0,
                        "after_reranking": 0,
                        "stores_searched": ["graph_communities"],
                        "graph_mode": "global",
                        "used_communities": _global_answer.get("used_communities", []),
                    },
                    query=req.query,
                    processing_time_seconds=round(time.time() - start, 2),
                    notes="Answer synthesized from knowledge graph community summaries.",
                    agentic_stats=agentic_stats,
                    timings=tracing.get_timings(),
                )

        if not final_chunks:
            return QueryResponse(
                answer=(
                    "No relevant documents found. "
                    "Please upload relevant documents or try a broader query."
                ),
                confidence=0.0,
                citations=[],
                retrieval_stats={"total_retrieved": 0, "after_reranking": 0, "stores_searched": []},
                query=req.query,
                processing_time_seconds=round(time.time() - start, 2),
                agentic_stats=agentic_stats,
                timings=tracing.get_timings(),
            )

        stores_searched = list({c.store_type for c in final_chunks})
        pool_size = len(final_chunks)
        total_retrieved = len(final_chunks)

    else:
        # â”€â”€ Classic path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Retrieval and GraphRAG availability+routing have no data dependency on
        # each other (the only real dependency â€” graphrag_local_chunks dedup â€”
        # needs `retrieved` AND `graph_mode`, so it stays sequential below, after
        # both legs here have completed) â€” run them concurrently.
        top_k_per_store, rerank_cap = _resolve_retrieval_params(req.query)
        try:
            async def _retrieve():
                with tracing.stage("retrieve", top_k_per_store=top_k_per_store) as _span:
                    result = await asyncio.to_thread(
                        _classic_retrieve_fn(),
                        query=req.query,
                        document_types=req.document_types,
                        document_id=req.document_id,
                        top_k_per_store=top_k_per_store,
                        table_filters=_to_table_filters(req.table_filters),
                    )
                    _span.set_attribute("chunks", len(result))
                    return result

            async def _route():
                with tracing.stage("graphrag-route"):
                    return await _resolve_graph_mode(req.query, req.document_id)

            retrieved, graph_mode = await asyncio.gather(_retrieve(), _route())
        except OSError as e:
            # Windows error 1455 = page file too small to create a new thread.
            # Threads should have been pre-created at startup so this should not
            # happen, but guard against it so the user gets a retriable 503.
            if getattr(e, "winerror", None) == 1455 or "paging file" in str(e).lower():
                logger.error("Windows page-file exhaustion during retrieval: %s", e)
                raise HTTPException(
                    status_code=503,
                    detail="Server is under memory pressure â€” please retry in a moment.",
                )
            logger.exception("Retrieval failed (OSError)")
            raise HTTPException(status_code=500, detail=f"Retrieval failed: {e}")
        except Exception as e:
            logger.exception("Retrieval failed")
            raise HTTPException(status_code=500, detail=f"Retrieval failed: {e}")

        global_answer: Optional[dict] = None
        graph_expanded = 0

        if graph_mode == "global":
            # Global map-reduce: answer comes from community summaries
            try:
                with tracing.stage("graphrag-global") as _span:
                    from app.services.graphrag_retriever import global_search
                    global_answer = await global_search(req.query, max_communities=settings.GRAPHRAG_GLOBAL_MAX_COMMUNITIES)
                    _span.set_attribute("answer_len", len(global_answer.get("answer", "")))
            except Exception as _gs_exc:
                logger.warning("GraphRAG global_search failed (non-fatal): %s", _gs_exc)
                global_answer = None

            if global_answer and global_answer.get("answer"):
                # Return global answer in same QueryResponse shape
                retrieval_stats_global: dict = {
                    "total_retrieved": 0,
                    "pool_size": 0,
                    "after_reranking": 0,
                    "stores_searched": ["graph_communities"],
                    "graph_mode": "global",
                    "used_communities": global_answer.get("used_communities", []),
                }
                return QueryResponse(
                    answer=global_answer["answer"],
                    confidence=0.6,
                    citations=[],
                    retrieval_stats=retrieval_stats_global,
                    query=req.query,
                    processing_time_seconds=round(time.time() - start, 2),
                    notes="Answer synthesized from knowledge graph community summaries.",
                    timings=tracing.get_timings(),
                )

        elif graph_mode == "local":
            # Local entity-centric: merge graph chunks into the rerank pool
            try:
                with tracing.stage("graphrag-local") as _span:
                    graph_chunks = await asyncio.to_thread(
                        retriever_service.graphrag_local_chunks,
                        req.query, req.document_types,
                    )
                    if graph_chunks:
                        # Dedup by chunk_id
                        existing_ids = {c.chunk_id for c in retrieved}
                        new_chunks = [c for c in graph_chunks if c.chunk_id not in existing_ids]
                        retrieved.extend(new_chunks)
                        graph_expanded = len(new_chunks)
                        _span.set_attribute("graph_expanded", graph_expanded)
            except Exception as _gl_exc:
                logger.warning("GraphRAG local_search failed (non-fatal): %s", _gl_exc)

        # The legacy multi-PDF graph expansion (graph_expanded_chunks) used to run
        # here for graph_mode == "none" â€” i.e. for ordinary, non-entity questions â€”
        # mining entities from the retrieved chunk text and pulling in related
        # documents via the graph. That meant "normal" questions still got graph
        # contributions even though route_graphrag() found no entity to justify it.
        # GraphRAG now only participates when the query itself names an entity that
        # exists in the graph (graph_mode == "local", handled above); a "none"
        # result means the plain vector/hybrid pipeline answers with no graph.

        total_retrieved = len(retrieved)

        if not retrieved:
            return QueryResponse(
                answer=(
                    "No relevant documents found. "
                    "Please upload relevant documents or try a broader query."
                ),
                confidence=0.0,
                citations=[],
                retrieval_stats={"total_retrieved": 0, "after_reranking": 0, "stores_searched": []},
                query=req.query,
                processing_time_seconds=round(time.time() - start, 2),
                timings=tracing.get_timings(),
            )

        # Balanced per-store pool â†’ cross-encoder rerank (CPU-heavy) â€” thread pool
        with tracing.stage("rank") as _span:
            final_chunks, pool_size = await asyncio.to_thread(
                _rank_chunks, req.query, retrieved, req.top_k, req.use_reranker, rerank_cap
            )
            _span.set_attribute("pool_size", pool_size)

        stores_searched = list({c.store_type for c in retrieved})

    # â”€â”€ Structured table query (Phase 2, additive) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Runs alongside retrieval/rerank above â€” never replaces it. When it
    # recognizes an exact aggregate/lookup intent, its computed fact is
    # prefixed onto the synthesis query so Groq can cite the exact number
    # instead of estimating from retrieved text. When it returns None
    # (feature disabled or intent not recognized), synthesis_query stays
    # byte-identical to req.query â€” no behavior change.
    with tracing.stage("structured-query"):
        structured_result = await asyncio.to_thread(_try_structured_query, req)
    synthesis_query = req.query
    if structured_result is not None:
        synthesis_query = f"{_format_structured_fact(structured_result)}\n\n{req.query}"

    # â”€â”€ Synthesis â€” same for both paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        with tracing.stage("synthesize"):
            synthesis = await synthesis_service.synthesize(synthesis_query, final_chunks)
    except Exception as e:
        logger.exception("Synthesis failed")
        synthesis = {
            "answer": "Answer synthesis failed. Please review the citations below.",
            "confidence": 0.0,
            "confidence_breakdown": None,
            "sources_used": [],
            "notes": str(e),
        }

    bucket = settings.SUPABASE_STORAGE_BUCKET
    citations = [_citation_from_chunk(c, bucket) for c in final_chunks]

    retrieval_stats: dict = {
        "total_retrieved": total_retrieved,
        "pool_size": pool_size,
        "after_reranking": len(final_chunks),
        "stores_searched": stores_searched,
    }
    if settings.AGENTIC_RAG_ENABLED:
        # graph_mode/graph_expanded were computed inside agentic_pipeline.run()
        # (Phase 0 route_graphrag + local-search merge) and returned via
        # agentic_stats â€” they never existed as query.py locals on this path,
        # so pull them from there instead of the classic-path variables below.
        retrieval_stats["graph_mode"] = (agentic_stats or {}).get("graph_mode", "none")
        retrieval_stats["graph_expanded"] = (agentic_stats or {}).get("graph_expanded", 0)
    else:
        retrieval_stats["graph_expanded"] = graph_expanded  # type: ignore[name-defined]
        retrieval_stats["graph_mode"] = graph_mode  # type: ignore[name-defined]

    return QueryResponse(
        answer=synthesis["answer"],
        confidence=synthesis["confidence"],
        confidence_breakdown=synthesis.get("confidence_breakdown"),
        citations=citations,
        retrieval_stats=retrieval_stats,
        query=req.query,
        processing_time_seconds=round(time.time() - start, 2),
        notes=synthesis.get("notes"),
        agentic_stats=agentic_stats,
        structured_result=structured_result,
        timings=tracing.get_timings(),
    )


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/query/stream")
async def query_documents_stream(req: QueryRequest):
    """SSE streaming variant â€” emits token deltas then a final metadata event.
    The frontend uses this for real-time display; /query returns a full JSON object.

    IMPORTANT: ALL blocking work (retrieval, graph expansion, reranking) is now
    performed INSIDE the async generator body, after an initial heartbeat event is
    yielded.  This causes FastAPI to flush HTTP 200 headers to the client on the
    very first yield, establishing the SSE connection before any slow work begins.
    Without this, headers were only sent after all pre-stream work completed (~30 s),
    causing the Next.js proxy's AbortController to fire before the backend responded.

    When AGENTIC_RAG_ENABLED=True:
    - Before token streaming, stage events are emitted:
      {"type":"stage","stage":"raven"|"hybrid"|"spyder","loop":n,"detail":{...}}
    - The done event gains an "agentic_stats" key.
    - The existing token/done contract is unchanged so old frontend keeps working.
    """
    start = time.time()

    # Short-circuit: conversational / off-topic queries must not trigger retrieval.
    if _is_conversational(req.query):
        conv_answer = await synthesis_service.synthesize_conversational(req.query)
        async def _conv_stream():
            yield f"data: {_json.dumps({'type': 'token', 'text': conv_answer})}\n\n"
            yield f"data: {_json.dumps({'type': 'done', 'answer': conv_answer, 'confidence': 1.0, 'citations': [], 'retrieval_stats': {'total_retrieved': 0, 'after_reranking': 0, 'stores_searched': []}, 'query': req.query, 'processing_time_seconds': round(time.time() - start, 3)})}\n\n"
        return StreamingResponse(_conv_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    if req.document_types:
        invalid = set(req.document_types) - _VALID_DOC_TYPES
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid document_types: {sorted(invalid)}")

    # â”€â”€ Table query shortcut: filter/aggregation/ranking/lookup/mixed â”€â”€â”€â”€â”€â”€â”€â”€
    # Same rationale as the non-streaming handler's shortcut â€” see
    # _try_table_query_shortcut's docstring. Run before the agentic/classic
    # branch so these query shapes never depend on ANN top_k truncation or
    # burn RAVEN/hybrid-loop/SPYDER time. Classification is pure regex and
    # the structured engine has no LLM calls, so â€” like the conversational
    # short-circuit right above, which already awaits an LLM call before any
    # heartbeat â€” this is fast enough to run before the stream's heartbeat
    # yield without meaningfully risking the proxy-timeout this endpoint's
    # docstring otherwise warns about for genuinely slow pre-stream work.
    tracing.reset_timings()
    shortcut = await _try_table_query_shortcut(req)
    if shortcut is not None:
        async def _stream_shortcut():
            synthesis_query = shortcut["synthesis_query"]
            chunks = shortcut["chunks"]
            answer_parts: list[str] = []
            synthesis_ok = True
            with tracing.stage("synthesize"):
                try:
                    async for token in synthesis_service.synthesize_stream(synthesis_query, chunks):
                        answer_parts.append(token)
                        yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"
                except Exception:
                    logger.exception("Streaming synthesis error (table query shortcut)")
                    synthesis_ok = False
                    yield f"data: {_json.dumps({'type': 'token', 'text': 'Answer synthesis failed. Please review the citations below.'})}\n\n"

            if synthesis_ok and answer_parts:
                conf_result = await _blended_confidence_for_stream(
                    synthesis_query, "".join(answer_parts), chunks,
                )
            else:
                ret_breakdown = _retrieval_confidence_breakdown(chunks)
                conf_result = {
                    "confidence": ret_breakdown["score"],
                    "confidence_breakdown": {
                        "method": "retrieval_only",
                        "final": ret_breakdown["score"],
                        "components": [
                            {"label": "Retrieval confidence", "score": ret_breakdown["score"], "weight": 1.0, "detail": ret_breakdown},
                        ],
                    },
                }

            bucket = settings.SUPABASE_STORAGE_BUCKET
            shortcut_citations = [_citation_from_chunk(c, bucket) for c in chunks]
            done_payload = {
                "type": "done",
                "citations": [c.model_dump() for c in shortcut_citations],
                "confidence": conf_result["confidence"],
                "confidence_breakdown": conf_result["confidence_breakdown"],
                "retrieval_stats": {
                    "total_retrieved": len(chunks),
                    "after_reranking": len(chunks),
                    "stores_searched": list({c.store_type for c in chunks}),
                },
                "query": req.query,
                "processing_time_seconds": round(time.time() - start, 2),
                "notes": None if synthesis_ok else "Synthesis error â€” citations shown below.",
                "structured_result": shortcut["structured_result"],
                "timings": tracing.get_timings(),
            }
            yield f"data: {_json.dumps(done_payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream_shortcut(), media_type="text/event-stream", headers=_SSE_HEADERS)

    if settings.AGENTIC_RAG_ENABLED:
        # â”€â”€ Agentic path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        from app.services.agents import agentic_pipeline

        async def _stream_agentic_full():
            # Without this, _current_timings stays at its ContextVar default of
            # None for the whole request, so every tracing.stage() call inside
            # agentic_pipeline.run() (and below) silently no-ops â€” see
            # tracing.stage()'s "if timings is not None" guard. That's why the
            # agentic streaming path used to report no timings at all while the
            # classic path (which does call this) worked fine.
            tracing.reset_timings()
            # Emit heartbeat immediately so HTTP headers are flushed to the
            # client before the pipeline blocks on retrieval.
            yield f"data: {_json.dumps({'type': 'status', 'stage': 'retrieving', 'message': 'Searching documents...'})}\n\n"

            # Stage events queue: on_stage callback deposits events here;
            # the SSE generator drains them before streaming tokens.
            stage_events: list = []

            async def on_stage(stage_name: str, detail: dict) -> None:
                loop_n = detail.get("loop", 0)
                ev = {"type": "stage", "stage": stage_name, "loop": loop_n, "detail": detail}
                stage_events.append(ev)

            try:
                with tracing.stage("agentic-pipeline"):
                    final_chunks, agentic_stats = await agentic_pipeline.run(
                        query=req.query,
                        document_types=req.document_types,
                        document_id=req.document_id,
                        top_k=req.top_k,
                        use_reranker=req.use_reranker,
                        on_stage=on_stage,
                        table_filters=_to_table_filters(req.table_filters),
                        enable_hybrid=req.enable_hybrid,
                        enable_graphrag=req.enable_graphrag,
                    )
            except Exception as e:
                logger.exception("Agentic pipeline failed (stream)")
                yield f"data: {_json.dumps({'type': 'error', 'message': f'Agentic pipeline failed: {e}'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Flush any queued stage events
            for ev in stage_events:
                yield f"data: {_json.dumps(ev)}\n\n"

            # Mirror the non-streaming agentic branch: a global-mode answer
            # comes back as ([], {"graph_mode": "global", "global_answer": {...}})
            # and must be streamed as the real answer, not dropped into the
            # "no documents found" branch below.
            if agentic_stats.get("graph_mode") == "global":
                _global_answer = agentic_stats.get("global_answer") or {}
                if _global_answer.get("answer"):
                    yield f"data: {_json.dumps({'type': 'token', 'text': _global_answer['answer']})}\n\n"
                    yield f"data: {_json.dumps({'type': 'done', 'citations': [], 'confidence': 0.6, 'retrieval_stats': {'total_retrieved': 0, 'pool_size': 0, 'after_reranking': 0, 'stores_searched': ['graph_communities'], 'graph_mode': 'global', 'used_communities': _global_answer.get('used_communities', [])}, 'query': req.query, 'processing_time_seconds': round(time.time() - start, 2), 'notes': 'Answer synthesized from knowledge graph community summaries.', 'agentic_stats': agentic_stats, 'timings': tracing.get_timings()})}\n\n"
                    yield "data: [DONE]\n\n"
                    return

            if not final_chunks:
                yield f"data: {_json.dumps({'type': 'token', 'text': 'No relevant documents found. Please upload relevant documents or try a broader query.'})}\n\n"
                yield f"data: {_json.dumps({'type': 'done', 'citations': [], 'confidence': 0.0, 'retrieval_stats': {'total_retrieved': 0, 'after_reranking': 0, 'stores_searched': []}, 'query': req.query, 'processing_time_seconds': round(time.time() - start, 2), 'agentic_stats': agentic_stats, 'timings': tracing.get_timings()})}\n\n"
                yield "data: [DONE]\n\n"
                return

            stores_searched = list({c.store_type for c in final_chunks})
            pool_size = len(final_chunks)
            total_retrieved = len(final_chunks)
            bucket = settings.SUPABASE_STORAGE_BUCKET
            citations = [_citation_from_chunk(c, bucket) for c in final_chunks]

            synthesis_ok = True
            try:
                with tracing.stage("synthesize"):
                    async for token in synthesis_service.synthesize_stream(req.query, final_chunks):
                        yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"
            except Exception:
                logger.exception("Streaming synthesis error (agentic)")
                synthesis_ok = False
                yield f"data: {_json.dumps({'type': 'token', 'text': 'Answer synthesis failed. Please review the citations below.'})}\n\n"

            confidence = (
                min(round(sum(c.relevance_score for c in final_chunks) / len(final_chunks), 2), 1.0)
                if final_chunks else 0.0
            )
            confidence_breakdown = {
                "method": "chunk_average",
                "final": confidence,
                "components": [
                    {
                        "label": f"Average relevance across {len(final_chunks)} chunk(s)",
                        "score": confidence,
                        "weight": 1.0,
                        "detail": None,
                    },
                ],
            }
            done_payload = {
                "type": "done",
                "citations": [c.model_dump() for c in citations],
                "confidence": confidence,
                "confidence_breakdown": confidence_breakdown,
                "retrieval_stats": {
                    "total_retrieved": total_retrieved,
                    "pool_size": pool_size,
                    "after_reranking": len(final_chunks),
                    "stores_searched": stores_searched,
                    "graph_mode": agentic_stats.get("graph_mode", "none"),
                    "graph_expanded": agentic_stats.get("graph_expanded", 0),
                },
                "query": req.query,
                "processing_time_seconds": round(time.time() - start, 2),
                "notes": None if synthesis_ok else "Synthesis error â€” citations shown below.",
                "agentic_stats": agentic_stats,
                "timings": tracing.get_timings(),
            }
            yield f"data: {_json.dumps(done_payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream_agentic_full(), media_type="text/event-stream", headers=_SSE_HEADERS)

    else:
        # â”€â”€ Classic path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # ALL blocking work (retrieve, graph-expand, rerank, structured query)
        # is performed INSIDE the generator, after the initial heartbeat yield.
        # This lets FastAPI flush HTTP 200 + SSE headers to the client immediately,
        # preventing the Next.js proxy's 180 s AbortController from firing while
        # the backend is still in the pipeline.

        async def _stream():
            tracing.reset_timings()
            # â”€â”€ 1. Heartbeat â€” establish the SSE connection before any blocking work â”€â”€
            yield f"data: {_json.dumps({'type': 'status', 'stage': 'retrieving', 'message': 'Searching documents...'})}\n\n"
            yield f"data: {_json.dumps({'type': 'status', 'stage': 'graph-routing', 'message': 'Routing the query through GraphRAG...'})}\n\n"

            # â”€â”€ 2. Retrieval + GraphRAG routing (concurrent â€” see _resolve_graph_mode) â”€â”€
            stream_top_k_per_store, stream_rerank_cap = _resolve_retrieval_params(req.query)
            try:
                async def _retrieve():
                    with tracing.stage("retrieve", top_k_per_store=stream_top_k_per_store) as _span:
                        result = await asyncio.to_thread(
                            _classic_retrieve_fn(),
                            query=req.query,
                            document_types=req.document_types,
                            document_id=req.document_id,
                            top_k_per_store=stream_top_k_per_store,
                            table_filters=_to_table_filters(req.table_filters),
                        )
                        _span.set_attribute("chunks", len(result))
                        return result

                async def _route():
                    with tracing.stage("graphrag-route"):
                        return await _resolve_graph_mode(req.query, req.document_id)

                retrieved, stream_graph_mode = await asyncio.gather(_retrieve(), _route())
            except Exception as e:
                logger.exception("Retrieval failed (stream)")
                yield f"data: {_json.dumps({'type': 'error', 'message': f'Retrieval failed: {e}'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            stream_graph_expanded = 0
            yield f"data: {_json.dumps({'type': 'stage', 'stage': 'graphrag_route', 'detail': {'mode': stream_graph_mode}})}\n\n"
            yield f"data: {_json.dumps({'type': 'stage', 'stage': 'retrieved', 'detail': {'chunks': [_chunk_preview_for_stream(c) for c in retrieved]}})}\n\n"

            if not retrieved:
                yield f"data: {_json.dumps({'type': 'token', 'text': 'No relevant documents found. Please upload relevant documents or try a broader query.'})}\n\n"
                yield f"data: {_json.dumps({'type': 'done', 'citations': [], 'confidence': 0.0, 'retrieval_stats': {'total_retrieved': 0, 'after_reranking': 0, 'stores_searched': [], 'graph_mode': stream_graph_mode}, 'query': req.query, 'processing_time_seconds': round(time.time() - start, 2), 'timings': tracing.get_timings()})}\n\n"
                yield "data: [DONE]\n\n"
                return

            if stream_graph_mode == "global":
                try:
                    from app.services.graphrag_retriever import global_search as _global_s
                    try:
                        yield f"data: {_json.dumps({'type': 'status', 'stage': 'graph-global', 'message': 'Searching Neo4j community summaries...'})}\n\n"
                        _ga = await _global_s(req.query, max_communities=settings.GRAPHRAG_GLOBAL_MAX_COMMUNITIES)
                        answer_text = _ga.get("answer", "") if _ga else ""
                        if not answer_text:
                            answer_text = "No relevant information found in knowledge graph communities."
                        yield f"data: {_json.dumps({'type': 'token', 'text': answer_text})}\n\n"
                        done_payload_g = {
                            "type": "done",
                            "citations": [],
                            "confidence": 0.6,
                            "retrieval_stats": {
                                "total_retrieved": 0,
                                "pool_size": 0,
                                "after_reranking": 0,
                                "stores_searched": ["graph_communities"],
                                "graph_mode": "global",
                                "used_communities": (_ga or {}).get("used_communities", []),
                            },
                            "query": req.query,
                            "processing_time_seconds": round(time.time() - start, 2),
                            "notes": "Answer synthesized from knowledge graph community summaries.",
                            "timings": tracing.get_timings(),
                        }
                        yield f"data: {_json.dumps(done_payload_g)}\n\n"
                        yield "data: [DONE]\n\n"
                    except Exception as _gse:
                        logger.warning("GraphRAG global stream failed: %s", _gse)
                        yield f"data: {_json.dumps({'type': 'token', 'text': 'Global graph search failed â€” please try again.'})}\n\n"
                        yield f"data: {_json.dumps({'type': 'done', 'citations': [], 'confidence': 0.0, 'retrieval_stats': {}, 'query': req.query, 'processing_time_seconds': round(time.time() - start, 2), 'timings': tracing.get_timings()})}\n\n"
                        yield "data: [DONE]\n\n"
                    return
                except Exception as _g_imp_exc:
                    logger.warning("GraphRAG global stream import failed: %s", _g_imp_exc)

            elif stream_graph_mode == "local":
                try:
                    yield f"data: {_json.dumps({'type': 'status', 'stage': 'graph-traversal', 'message': 'Traversing matching Neo4j entities...'})}\n\n"
                    with tracing.stage("graphrag-local") as _span:
                        graph_chunks_s = await asyncio.to_thread(
                            retriever_service.graphrag_local_chunks,
                            req.query, req.document_types,
                        )
                        if graph_chunks_s:
                            existing_ids_s = {c.chunk_id for c in retrieved}
                            new_s = [c for c in graph_chunks_s if c.chunk_id not in existing_ids_s]
                            retrieved.extend(new_s)
                            stream_graph_expanded = len(new_s)
                            _span.set_attribute("graph_expanded", stream_graph_expanded)
                    yield f"data: {_json.dumps({'type': 'stage', 'stage': 'graphrag_local', 'detail': {'expanded': stream_graph_expanded}})}\n\n"
                except Exception as _sl_exc:
                    logger.warning("GraphRAG local (stream) failed: %s", _sl_exc)

            # Legacy multi-PDF graph expansion for non-entity ("none") questions is
            # intentionally NOT run here â€” GraphRAG only participates when the query
            # names an entity present in the graph (stream_graph_mode == "local",
            # handled above). See the matching note in the non-streaming path.

            # â”€â”€ 4. Rerank â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            yield f"data: {_json.dumps({'type': 'status', 'stage': 'ranking', 'message': 'Ranking results...'})}\n\n"
            with tracing.stage("rank") as _span:
                final_chunks, pool_size = await asyncio.to_thread(
                    _rank_chunks, req.query, retrieved, req.top_k, req.use_reranker, stream_rerank_cap
                )
                _span.set_attribute("pool_size", pool_size)
            stores_searched = list({c.store_type for c in retrieved})
            bucket = settings.SUPABASE_STORAGE_BUCKET
            citations = [_citation_from_chunk(c, bucket) for c in final_chunks]
            yield f"data: {_json.dumps({'type': 'stage', 'stage': 'selected', 'detail': {'chunks': [_chunk_preview_for_stream(c) for c in final_chunks]}})}\n\n"

            # â”€â”€ 5. Structured table query (additive) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            with tracing.stage("structured-query"):
                structured_result = await asyncio.to_thread(_try_structured_query, req)
            synthesis_query = req.query
            if structured_result is not None:
                synthesis_query = f"{_format_structured_fact(structured_result)}\n\n{req.query}"

            # â”€â”€ 6. Announce synthesis start â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            yield f"data: {_json.dumps({'type': 'synthesis_start', 'model': settings.GROQ_MODEL_NAME, 'max_tokens': settings.GROQ_MAX_TOKENS, 'chunks_used': len(final_chunks), 'stores_searched': stores_searched, 'graph_mode': stream_graph_mode, 'graph_expanded': stream_graph_expanded})}\n\n"

            # â”€â”€ 7. Stream synthesis tokens â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            synthesis_ok = True
            answer_parts: list[str] = []
            with tracing.stage("synthesize"):
                try:
                    async for token in synthesis_service.synthesize_stream(synthesis_query, final_chunks):
                        answer_parts.append(token)
                        yield f"data: {_json.dumps({'type': 'token', 'text': token})}\n\n"
                except Exception:
                    logger.exception("Streaming synthesis error")
                    synthesis_ok = False
                    yield f"data: {_json.dumps({'type': 'token', 'text': 'Answer synthesis failed. Please review the citations below.'})}\n\n"

            # â”€â”€ 8. Confidence (post-hoc LLM rating) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if synthesis_ok and answer_parts:
                conf_result = await _blended_confidence_for_stream(
                    synthesis_query, "".join(answer_parts), final_chunks,
                )
            else:
                ret_breakdown = _retrieval_confidence_breakdown(final_chunks)
                conf_result = {
                    "confidence": ret_breakdown["score"],
                    "confidence_breakdown": {
                        "method": "retrieval_only",
                        "final": ret_breakdown["score"],
                        "components": [
                            {"label": "Retrieval confidence", "score": ret_breakdown["score"], "weight": 1.0, "detail": ret_breakdown},
                        ],
                    },
                }

            # â”€â”€ 9. Done event â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            done_payload = {
                "type": "done",
                "citations": [c.model_dump() for c in citations],
                "confidence": conf_result["confidence"],
                "confidence_breakdown": conf_result["confidence_breakdown"],
                "retrieval_stats": {
                    "total_retrieved": len(retrieved),
                    "pool_size": pool_size,
                    "after_reranking": len(final_chunks),
                    "stores_searched": stores_searched,
                    "graph_mode": stream_graph_mode,
                    "graph_expanded": stream_graph_expanded,
                },
                "query": req.query,
                "processing_time_seconds": round(time.time() - start, 2),
                "notes": None if synthesis_ok else "Synthesis error â€” citations shown below.",
                "structured_result": structured_result,
                "timings": tracing.get_timings(),
            }
            yield f"data: {_json.dumps(done_payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
