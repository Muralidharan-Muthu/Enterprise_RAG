-- ============================================================
-- Multi-Store RAG Chatbot — Migration 015: Ingestion Worker/Code-Version Stamp
--
-- Apply manually via the Supabase SQL Editor (no Alembic in this project;
-- see CLAUDE.md "DB Layer"). Idempotent: every ADD COLUMN uses
-- IF NOT EXISTS, so re-running this file is always safe.
--
-- Context: a stray native Celery worker running stale/older code competed
-- with the Docker worker container on the same Redis queue, causing 50% of
-- identical re-ingestions of the same test document to SILENTLY produce
-- broken results (a multi-page table that should merge into 1 table_store
-- row instead stayed split into 3), while document_registry.status still
-- read 'completed' with zero error signal. These two columns give every
-- ingestion_jobs row a diagnosable "who touched this" stamp so a future
-- stale-worker incident is visible instead of silent. Point lookups/audit
-- display only — no indexes added (no concrete query need yet).
-- ============================================================

SET search_path TO multi_store_rag_working, public;

-- ------------------------------------------------------------
-- ingestion_jobs.worker_id
-- Identifies the specific worker PROCESS that touched this job — combines
-- hostname, PID and process-start timestamp so a native venv worker, a
-- Docker container, and successive container restarts are all
-- distinguishable from one another.
-- ------------------------------------------------------------
ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS worker_id TEXT;

COMMENT ON COLUMN multi_store_rag_working.ingestion_jobs.worker_id IS
    'Identifier of the worker process that handled this job: '
    '"<hostname>-<pid>-<process_start_unix_ts>". Computed once at worker '
    'module import time (app.core.worker_identity). Nullable — jobs run '
    'before this migration/stamping was added have no value.';

-- ------------------------------------------------------------
-- ingestion_jobs.code_version
-- Git short commit hash of the code that processed this job, so two workers
-- running different code versions against the same queue are distinguishable
-- after the fact.
-- ------------------------------------------------------------
ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS code_version TEXT;

COMMENT ON COLUMN multi_store_rag_working.ingestion_jobs.code_version IS
    'Git short commit hash ("git rev-parse --short HEAD") of the worker '
    'process that handled this job, or "unknown" if git was unavailable. '
    'Computed once at worker module import time '
    '(app.core.worker_identity). Nullable — jobs run before this '
    'migration/stamping was added have no value.';
