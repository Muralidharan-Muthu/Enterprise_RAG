from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from urllib.parse import quote_plus
from pathlib import Path
import json

ROOT_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
BACKEND_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    # ── Supabase ──────────────────────────────────────────────
    SUPABASE_HOST: str = ""
    SUPABASE_PORT: int = 6543
    SUPABASE_DB: str = ""
    SUPABASE_USER: str = ""
    SUPABASE_PASSWORD: str = ""
    SUPABASE_SCHEMA: str = "multi_store_rag_working"
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "rag-documents"
    DB_SSLMODE: str = "require"

    # ── LLM (Groq API - Multi-Model Architecture) ────────────
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_API_KEY: str = ""
    GROQ_MODEL_NAME: str = "openai/gpt-oss-120b"              # Default flagship model
    GROQ_SYNTHESIS_MODEL: str = "openai/gpt-oss-120b"         # Complex RAG reasoning & multi-document synthesis (120B)
    GROQ_EXTRACTION_MODEL: str = "qwen/qwen3.6-27b"           # Knowledge Graph entity/relation extraction & Cypher (27B)
    GROQ_SPYDER_MODEL: str = "groq/compound"                  # Post-rerank sufficiency judge & corrective self-RAG (70K TPM)
    GROQ_RAVEN_MODEL: str = "groq/compound-mini"              # Pre-retrieval query reframing & decomposition (70K TPM)
    GROQ_ENRICHMENT_MODEL: str = "openai/gpt-oss-20b"         # Ingestion chunk & clause metadata enrichment (20B)
    GROQ_ROUTING_MODEL: str = "groq/compound-mini"            # Ultra-fast intent classification & query routing
    GROQ_CHAT_MODEL: str = "groq/compound-mini"               # Conversational & small talk chat
    GROQ_TIMEOUT_SECONDS: int = 120          # read timeout (model generation)
    GROQ_CONNECT_TIMEOUT_SECONDS: int = 10   # fail fast when endpoint is down
    GROQ_MAX_RETRIES: int = 6                # retries on 429 rate-limit & transient 5xx errors
    GROQ_MIN_INTERVAL_SECONDS: float = 2.0   # minimum spacing between Groq requests (~30 RPM limit safe)
    GROQ_MAX_TOKENS: int = 800               # max output tokens per answer
    GROQ_MAX_CONCURRENT: int = 3

    # Use the LLM to classify query intent (which stores to search). Off by
    # default: it adds a full Groq round-trip on the critical path, and the
    # rule-based fallback is instant and recall-safe (searches all stores when
    # ambiguous). Turn on only if store routing precision matters more than latency.
    # With INTENT_USE_SEMANTIC_ROUTER on (default), Groq is only ever reached
    # as the last-resort tier for queries neither rules nor the embedding
    # router could confidently classify — see intent_service.classify_intent.
    INTENT_USE_LLM: bool = True
    # Embedding-centroid store router (semantic_router.py) — classifies the
    # query by cosine similarity against per-store prototype embeddings. Reuses
    # the query embedding retrieve() already computes, so it adds no extra
    # model call or network round-trip. On by default: strictly cheaper and
    # more robust to paraphrasing than the keyword-only rule fallback.
    INTENT_USE_SEMANTIC_ROUTER: bool = True
    # Below this cosine similarity to every store's centroid, the query is
    # considered ambiguous — search all stores (recall-safe) rather than trust
    # a low-similarity match.
    INTENT_SEMANTIC_MIN_SIMILARITY: float = 0.30
    # Similarity gap between the best- and second-best-matching store needed to
    # call it an unambiguous single-store match. Below this gap, all stores
    # within the margin of the top score are returned together.
    INTENT_SEMANTIC_MARGIN: float = 0.05

    # ── Embeddings / Reranker ─────────────────────────────────
    BGE_MODEL_NAME: str = "BAAI/bge-large-en-v1.5"
    RERANKER_NAME: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_DEVICE: str = "cpu"

    # ── Redis / Celery ────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # ── Neo4j ─────────────────────────────────────────────────
    # Enabled by default (part of the standard GraphRAG architecture). Every
    # graph operation is guarded: graph_service._get_driver() returns None and
    # is_available() returns False when NEO4J_URI/credentials are unset or the
    # server is unreachable, so BOTH query-time routing and ingestion-time
    # extraction no-op gracefully (one connectivity check, then skipped) with no
    # errors and no wasted Groq calls. To actually populate/query the graph,
    # set NEO4J_URI + NEO4J_USERNAME/PASSWORD/DATABASE to a reachable instance.
    # For Neo4j Aura: URI is neo4j+s://<id>.databases.neo4j.io,
    # NEO4J_USERNAME and NEO4J_DATABASE are both the instance ID.
    # Set NEO4J_ENABLED=False to hard-disable and skip even the connectivity check.
    NEO4J_ENABLED: bool = True
    NEO4J_URI: str = ""
    NEO4J_USERNAME: str = ""
    NEO4J_PASSWORD: str = ""
    NEO4J_DATABASE: str = ""
    # graph_service.is_available() caches its result for this many seconds so a
    # down (or up) Neo4j isn't re-verified over the network on every call —
    # it's called multiple times per request (query.py + route_graphrag() +
    # local_search()/global_search()).
    NEO4J_HEALTH_CACHE_TTL_SECONDS: int = 300

    # ── Multi-hop retrieval (entity-chained cross-document expansion) ─
    # Bounded hop count for retriever_service.graph_expanded_chunks(): each hop
    # mines entities from the query + newly discovered chunk text, then pulls
    # documents connected via those entities. 0 disables expansion entirely.
    MULTI_HOP_MAX_HOPS: int = 2
    MULTI_HOP_MAX_RELATED_DOCS: int = 5
    MULTI_HOP_PER_DOC_TOP_K: int = 5

    # ── Agentic RAG (Feature 1.1) — all default OFF/conservative ─
    AGENTIC_RAG_ENABLED: bool = True
    RAVEN_ENABLED: bool = True
    HYBRID_SEARCH_ENABLED: bool = True
    SPYDER_ENABLED: bool = True
    AGENTIC_MAX_LOOPS: int = 2
    SPYDER_MIN_CONFIDENCE: float = 0.6
    HYBRID_RRF_K: int = 60
    # Run hybrid semantic+keyword retrieval in the CLASSIC (non-agentic) query
    # path — no RAVEN/SPYDER loop, just hybrid_retrieve() in place of
    # retriever_service.retrieve(). Independent of AGENTIC_RAG_ENABLED.
    # True (default): the classic path is vector+keyword (RRF-fused) per store —
    # the intended enterprise retrieval architecture — so hybrid is guaranteed
    # even in environments whose .env doesn't set it. Keyword half still degrades
    # gracefully to semantic-only if migration 011 (tsvector columns) is unapplied.
    # Set False to restore the legacy vector-only classic path.
    HYBRID_IN_CLASSIC_PATH: bool = True

    # ── Retrieval cache (additive, opt-in) ────────────────────
    # Master flag — False (default) means embed_query()/retrieve() behave
    # exactly as before this module existed; retrieval_cache is never imported.
    RETRIEVAL_CACHE_ENABLED: bool = True
    # Query-embedding cache: safe whenever the master flag is on — a cached
    # embedding is never stale (BGE is static/deterministic). TTL is memory
    # housekeeping only, not correctness.
    RETRIEVAL_CACHE_EMBEDDINGS_ENABLED: bool = True
    RETRIEVAL_CACHE_EMBEDDING_TTL_SECONDS: int = 21600   # 6h
    RETRIEVAL_CACHE_EMBEDDING_MAX_ENTRIES: int = 5000    # ~21 MB ceiling
    # Full-retrieve()-result cache: OFF by default even when the master flag
    # is on — results depend on live DB state, so a just-ingested document can
    # stay hidden for up to RESULT_TTL seconds. Opt in only if that staleness
    # window is acceptable.
    RETRIEVAL_CACHE_RESULTS_ENABLED: bool = True
    RETRIEVAL_CACHE_RESULT_TTL_SECONDS: int = 90
    RETRIEVAL_CACHE_RESULT_MAX_ENTRIES: int = 500

    # ── App ───────────────────────────────────────────────────
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    MAX_UPLOAD_SIZE_MB: int = 100
    CORS_ORIGINS: list[str] | str = ["*"]

    # ── Chunking ──────────────────────────────────────────────
    CHUNK_SIZE_TOKENS: int = 512
    CHUNK_OVERLAP_TOKENS: int = 64       # retained; used only on the legacy fixed-size path
    MIN_CHUNK_SIZE_TOKENS: int = 50

    # ── Semantic chunking (LlamaIndex-style breakpoint detection) ─────────────
    # Master toggle — False restores the old fixed-size sentence-overlap path.
    # Defaults MUST be present so a missing .env key never kills worker boot.
    CHUNK_USE_SEMANTIC: bool = True
    # Cosine-distance percentile above which a sentence boundary becomes a chunk
    # break.  95 = cut only the top 5 % most dissimilar consecutive sentences.
    CHUNK_SEMANTIC_BREAKPOINT_PERCENTILE: int = 95
    # Hard ceiling on chunk size (tokens).  Chunks that exceed this are
    # force-split at the next-best interior breakpoint.
    CHUNK_MAX_TOKENS: int = 1024
    # How many chunks to send to Groq per enrichment call.  Larger batches
    # reduce API round-trips; smaller batches lower per-call latency variance.
    CHUNK_ENRICH_BATCH_SIZE: int = 8

    # ── Docling parsing ───────────────────────────────────────
    # OCR is the dominant parsing cost (~180s for a 5-page image/table-heavy
    # PDF on CPU). Keep it on for scanned docs; set DOCLING_DO_OCR=false to
    # parse text-layer PDFs in seconds. images_scale trades crop quality for
    # render speed (2.0 = sharp but slow, 1.0 = fast).
    DOCLING_DO_OCR: bool = True
    DOCLING_DO_TABLE_STRUCTURE: bool = True
    DOCLING_IMAGES_SCALE: float = 2.0

    # ── Image pre-filter (before the VLM) ─────────────────────
    # A cheap PIL/numpy pipeline decides SKIP / OCR_ONLY / VLM_PROCESSED so the
    # expensive VLM (and OCR) is not run on junk (logos, icons, blanks,
    # separators, duplicates). Fail-open: any error defaults to VLM_PROCESSED so
    # an informative image is never dropped by a filter bug. Tunable thresholds:
    PREFILTER_ENABLED: bool = True
    PREFILTER_MIN_DIM: int = 24            # px; smaller on either side -> tiny -> skip
    PREFILTER_MIN_AREA: int = 1600         # px^2 (40x40); below -> tiny -> skip
    PREFILTER_BLANK_STD: float = 6.0       # grayscale std below -> blank -> skip
    PREFILTER_SEPARATOR_ASPECT: float = 12.0   # max/min dim above + thin -> separator
    PREFILTER_SEPARATOR_THIN_DIM: int = 40     # px; short side of a wide separator bar
                                               # (> MIN_DIM so it isn't pre-empted by 'tiny')
    PREFILTER_DUP_HAMMING: int = 5         # aHash hamming distance <= this -> duplicate
    # Pre-OCR decorative-icon detection (Stage 1) — saves the OCR pass too, not just
    # the VLM call. Deliberately MORE conservative than the post-OCR icon rule
    # (smaller area) because here we cannot confirm "no text" via OCR. Real charts/
    # tables are far larger, so a very small area is a high-confidence decorative
    # signal on its own.
    PREFILTER_VERY_SMALL_AREA: int = 6000      # px^2 (~77x77); below -> obvious icon (no OCR)
    PREFILTER_LOWCOMPLEXITY_EDGE: float = 0.01 # near-flat graphic: almost no edges ...
    PREFILTER_LOWCOMPLEXITY_COLORS: int = 6    # ... and very few colours -> skip pre-OCR
    # Decorative icon/logo = SMALL + (almost) no text. Real charts/tables are far
    # larger (tens of thousands to millions of px^2) and carry axis/cell text, so
    # area+OCR separate them cleanly. Colour count is NOT used: real rasterised
    # icons are antialiased/gradient-filled and can have MANY colours.
    PREFILTER_ICON_MAX_AREA: int = 10000   # px^2 (~100x100); above this -> not a decorative icon
    PREFILTER_ICON_MAX_OCR_CHARS: int = 6  # decorative icon carries ~no text
    PREFILTER_LOWINFO_MAX_OCR_CHARS: int = 8   # low-info skip: almost no text ...
    PREFILTER_LOWINFO_MAX_EDGE: float = 0.02   # ... and almost no structure ...
    PREFILTER_LOWINFO_MAX_COLORS: int = 16     # ... and few colors
    PREFILTER_TEXT_MIN_OCR_CHARS: int = 12     # OCR_ONLY: real text, no visual structure

    # ── Document Parsing Engine ───────────────────────────────
    # Uses Docling with local offline model artifacts stored in backend/docling_models/
    DOCLING_ENABLED: bool = True
    DOCLING_DO_OCR: bool = True
    DOCLING_DO_TABLE_STRUCTURE: bool = True
    DOCLING_IMAGES_SCALE: float = 1.0
    XLSX_MAX_ROWS_PER_SHEET: int = 10000

    # ── Ingestion safety & performance (A+B) ──────────────────
    # Real BGE tokenizer for chunk token counts instead of whitespace word-count.
    # False restores the old len(text.split()) approximation. When True, chunkers
    # count/limit by the actual BGE subword tokenizer so CHUNK_MAX_TOKENS truly holds.
    CHUNK_USE_REAL_TOKENIZER: bool = True
    # Max characters of OCR text appended to the VLM prompt. Unbounded OCR could
    # overflow the model context and silently truncate the useful instructions.
    VLM_OCR_MAX_CHARS: int = 8000
    # Max VLM image analyses in flight at once during ingestion. Bounds pressure on
    # the CDAC endpoint while still overlapping the per-image latency. 1 = sequential.
    VLM_MAX_CONCURRENCY: int = 4
    # Max output tokens for the per-image VLM extraction call (image_analysis_service).
    VLM_MAX_TOKENS: int = 1536
    # Groq Vision-Language Model for multimodal image analysis & table reconstruction
    GROQ_VLM_MODEL: str = "llama-3.2-11b-vision-preview"
    # Timeout in seconds for per-image VLM analysis (prevents pipeline stalling)
    VLM_TIMEOUT_SECONDS: float = 12.0
    # Flag to toggle VLM image extraction
    VLM_ENABLED: bool = True
    # Run the VLM on Docling-extracted table crops (image + Docling text as OCR
    # evidence) to reconstruct a clean structured table — correct OCR errors,
    # recover missing values, rebuild merged cells. VLM output becomes authoritative
    # for table_store; on any failure the original Docling extraction is kept (no
    # regression). False restores the pre-VLM table-crop behaviour.
    TABLE_VLM_RECONSTRUCT: bool = True
    # Max characters of retrieved context assembled into a synthesis prompt. Keeps
    # the Groq prompt within a safe context budget (~4 chars/token heuristic).
    SYNTHESIS_CONTEXT_MAX_CHARS: int = 12000

    # ── Context compression (extractive, accuracy-first) ──────
    # After reranking, before synthesis: score each SENTENCE of the top text
    # chunks against the query with the cross-encoder reranker and keep only the
    # relevant sentences. Extractive (never rewrites/summarizes) so source text
    # stays verbatim and citation fidelity is preserved. Tables/images and
    # graph-sourced chunks are exempt (verbatim figures / deliberately-low
    # similarity). Feeds a NEW compressed_text field into the synthesis prompt;
    # chunk.text (what citations render) is never mutated.
    CONTEXT_COMPRESSION_ENABLED: bool = True
    # Chunks with <= this many sentences are already dense — skip compression
    # (nothing to gain, avoids over-pruning short passages).
    CONTEXT_COMPRESSION_MIN_SENTENCES: int = 4
    # Keep a sentence when its cross-encoder relevance (sigmoid of the logit) is
    # >= this. Accuracy-first default is permissive — the goal is dropping clearly
    # off-topic sentences, not aggressive summarization.
    CONTEXT_COMPRESSION_KEEP_SCORE: float = 0.30
    # Hard cap on sentences kept per chunk (highest-scoring survive), so one long
    # chunk can't dominate the prompt budget after compression.
    CONTEXT_COMPRESSION_MAX_SENTENCES: int = 6
    # Never compress a chunk down to fewer than this many sentences — guarantees
    # the best sentence(s) always survive even if every score is below threshold.
    CONTEXT_COMPRESSION_MIN_KEEP: int = 1
    # Upper bound on total sentence pairs scored per query (across all chunks) so
    # compression latency stays bounded regardless of chunk sizes.
    CONTEXT_COMPRESSION_MAX_PAIRS: int = 120

    # ── Multi-format ingestion (Feature 1.2) ──────────────────
    # NOTE: these Feature 1.2–1.6 fields were dropped by a bad merge (161634e)
    # while the code that reads them stayed. A missing field → AttributeError at
    # task runtime (e.g. ingestion_orchestrator.py reads INGESTION_STAGED_ENABLED
    # before the try/except, so the task dies before updating any stage and the
    # job is stuck showing "Queued" forever). Keep them declared with defaults.
    ALLOWED_UPLOAD_EXTS: list[str] = [".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md"]
    XLSX_MAX_ROWS_PER_SHEET: int = 5000

    # ── Scalable table vectorization (Feature 1.5) ────────────
    # Child row-window chunking knobs
    TABLE_CHUNK_MAX_TOKENS: int = 256          # approximate token budget per child window
    TABLE_CHUNK_MAX_ROWS: int = 25             # hard row-count cap per window
    TABLE_CHUNK_OVERLAP_ROWS: int = 0          # rows to repeat between consecutive windows
    TABLE_MAX_WINDOWS_PER_TABLE: int = 200     # cap windows/table; excess → coarser windows
    # Cap on how many child row-windows from the SAME table_id may survive the
    # per-table dedup step in one query result (retrieval-time, not ingestion-time
    # like TABLE_MAX_WINDOWS_PER_TABLE above). Keeps the top-K closest windows per
    # table_id instead of collapsing to a single best window, so a wide table with
    # multiple genuinely relevant row-windows isn't reduced to just one. Still
    # subject to the overall top_k truncation of the final result list.
    TABLE_MAX_WINDOWS_PER_QUERY_RESULT: int = 2
    # Enable ANN search over table_chunk_store (child) instead of parent-only table_store
    TABLE_CHILD_SEARCH_ENABLED: bool = True
    # Exact structured table query engine (Phase 2, additive) — rule-based
    # SUM/AVG/COUNT/MIN/MAX + exact row/column lookup over table_store.json_data,
    # run ALONGSIDE (never instead of) the semantic retrieval+synthesis path.
    # False disables the feature entirely: try_structured_query is never called
    # and response.structured_result stays absent — zero behavior change.
    STRUCTURED_QUERY_ENABLED: bool = True

    # Tier-1 SQL pushdown for structured table queries (migration 019:
    # table_cell_store/table_row_store) — indexed WHERE/AND/OR/BETWEEN/IN/
    # GROUP BY evaluated server-side instead of scanning table_store.json_data
    # in Python. False (or the migration not yet applied) makes
    # table_query_engine.try_structured_query() skip straight to the tier-2
    # Python/JSONB engine — same recognized intents, just single-table/
    # single-AND-condition/no-GROUP-BY, and slower on very large tables.
    TABLE_CELL_STORE_ENABLED: bool = True
    # Verbatim-row cap injected into the synthesis prompt for FILTER/RANKING
    # structured results — protects SYNTHESIS_CONTEXT_MAX_CHARS from an
    # unbounded match set. Truncation is always announced in the fact block
    # ("showing N of M — refine your query for the rest"), never silent.
    TABLE_STRUCTURED_MAX_ROWS_INJECTED: int = 200
    # GROUP BY support in the tier-1 SQL pushdown engine (table_sql_compiler.
    # run_group_by). False disables GROUP BY detection/execution entirely —
    # a "average revenue by sector" query then falls through past structured
    # querying to ordinary semantic retrieval, same as before this feature.
    TABLE_GROUP_BY_ENABLED: bool = True

    # ── Staged ingestion (Feature 1.6) ────────────────────────
    # Master flag — False (default) keeps the original monolithic ingest_document
    # task so existing deployments are unaffected.  Set True to enable the
    # two-task chain (parse_document_task → chunk_embed_store_task).
    INGESTION_STAGED_ENABLED: bool = False
    # Bucket for staging blobs.  Empty string → fall back to SUPABASE_STORAGE_BUCKET
    # so a single-bucket deployment works with zero extra config.
    PARSE_STAGING_BUCKET: str = ""
    # 0 = delete staging blobs immediately after embed task succeeds.
    # N > 0 = keep for N days (manual / scheduled cleanup).
    PARSE_STAGING_RETENTION_DAYS: int = 0
    # Celery rate limit for the embed queue (Celery rate_limit format: "N/m", "N/h").
    EMBED_QUEUE_RATE_LIMIT: str = "10/m"
    # Concurrency hints (used in worker launch docs; not applied programmatically
    # here — pass -c <N> to the celery worker command).
    PARSE_QUEUE_CONCURRENCY: int = 4
    EMBED_QUEUE_CONCURRENCY: int = 1

    # ── Full GraphRAG (Feature 1.3) ───────────────────────────
    # Master flag — True by default (standard architecture): enables typed entity
    # relations, communities, and local/global graph search. Requires
    # NEO4J_ENABLED=True and a reachable Neo4j to actually do anything — when the
    # graph is unavailable, run_graph_stage() (ingestion) and route_graphrag()
    # (query) both short-circuit on is_available(), so this is safe to leave on
    # even without Neo4j provisioned. Sub-flags below (per-chunk/table/image
    # extraction) are already on, so this single flag turns GraphRAG on fully.
    # Set False to fall back to the lightweight doc-level entity graph.
    GRAPHRAG_ENABLED: bool = True
    # Extract entities/relationships per chunk (True) or doc-level fallback (False).
    GRAPHRAG_EXTRACT_PER_CHUNK: bool = True
    # Beyond this many chunks, fall back to doc-level extraction (cost ceiling).
    GRAPHRAG_MAX_CHUNKS_PER_DOC: int = 200
    # Worker-side ThreadPoolExecutor concurrency for parallel Groq extraction calls.
    GRAPHRAG_EXTRACT_CONCURRENCY: int = 2
    # Max entities to extract per chunk (or per doc in fallback mode).
    GRAPHRAG_ENTITIES_PER_CHUNK: int = 15
    # Community detection algorithm: "louvain" (python-louvain) or "label_propagation".
    GRAPHRAG_COMMUNITY_ALGO: str = "louvain"
    # Minimum seconds between community recompute runs (debounce).
    GRAPHRAG_COMMUNITY_MIN_INTERVAL_SEC: int = 1800
    # Also rebuild if dirty doc count reaches this threshold before the interval.
    # Kept comfortably above a typical ingestion burst so a batch upload doesn't
    # force a full-graph resummarize on every document (the interval still bounds
    # staleness). Lower it only if you need communities to refresh mid-burst.
    GRAPHRAG_COMMUNITY_DIRTY_DOCS: int = 25
    # Single-flight lock TTL (seconds). Only one recompute runs at a time; any
    # duplicate or redelivered task within this window skips immediately instead
    # of re-running the (expensive, per-community Groq) summarization. Must be
    # long enough to cover one full rebuild; auto-expires if the holder crashes.
    GRAPHRAG_COMMUNITY_LOCK_TTL_SEC: int = 900
    # Only communities with at least this many member entities get an LLM
    # summary; smaller ones (singletons/pairs — common with fragmented
    # partitions) get a cheap member-list summary with NO Groq call. This is
    # the main lever that keeps a recompute from firing one call per community.
    GRAPHRAG_COMMUNITY_MIN_SIZE: int = 3
    # Hard ceiling on Groq summaries per recompute run. The largest communities
    # are summarized first; the rest fall back to the cheap member-list summary.
    GRAPHRAG_COMMUNITY_MAX_SUMMARIES: int = 40
    # Hops to traverse in local neighborhood search (2 = entity + up to 2-hop neighbours
    # for true multi-hop cross-document reasoning).
    GRAPHRAG_LOCAL_HOPS: int = 2
    # Max entities to extract from the query for local search.
    GRAPHRAG_LOCAL_TOP_ENTITIES: int = 10
    # Max communities to consider in global map-reduce search.
    GRAPHRAG_GLOBAL_MAX_COMMUNITIES: int = 8
    # ── Store-level entity extraction (extends GraphRAG to table/image stores) ─
    # When GRAPHRAG_ENABLED=True, also extract entities from table_store rows.
    # Uses markdown_text if available (GRAPHRAG_TABLE_PREFER_MARKDOWN=True),
    # otherwise falls back to raw_text.
    GRAPHRAG_TABLE_STORE_ENABLED: bool = True
    GRAPHRAG_TABLE_PREFER_MARKDOWN: bool = True
    # When GRAPHRAG_ENABLED=True, also extract entities from image_store rows.
    # Uses structured_content (VLM output) first, then ocr_text.
    # Images with processing_status='SKIPPED' or text shorter than
    # GRAPHRAG_IMAGE_MIN_TEXT_LEN are skipped (no useful signal).
    GRAPHRAG_IMAGE_STORE_ENABLED: bool = True
    GRAPHRAG_IMAGE_MIN_TEXT_LEN: int = 30

    # ── Observability (OpenTelemetry tracing + latency timings) ───────────
    # Master flag — spans are still created when True even with no exporter
    # configured (near-zero overhead, just dropped instead of shipped), which
    # is what powers QueryResponse.timings regardless of whether an OTel
    # collector is deployed. False fully skips TracerProvider/instrumentation
    # setup in app.core.tracing.setup_tracing().
    OTEL_ENABLED: bool = True
    # OTLP/HTTP collector endpoint (e.g. Tempo/Jaeger/Datadog-agent). Empty
    # (default) = spans are created but never exported — safe for dev/CI
    # environments with no collector running.
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_SERVICE_NAME: str = "multi-store-rag-backend"

    # ── Adaptive retrieval planning ────────────────────────────────────────
    # Scale retrieval depth to query complexity instead of a fixed
    # top_k_per_store for every query. False reproduces the previous fixed
    # behavior exactly (top_k_per_store=10, rerank per-store cap=8 — see
    # ADAPTIVE_TOP_K_SIMPLE/ADAPTIVE_RERANK_CAP_SIMPLE below, which match the
    # old hardcoded constants).
    ADAPTIVE_RETRIEVAL_ENABLED: bool = True
    ADAPTIVE_TOP_K_SIMPLE: int = 10
    ADAPTIVE_TOP_K_COMPLEX: int = 25
    ADAPTIVE_RERANK_CAP_SIMPLE: int = 8
    ADAPTIVE_RERANK_CAP_COMPLEX: int = 20
    # A query is "complex" if it has at least this many words, OR contains one
    # of the comparison/aggregation cue phrases below. Deliberately a cheap
    # string heuristic (no embedding/LLM call) so adaptive planning itself
    # adds no latency.
    ADAPTIVE_COMPLEXITY_WORD_THRESHOLD: int = 14

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                try:
                    return json.loads(v)
                except Exception:
                    pass
            if "," in v:
                return [i.strip() for i in v.split(",") if i.strip()]
            return [v] if v else ["*"]
        return v

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.SUPABASE_USER}:{quote_plus(self.SUPABASE_PASSWORD)}"
            f"@{self.SUPABASE_HOST}:{self.SUPABASE_PORT}/{self.SUPABASE_DB}"
        )

    def model_post_init(self, __context) -> None:
        # Strip potential trailing/leading whitespace from Supabase & Neo4j config
        self.SUPABASE_HOST = self.SUPABASE_HOST.strip()
        self.SUPABASE_URL = self.SUPABASE_URL.strip()
        self.SUPABASE_USER = self.SUPABASE_USER.strip()
        self.SUPABASE_PASSWORD = self.SUPABASE_PASSWORD.strip()
        self.NEO4J_URI = self.NEO4J_URI.strip()
        self.NEO4J_USERNAME = self.NEO4J_USERNAME.strip()
        self.NEO4J_PASSWORD = self.NEO4J_PASSWORD.strip()
        self.NEO4J_DATABASE = self.NEO4J_DATABASE.strip()

        self.GROQ_API_KEY = self.GROQ_API_KEY.strip()
        self.GROQ_BASE_URL = self.GROQ_BASE_URL.strip()
        self.GROQ_MODEL_NAME = self.GROQ_MODEL_NAME.strip()
        self.GROQ_SYNTHESIS_MODEL = (self.GROQ_SYNTHESIS_MODEL or self.GROQ_MODEL_NAME).strip()
        self.GROQ_ROUTING_MODEL = (self.GROQ_ROUTING_MODEL or self.GROQ_MODEL_NAME).strip()
        self.GROQ_EXTRACTION_MODEL = (self.GROQ_EXTRACTION_MODEL or self.GROQ_MODEL_NAME).strip()
        self.GROQ_SPYDER_MODEL = (self.GROQ_SPYDER_MODEL or self.GROQ_MODEL_NAME).strip()
        self.GROQ_RAVEN_MODEL = (self.GROQ_RAVEN_MODEL or self.GROQ_MODEL_NAME).strip()
        self.GROQ_CHAT_MODEL = (self.GROQ_CHAT_MODEL or self.GROQ_MODEL_NAME).strip()

        def _sanitize_redis_ssl(u: str) -> str:
            u = (u or "").strip()
            # Replace uppercase CERT_NONE with lowercase none for redis-py compatibility
            u = u.replace("ssl_cert_reqs=CERT_NONE", "ssl_cert_reqs=none")
            if u.startswith("rediss://") and "ssl_cert_reqs" not in u:
                sep = "&" if "?" in u else "?"
                return f"{u}{sep}ssl_cert_reqs=none"
            return u

        self.REDIS_URL = _sanitize_redis_ssl(self.REDIS_URL)
        self.CELERY_BROKER_URL = _sanitize_redis_ssl(self.CELERY_BROKER_URL)
        self.CELERY_RESULT_BACKEND = _sanitize_redis_ssl(self.CELERY_RESULT_BACKEND)



    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    model_config = SettingsConfigDict(
        # Both absolute (cwd-independent, unlike the old ("../.env", ".env")
        # relative tuple) — repo-root .env first, backend/.env second so it
        # overrides. Restores the backend/.env fallback that af3cd0a dropped,
        # which silently emptied NEO4J_URI/USERNAME etc. in checkouts without a
        # repo-root .env.
        env_file=(str(ROOT_ENV_FILE), str(BACKEND_ENV_FILE)),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
