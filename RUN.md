# Running Multi-Store RAG Chatbot

Two ways to run. **Windows one-click is the fast path** for dev.

---

## Option A — Windows one-click (recommended for dev)

Infra (Redis + Neo4j) runs in Docker; API + Worker + Frontend run natively for fast reload.

```
git pull origin main
setup.bat     <- ONCE after a pull (venv, pip install, npm install, docker pull)
run.bat       <- start everything (opens 3 windows: API, Worker, Frontend)
stop.bat      <- stop everything
```

Requires: **Docker Desktop running**, **Python 3.11+**, **Node 18+** on PATH.

After `run.bat`:
| Service   | URL                              | Notes |
|-----------|----------------------------------|-------|
| Frontend  | http://localhost:3000            | upload UI |
| API docs  | http://localhost:8000/docs       | Swagger |
| API health| http://localhost:8000/api/v1/health | all-green = ready |
| Neo4j     | http://localhost:7474            | login `neo4j` / `multi-store-graph` |

> The Worker window loads the 1.3 GB BGE model on start (~30–60 s).
> Wait for `celery@... ready.` before uploading, or jobs sit in "Queued".

---

## Option B — Full Docker (everything in containers)

```
docker compose up          # redis, neo4j, api, worker, frontend
docker compose up -d       # detached
docker compose down        # stop
```

First build downloads the BGE model into the `model_cache` volume (slow once, cached after).

---

## Required env files (NOT in git)

Create `.env` in the repo root (single source of truth — no `backend/.env`):

```
# Supabase (Postgres + Storage)
DATABASE_URL=postgresql://...
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=...

# Gemma 4 (CDAC OpenAI-compatible endpoint)
GEMMA4_BASE_URL=...
GEMMA4_API_KEY=...

# Redis / Celery (native hybrid uses localhost)
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

# Neo4j (cross-document knowledge graph)
NEO4J_ENABLED=true
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=multi-store-graph
```

---

## Manual commands (if you prefer no .bat)

```bash
# Infra
docker compose up -d redis neo4j

# API            (backend/, venv active)
uvicorn app.main:app --reload --port 8000

# Worker         (backend/, venv active) - MUST include the ingestion queue
celery -A app.core.background_tasks worker --loglevel=info -Q ingestion,celery

# Frontend
cd frontend && npm run dev
```

> Worker queue gotcha: uploads stuck in "Queued" usually mean the worker is on
> the wrong queue. Always start it with `-Q ingestion,celery`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Upload stuck "Queued" forever | Worker not on `ingestion` queue → restart with `-Q ingestion,celery` |
| API `Error 10061` on start | Redis not up → `docker compose up -d redis` |
| Worker slow to accept jobs | BGE model loading (~30–60 s), wait for `ready.` |
| Neo4j unreachable | `docker compose up -d neo4j`, check http://localhost:7474 |
| Frontend SWC / Jest worker crash | delete `frontend/.next`, re-run `npm run dev` |
