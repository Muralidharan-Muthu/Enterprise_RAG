import json
import logging

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def update_job(job_id: str, stage: str, progress: int = 0, **kwargs) -> None:
    """Update the current stage and progress of an ingestion job."""
    sets = ["current_stage = %s", "stage_progress = %s", "updated_at = NOW()"]
    params: list = [stage, progress]

    if stage != "queued" and "started_at" not in kwargs:
        # Auto-set started_at on first real stage
        sets.append("started_at = COALESCE(started_at, NOW())")

    for field in ("total_chunks", "processed_chunks", "celery_task_id",
                  "error_message", "error_traceback", "is_retryable",
                  "worker_id", "code_version"):
        if field in kwargs:
            sets.append(f"{field} = %s")
            params.append(kwargs[field])

    if "stage_timing" in kwargs:
        # Merge a single stage timing into the JSONB column
        stage_name, duration = kwargs["stage_timing"]
        sets.append(
            "stage_timings = stage_timings || %s::jsonb"
        )
        params.append(json.dumps({stage_name: round(duration, 3)}))

    if "stage_detail" in kwargs:
        # Merge granular per-stage detail (e.g. parsing per-page workload map)
        # under a top-level key so the frontend can show live, accurate detail.
        sets.append("stage_detail = COALESCE(stage_detail, '{}'::jsonb) || %s::jsonb")
        params.append(json.dumps(kwargs["stage_detail"]))

    if stage == "done":
        sets.append("completed_at = NOW()")
        sets.append(
            "duration_seconds = EXTRACT(EPOCH FROM (NOW() - queued_at))"
        )

    params.append(job_id)
    sql = f"UPDATE multi_store_rag_working.ingestion_jobs SET {', '.join(sets)} WHERE id = %s"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
