# Enterprise RAG — Multi-Store Document Intelligence Platform

**Enterprise RAG** is a production-grade, agentic document intelligence platform that decomposes enterprise PDFs into **five purpose-built specialized datastores** and answers queries with grounded, page-cited responses via a streaming Next.js chat UI.

> Upload a 200-page 10-K annual report. Ask *"What was the EBITDA and YoY revenue growth?"*  
> The system executes a SQL pushdown query across structured row metrics and returns the exact figure with a table citation — no hallucinations.

---

## Why Enterprise RAG?

Generic vector databases force every document element — financial tables, legal clauses, org-chart entities, and prose paragraphs — into the same flat embedding index. This causes:

- **Numeric hallucinations**: LLMs estimate totals that should be a simple SQL `SUM`.
- **Clause misses**: Legal risk clauses buried in dense contract text are semantically similar to benign prose.
- **Graph blindness**: Subsidiary ownership trees require multi-hop traversal, not nearest-neighbour search.

Enterprise RAG solves this by routing every document dimension to its optimal store at ingestion time.

---

## The 5 Specialized Stores

| Store | Technology | What It Holds | Query Pattern |
|---|---|---|---|
| `table_store` + `table_row_store` | Supabase Postgres + pgvector | Macro table grids + typed JSONB row metrics | Single-pass SQL pushdown (`SUM`, `AVG`, `WHERE > 50K`) |
| `clause_store` | Supabase Postgres + pgvector | Legal clauses, risk tiers (LOW/MEDIUM/HIGH/CRITICAL), governing law, parties | JSONB metadata filter + semantic similarity |
| Neo4j Graph Store | Neo4j Aura | Cross-document entities, relationships, org trees | Cypher multi-hop traversal + Louvain community summaries |
| `vector_store` | Supabase pgvector (HNSW 1024-dim) | Text prose, SOPs, policies, ESG narratives | Hybrid BM25 + dense BGE + RRF fusion |
| `image_store` | Supabase Postgres + Storage | Chart crops, visual tables, VLM-captioned figures | VLM visual grounding + OCR keyword search |

---

## What You Can Upload

| Document Type | Examples | Primary Stores Used |
|---|---|---|
| Financial & Corporate Reports | Annual Reports, 10-K, 10-Q, P&L, Segment KPIs | `table_store`, `table_row_store`, `vector_store` |
| Legal Agreements & Contracts | MSAs, NDAs, SLAs, Vendor Contracts | `clause_store`, `vector_store` |
| M&A & Corporate Filings | IPO Prospectuses, Shareholder Circulars, Org Charts | Neo4j Graph Store, `vector_store` |
| Policies, SOPs & ESG | Employee Handbooks, Sustainability Reports, Audits | `vector_store`, `image_store` |

---

## Key Features

### Ingestion Pipeline
- PDF/DOCX/XLSX/HTML document parsing via **Docling** + **PyMuPDF**
- Automatic document type classification (`financial`, `legal`, `entity`, `policy`)
- Multi-store parallel routing: tables → `table_store`, clauses → `clause_store`, entities → Neo4j, text → `vector_store`, figures → `image_store`
- Async Celery task pipeline with Redis broker, per-document job tracking, and real-time stage progress

### Query & Retrieval
- **Agentic query planning**: intent classifier routes queries to the optimal store combination
- **RAVEN**: iterative query reframing (up to 3 hops) to enrich sparse queries before retrieval
- **SPYDER**: sufficiency judge that decides whether to loop retrieval or proceed to synthesis
- **Hybrid search**: BM25 keyword + dense BGE `bge-large-en-v1.5` (1024-dim) vector search fused with Reciprocal Rank Fusion (RRF)
- **Cross-encoder reranking**: `ms-marco-MiniLM-L-6-v2` for precision-at-top re-scoring
- **SQL pushdown**: single-pass Postgres queries on `table_row_store.row_numeric` JSONB for exact financial calculations
- **GraphRAG**: local multi-hop Cypher traversal + global Louvain community summarization via Neo4j

### LLM & Synthesis
- **Groq multi-model mesh**: Groq `llama-3.3-70b` for fast routing and entity extraction, `moonshotai/kimi-k2-instruct` (120B) for deep synthesis and multi-step reasoning
- **Google Gemma 4** vision-language model for VLM figure captioning and visual table grounding (EasyOCR)
- Streaming SSE responses with exact page citations, store-type badges, and confidence scores
- Confidence scoring: blended retrieval + Gemma self-evaluation with per-component weight breakdown

### Frontend
- **Next.js 14** + TypeScript + Tailwind CSS
- Session-based multi-turn chat with message pinning, editing, and history sidebar
- Per-document chunk explorer: vector, table, clause, and image chunk inspection
- Real-time ingestion pipeline tracker with per-stage progress bars and timing breakdowns
- Dark/light mode with full responsive design

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI + Uvicorn |
| Background Tasks | Celery + Redis |
| Database | Supabase Postgres (pgvector extension) |
| File Storage | Supabase Storage |
| Graph | Neo4j Aura |
| Embeddings | BAAI/bge-large-en-v1.5 (1024-dim), via SentenceTransformers |
| Reranker | cross-encoder/ms-marco-MiniLM-L-6-v2 |
| Document Parsing | Docling, PyMuPDF, EasyOCR |
| LLM Routing | Groq API (OpenAI-compatible) |
| Vision LLM | Google Gemma 4 (via OpenAI-compatible endpoint) |
| Frontend | Next.js 14, React, TypeScript |
| Auth | Supabase Auth + JWT |
| Testing | pytest (86+ tests) |

---

## Project Structure

```text
backend/
  app/
    api/routes/         FastAPI routes: ingest, query, chats, documents, graph, health
    db/
      repositories/     pipeline_runs, table_store, clause_store, vector_store, image_store
      migrations/       023 numbered SQL migrations (Supabase Postgres)
    services/
      ingestion_orchestrator.py   master ingestion coordinator
      ingestion_tasks.py          Celery task graph
      storage_service.py          multi-store write routing
      retriever_service.py        hybrid retrieval + RRF fusion
      table_schema_service.py     unified row extraction (row_data, row_numeric, row_text)
      table_sql_compiler.py       single-pass SQL pushdown on table_row_store
      graph_rag_service.py        Neo4j Cypher traversal + Louvain summaries
      agentic_pipeline.py         RAVEN + SPYDER agentic query loop
    models/             API response and document models
  tests/                86+ backend tests (table engine, hybrid retrieval, store routing)
  scripts/              truncate_supabase.py, backfill utilities

frontend/
  src/app/              Next.js pages and API proxy routes
  src/components/
    layout/             Sidebar, ThemeToggle
    documents/          ChunkViewer, DocumentTable
    pipeline/           ChunkingDetail, PipelineCard
    upload/             FileDropzone
    ui/                 AppLogo, AppLogoIcon
  src/lib/              API client, types, auth, taxonomy, utilities

backend/app/db/migrations/
  001_initial_schema.sql       Core tables: document_registry, table_store, clause_store,
                                vector_store, image_store, ingestion_jobs, pipeline_runs
  002–021                      Incremental feature migrations
  022_remove_document_and_table_chunk_stores.sql   Decommission legacy stores
  023_merge_table_cell_into_table_row_store.sql    Unified row schema with row_numeric JSONB
```

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- Docker Desktop (for Redis)
- Supabase project with Postgres + Storage enabled
- Neo4j or Neo4j Aura (optional but required for GraphRAG)
- Groq API key (OpenAI-compatible)
- Google Gemma 4 API endpoint (for VLM image captioning)

---

## Environment Setup

```bash
cp .env.example .env
```

Key environment variables:

```env
# Supabase Postgres
SUPABASE_HOST=
SUPABASE_PORT=6543
SUPABASE_DB=postgres
SUPABASE_USER=
SUPABASE_PASSWORD=
SUPABASE_SCHEMA=multi_store_rag_working
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
SUPABASE_STORAGE_BUCKET=rag-documents

# LLM — Groq (primary chat model)
GEMMA4_BASE_URL=https://api.groq.com/openai/v1
GEMMA4_API_KEY=<your-groq-api-key>
GEMMA4_MODEL_NAME=moonshotai/kimi-k2-instruct

# LLM — Fast routing model (20B)
GROQ_FAST_MODEL=llama-3.3-70b-versatile

# Vision LLM (Gemma 4 for VLM captioning)
VISION_BASE_URL=
VISION_API_KEY=
VISION_MODEL_NAME=

# Redis / Celery
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

# Neo4j Graph
NEO4J_ENABLED=true
NEO4J_URI=
NEO4J_USERNAME=
NEO4J_PASSWORD=
NEO4J_DATABASE=

# Frontend
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Apply the 23 SQL migrations in order to your Supabase schema before first run.

---

## Run Locally

### Windows (Fast Path)

```bat
setup.bat    # installs Python venv + npm deps
run.bat      # starts Redis, API, Celery worker, and frontend
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API Swagger Docs | http://localhost:8000/api/docs |
| Health Check | http://localhost:8000/api/v1/health |

```bat
stop.bat     # graceful shutdown
```

### Docker Compose

```bash
docker compose up        # all services
docker compose up -d     # detached
docker compose down      # stop
```

### Manual

```bash
# Redis
docker compose up -d redis

# Backend API
cd backend
uvicorn app.main:app --reload --port 8000

# Celery Worker
celery -A app.core.background_tasks worker --loglevel=info -Q ingestion,celery,parse,embed,graph

# Frontend
cd frontend
npm run dev
```

---

## Main API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/ingest/pipeline` | Upload a batch of files as a named pipeline run |
| `GET` | `/api/v1/ingest/pipelines` | List all pipeline runs |
| `GET` | `/api/v1/ingest/pipelines/{run_id}` | Pipeline run detail with per-document timings |
| `POST` | `/api/v1/query` | Ask a document question (JSON response) |
| `POST` | `/api/v1/query/stream` | Streaming SSE query response |
| `GET` | `/api/v1/documents` | List all ingested documents |
| `GET` | `/api/v1/documents/{id}` | Document detail + chunk counts per store |
| `GET` | `/api/v1/documents/{id}/chunks` | Inspect stored chunks (vector, table, clause) |
| `GET` | `/api/v1/documents/{id}/images` | Inspect extracted image/figure crops |
| `GET` | `/api/v1/graph/*` | Graph entities, relationships, community summaries |
| `GET` | `/api/v1/health` | System health + store connectivity status |

---

## Testing

```bash
# Backend (86+ tests)
cd backend
.venv/Scripts/pytest

# Frontend type check + lint
cd frontend
npm run build
npm run lint
```

Test coverage includes: table SQL pushdown engine, hybrid retrieval + RRF fusion, store routing mapper, agentic pipeline loop, image analysis routing, and structured content extraction.

---

## Architecture Overview

```
User Query
    │
    ▼
Intent Classifier (Groq 70B)
    │
    ├──► Structured (financial) ──► SQL Pushdown on table_row_store (row_numeric JSONB)
    │
    ├──► Legal / Clause ──────────► clause_store semantic + JSONB risk filter
    │
    ├──► Graph / Entity ──────────► Neo4j Cypher multi-hop + Louvain summary
    │
    └──► Semantic / Policy ───────► vector_store HNSW + BM25 → RRF → Cross-encoder rerank
                                         │
                                         ▼
                                  RAVEN iterative reframing (up to 3 loops)
                                         │
                                         ▼
                                  SPYDER sufficiency judge
                                         │
                                         ▼
                              Groq 120B Synthesis (streaming SSE)
                                         │
                                         ▼
                              Cited Answer with page #, store badge, confidence %
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Upload stuck in `Queued` | Ensure Celery is running with the `ingestion` queue |
| First worker start is slow | BGE model loading takes 30–60 seconds on first run |
| Hybrid search is semantic-only | Apply migration `011_fulltext_search.sql` |
| Neo4j features unavailable | Check `NEO4J_ENABLED=true` and valid URI/credentials |
| Supabase upload fails | Verify `SUPABASE_SERVICE_KEY`, bucket name, schema, and storage policies |
| Pipeline detail returns 500 | Ensure migration 022 and 023 have been applied (dropped `document_store`, merged `table_cell_store`) |
