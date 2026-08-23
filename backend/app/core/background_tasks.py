import sys

from celery import Celery
from app.config import settings

celery_app = Celery(
    "multi_store_rag",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.services.ingestion_orchestrator",
        # Feature 1.6: two-task staged ingestion chain (parse → embed).
        # Always registered so workers can discover task names regardless of
        # INGESTION_STAGED_ENABLED — the flag only controls dispatch, not
        # task registration.
        "app.services.ingestion_tasks",
        # Feature 1.3: GraphRAG community recompute task (debounced, async).
        # Always registered so workers can receive the task; actual computation
        # is gated on GRAPHRAG_ENABLED inside the task body.
        "app.services.community_service",
    ],
)

import ssl

broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE} if settings.CELERY_BROKER_URL.startswith("rediss://") else None
redis_backend_use_ssl = {"ssl_cert_reqs": ssl.CERT_NONE} if settings.CELERY_RESULT_BACKEND.startswith("rediss://") else None

celery_app.conf.update(
    broker_use_ssl=broker_use_ssl,
    redis_backend_use_ssl=redis_backend_use_ssl,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Route the ingestion task to a dedicated queue AND make that queue the
    # worker default. Without task_default_queue the bare `celery ... worker`
    # command (per CLAUDE.md) listens only on "celery" while tasks land in
    # "ingestion" — so every upload hangs in Queued forever. Setting the
    # default here means `celery ... worker` (no -Q flag) consumes ingestion.
    task_default_queue="ingestion",
    task_routes={
        # Legacy monolithic task — kept for back-compat / INGESTION_STAGED_ENABLED=False
        "app.services.ingestion_orchestrator.ingest_document": {"queue": "ingestion"},
        # Feature 1.6: parse task runs on the "parse" queue (CPU-only, no BGE model)
        "app.services.ingestion_tasks.parse_document_task": {"queue": "parse"},
        # Feature 1.6: embed task runs on the "embed" queue (rate-limited, BGE model)
        # Rate limit is set on the task itself via settings.EMBED_QUEUE_RATE_LIMIT.
        "app.services.ingestion_tasks.chunk_embed_store_task": {"queue": "embed"},
        # Feature 1.3: community recompute runs on a dedicated low-priority queue
        # so it never blocks ingestion or query tasks.
        "app.services.community_service.recompute_communities_task": {"queue": "graph"},
    },
)

# Windows: the default 'prefork' pool uses billiard multiprocessing with
# shared-memory semaphores that Windows blocks (PermissionError WinError 5),
# crashing every worker child so tasks hang forever in the queue. Force the
# 'solo' pool on Windows so tasks run in the main process. Linux/Docker keep
# prefork for concurrency.
#
# Feature 1.6 — Windows multi-queue setup:
#   Run TWO separate worker processes so parse and embed queues are served:
#     celery -A app.core.background_tasks worker -Q parse -c 4 --loglevel=info
#     celery -A app.core.background_tasks worker -Q embed -c 1 --loglevel=info
#   Linux/Docker prefork workers can serve all queues in a single process:
#     celery -A app.core.background_tasks worker -Q parse,embed,ingestion -c 4
if sys.platform == "win32":
    celery_app.conf.worker_pool = "solo"
