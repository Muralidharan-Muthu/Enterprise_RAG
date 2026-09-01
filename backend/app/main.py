import asyncio
import concurrent.futures
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.logging import setup_logging
from app.core.tracing import setup_tracing
from app.api.routes import health, ingestion, documents, query, chats, graph as graph_routes, auth as auth_routes

setup_logging()
logger = logging.getLogger(__name__)

# Workers for asyncio.to_thread() calls (DB retrieval + reranker).
# Sized small: each blocked thread holds memory; 6 covers burst concurrency
# without exhausting a development machine.
_QUERY_THREAD_POOL_SIZE = 6


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-create the thread pool and warm all threads NOW — before any model
    # loads into this process.
    #
    # On Windows, each thread reserves ~1 MB of stack space from the page file.
    # If we defer thread creation until the first request (by which point the
    # BGE model has loaded 1.3 GB), there may not be enough page-file space left
    # → OS error 1455 "paging file too small".  Creating threads first (costs
    # ~6 MB total) while virtual memory is still plentiful avoids this entirely.
    #
    # BGE / reranker still load lazily on the first real query — we deliberately
    # do NOT pre-warm them here because PyTorch model loading holds the GIL and
    # would starve the event loop during the 30-60 s load window.
    loop = asyncio.get_running_loop()
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=_QUERY_THREAD_POOL_SIZE,
        thread_name_prefix="multi-store-query",
    )
    # Submit trivial tasks to every worker slot so all threads are spawned now.
    futures = [pool.submit(lambda: None) for _ in range(_QUERY_THREAD_POOL_SIZE)]
    concurrent.futures.wait(futures, timeout=10)
    loop.set_default_executor(pool)
    logger.info("Query thread pool ready (%d threads pre-created)", _QUERY_THREAD_POOL_SIZE)

    # Ensure Neo4j GraphRAG schema (constraints + indexes) on startup.
    # Idempotent: no-op when graph is disabled or unavailable.
    if settings.NEO4J_ENABLED:
        try:
            from app.services import graph_service
            graph_service.ensure_schema()
            logger.info("Neo4j GraphRAG schema initialization attempted")
        except Exception as _schema_exc:
            logger.warning("Neo4j schema init failed (non-fatal): %s", _schema_exc)

    # Verify the full-text (tsvector) columns the keyword half of hybrid search
    # depends on. Without migration 011, keyword_search() silently no-ops and
    # hybrid retrieval quietly degrades to semantic-only — surface that loudly.
    # Only checked when keyword search can actually run on the query path.
    _keyword_active = settings.HYBRID_SEARCH_ENABLED and (
        settings.HYBRID_IN_CLASSIC_PATH or settings.AGENTIC_RAG_ENABLED
    )
    if _keyword_active:
        try:
            from app.services import hybrid_search_service
            missing = hybrid_search_service.verify_fulltext_columns()
            if missing:
                logger.warning(
                    "HYBRID SEARCH DEGRADED: full-text tsvector columns missing (%s). "
                    "Keyword retrieval is INACTIVE — hybrid search is running "
                    "semantic-only. Apply migration 011_fulltext_search.sql in "
                    "Supabase to enable the keyword half.",
                    ", ".join(missing),
                )
            else:
                logger.info("Hybrid keyword search: all tsvector columns present.")
    # Pre-warm Docling neural models in a background thread so first document parse is instant
    def _warmup_docling():
        try:
            import time
            time.sleep(2)
            from app.services.document_parser import _make_converter
            _make_converter(do_ocr=False)
            logger.info("Docling neural models pre-warmed in background thread successfully")
        except Exception as wex:
            logger.warning("Docling warmup skipped: %s", wex)

    import threading
    threading.Thread(target=_warmup_docling, daemon=True, name="docling-warmup").start()

    # Auto-recover any jobs stuck in queued/uploaded state on server startup
    def _recover_stuck_jobs():
        try:
            import time
            time.sleep(15)
            from app.db.connection import get_db
            from app.services.ingestion_orchestrator import ingest_document
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT d.id, d.storage_path, j.id
                        FROM multi_store_rag_working.document_registry d
                        JOIN multi_store_rag_working.ingestion_jobs j ON j.document_id = d.id
                        WHERE d.status IN ('uploaded', 'processing') AND j.current_stage IN ('queued', 'parsing')
                        ORDER BY d.created_at ASC
                        LIMIT 10
                        """
                    )
                    stuck = cur.fetchall()
            for doc_id, storage_path, job_id in stuck:
                logger.info("Auto-recovering stuck ingestion job for doc %s (job %s)", doc_id, job_id)
                try:
                    ingest_document.run(doc_id, storage_path, job_id)
                except Exception as rec_err:
                    logger.error("Failed to auto-recover job %s: %s", job_id, rec_err)
        except Exception as scan_err:
            logger.warning("Startup job recovery scan: %s", scan_err)

    import threading
    threading.Thread(target=_recover_stuck_jobs, daemon=True, name="job-recovery").start()

    yield

    pool.shutdown(wait=False)

    # Close Neo4j driver on shutdown
    try:
        from app.services import graph_service
        graph_service.close()
    except Exception:
        pass


app = FastAPI(
    title="Enterprise RAG API",
    description="Enterprise Agentic RAG — Ingestion & multi-store document intelligence platform",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

setup_tracing(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(ingestion.router, prefix="/api/v1/ingest", tags=["ingestion"])
app.include_router(documents.router, prefix="/api/v1/documents", tags=["documents"])
app.include_router(query.router, prefix="/api/v1", tags=["query"])
app.include_router(chats.router, prefix="/api/v1/chats", tags=["chats"])
app.include_router(graph_routes.router, prefix="/api/v1/graph", tags=["graph"])
app.include_router(auth_routes.router, prefix="/api/v1/auth", tags=["auth"])


@app.get("/", include_in_schema=False)
def root():
    return {"service": "Enterprise RAG API", "version": "1.0.0", "docs": "/api/docs"}
