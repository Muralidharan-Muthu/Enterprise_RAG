# Multi-Store RAG Chatbot

Short name: **MS RAG Chatbot**

Multi-Store RAG Chatbot is an agentic document intelligence application. It lets users upload enterprise documents, extracts text, tables, clauses, images, and graph entities, stores them in dedicated retrieval stores, and answers questions with citations through a Next.js chat UI.

## What It Does

- Upload and process PDF, DOCX, PPTX, XLSX, HTML, and Markdown files.
- Parse documents with Docling, OCR, table structure extraction, and image extraction.
- Store document knowledge across vector, table, clause, image, research, and graph stores.
- Use hybrid retrieval with semantic search, keyword search, reranking, and adaptive top-k planning.
- Support GraphRAG over Neo4j for entity and multi-hop questions.
- Run structured table queries for exact lookup, filter, aggregation, ranking, and group-by answers.
- Generate cited answers with streaming responses in the frontend.
- Track ingestion jobs, pipeline runs, document metadata, extracted chunks, images, and page stats.

## Tech Stack

- Backend: FastAPI, Celery, Redis, Pydantic, httpx
- Frontend: Next.js 14, React, TypeScript, Tailwind CSS
- Storage: Supabase Postgres, Supabase Storage, pgvector
- Graph: Neo4j / Neo4j Aura
- Retrieval: BGE embeddings, cross-encoder reranker, hybrid vector plus full-text search
- Parsing: Docling, OCR, table and image processing
- LLM layer: OpenAI-compatible chat completions endpoint configured through `GEMMA4_*` environment variables

## Project Structure

```text
backend/
  app/
    api/routes/        FastAPI routes for ingest, query, chats, docs, graph, health
    db/                database connection, repositories, migrations
    services/          parsing, retrieval, GraphRAG, synthesis, table/image pipelines
    models/            API response and document models
  tests/               backend test suite

frontend/
  src/app/             Next.js pages and API proxies
  src/components/      upload, documents, pipeline, and layout components
  src/lib/             API client, types, taxonomy, utilities

docs/                  architecture notes, workflow diagrams, test docs
docker-compose.yml     Redis, API, worker, and frontend services
```

## Prerequisites

- Python 3.11+
- Node.js 18+
- Docker Desktop
- Supabase project with Postgres and Storage
- Neo4j or Neo4j Aura, optional but recommended for GraphRAG
- LLM API key for an OpenAI-compatible chat completions provider

## Environment Setup

Copy `.env.example` to `.env` in the repository root and fill in your values:

```bash
cp .env.example .env
```

Important variables:

```env
SUPABASE_HOST=
SUPABASE_PORT=6543
SUPABASE_DB=postgres
SUPABASE_USER=
SUPABASE_PASSWORD=
SUPABASE_SCHEMA=multi_store_rag_working
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
SUPABASE_STORAGE_BUCKET=rag-documents

GEMMA4_BASE_URL=
GEMMA4_API_KEY=
GEMMA4_MODEL_NAME=

REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

NEO4J_ENABLED=true
NEO4J_URI=
NEO4J_USERNAME=
NEO4J_PASSWORD=
NEO4J_DATABASE=

NEXT_PUBLIC_API_URL=http://localhost:8000
```

Apply the SQL migrations in `backend/app/db/migrations` to your Supabase schema before running ingestion.

## Run Locally

### Windows Fast Path

```bat
setup.bat
run.bat
```

This starts Redis and infrastructure with Docker, then runs the API, Celery worker, and frontend in separate local windows.

Useful URLs:

- Frontend: `http://localhost:3000`
- API docs: `http://localhost:8000/api/docs`
- Health: `http://localhost:8000/api/v1/health`

Stop everything:

```bat
stop.bat
```

### Docker Compose

```bash
docker compose up
```

Detached mode:

```bash
docker compose up -d
```

Stop:

```bash
docker compose down
```

## Manual Development Commands

Start infrastructure:

```bash
docker compose up -d redis
```

Start backend API:

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

Start Celery worker:

```bash
cd backend
celery -A app.core.background_tasks worker --loglevel=info -Q ingestion,celery,parse,embed,graph
```

Start frontend:

```bash
cd frontend
npm run dev
```

## Main API Endpoints

- `POST /api/v1/ingest/upload` - upload one document
- `POST /api/v1/ingest/pipeline` - upload a batch as a pipeline run
- `GET /api/v1/ingest/status/{job_id}` - check ingestion progress
- `POST /api/v1/query` - ask a document question
- `POST /api/v1/query/stream` - stream a document answer
- `GET /api/v1/documents` - list processed documents
- `GET /api/v1/documents/{document_id}` - inspect one document
- `GET /api/v1/documents/{document_id}/chunks` - inspect stored chunks
- `GET /api/v1/documents/{document_id}/images` - inspect extracted images
- `GET /api/v1/graph/*` - graph-related APIs

## Can You Use Groq API?

Yes. The backend already calls an OpenAI-compatible `/chat/completions` API through `backend/app/services/gemma_client.py`, so Groq can be used by changing the LLM environment variables.

Use this format:

```env
GEMMA4_BASE_URL=https://api.groq.com/openai/v1
GEMMA4_API_KEY=<your-groq-api-key>
GEMMA4_MODEL_NAME=<groq-chat-model-name>
```

Do not include `/chat/completions` in `GEMMA4_BASE_URL`; the code appends that path automatically.

Notes:

- Text chat, synthesis, routing, entity extraction, and graph summarization should work with a Groq chat model that supports OpenAI-compatible chat completions.
- Streaming should work because the project uses standard streamed chat-completion events.
- Image analysis and table-crop reconstruction require a vision-capable model. If the selected Groq model does not support image input, disable image/table VLM features or keep using a vision-capable provider for those paths.
- You may want to tune `GEMMA4_MAX_TOKENS`, `GEMMA4_TIMEOUT_SECONDS`, and `GEMMA4_MAX_CONCURRENT` based on the Groq model and rate limits.

Official Groq docs list the OpenAI-compatible base URL as `https://api.groq.com/openai/v1`.

## Testing

Backend tests:

```bash
cd backend
pytest
```

Frontend checks:

```bash
cd frontend
npm run lint
```

## Troubleshooting

- Upload stuck in `Queued`: make sure Celery is running with the `ingestion` queue.
- First worker start is slow: BGE model loading can take 30-60 seconds or more.
- Hybrid search is semantic-only: apply migration `011_fulltext_search.sql`.
- Neo4j unavailable: GraphRAG will degrade, but entity graph features need valid Neo4j settings.
- Supabase upload fails: check service key, bucket name, schema name, and storage policies.
