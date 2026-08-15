import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile, HTTPException, status, Body

from app.config import settings
from app.core.exceptions import FileTooLargeError, InvalidFileTypeError, JobNotFoundError
from app.db.connection import get_db
from app.db.repositories import pipeline_runs
from app.models.responses import (
    UploadResponse,
    JobStatusResponse,
    PipelineCreateResponse,
    PipelineFileResult,
    PipelineRunSummary,
    PipelineRunListResponse,
    PipelineDetail,
    PipelineDocumentDetail,
)

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",   # .docx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",         # .xlsx
    "text/html",
    "text/markdown",
}

# Filename-extension → MIME mapping used for browsers that send application/octet-stream
_EXT_TO_MIME: dict[str, str] = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    # .htm listed before .html so the inverse map (MIME → ext) resolves to .html
    ".htm":  "text/html",
    ".html": "text/html",
    ".md":   "text/markdown",
}

_SUPPORTED_EXTS = set(_EXT_TO_MIME.keys())
_GENERIC_MIMES = {"application/octet-stream", "text/plain", ""}


def _ext_for(filename: str, content_type: str) -> str:
    """Return the correct file extension ('.pdf', '.docx', …) for the upload.

    Priority:
    1. If content_type is a known explicit MIME, map it to an extension.
    2. Fall back to the filename's own suffix (lower-cased).
    3. Default to '.pdf' if nothing else matches.
    """
    ct = (content_type or "").strip().lower()
    _mime_to_ext = {v: k for k, v in _EXT_TO_MIME.items()}
    if ct in _mime_to_ext:
        return _mime_to_ext[ct]
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in _SUPPORTED_EXTS:
        return suffix
    return ".pdf"


def _is_allowed(filename: str, content_type: str) -> bool:
    """Return True if the file should be accepted.

    Accepts:
    - Any content_type in ALLOWED_MIME_TYPES.
    - Generic MIME types (octet-stream, empty) when the filename extension is supported
      (browsers commonly mislabel Office files as application/octet-stream).
    """
    ct = (content_type or "").strip().lower()
    if ct in ALLOWED_MIME_TYPES:
        return True
    if ct in _GENERIC_MIMES:
        suffix = os.path.splitext(filename)[1].lower()
        return suffix in _SUPPORTED_EXTS
    return False


_SUPPORTED_TYPES_MSG = "PDF, DOCX, PPTX, XLSX, HTML, Markdown"


# ── Internal helpers ───────────────────────────────────────────────────────
def _register_and_dispatch(
    content: bytes,
    filename: str,
    content_type: str,
    pipeline_run_id: Optional[str] = None,
) -> tuple[str, str]:
    """
    Persist a supported document, register it (+ ingestion job), dispatch the
    Celery pipeline. Returns (document_id, job_id). Caller validates type/size first.
    """
    # ── Upload to Supabase Storage ────────────────────────────
    ext = _ext_for(filename, content_type)
    safe_name = f"{uuid.uuid4()}{ext}"
    storage_path = f"documents/{safe_name}"
    bucket = settings.SUPABASE_STORAGE_BUCKET

    try:
        from app.services.supabase_storage import upload_file
        upload_file(bucket, storage_path, content, content_type or "application/octet-stream")
    except Exception as exc:
        logger.error("Storage upload failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload file to storage",
        )

    document_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO multi_store_rag_working.document_registry
                    (id, filename, original_filename, file_size_bytes, mime_type,
                     storage_path, storage_bucket, status, pipeline_run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'uploaded', %s)
                """,
                (document_id, safe_name, filename, len(content), content_type,
                 storage_path, bucket, pipeline_run_id),
            )
            cur.execute(
                """
                INSERT INTO multi_store_rag_working.ingestion_jobs
                    (id, document_id, current_stage)
                VALUES (%s, %s, 'queued')
                """,
                (job_id, document_id),
            )

    try:
        # Dispatch by task name via send_task — importing the orchestrator
        # module would trigger embedding_service.warmup(), loading the 1.3 GB
        # BGE model into the API process. The worker owns that model.
        from app.core.background_tasks import celery_app
        task = celery_app.send_task(
            "app.services.ingestion_orchestrator.ingest_document",
            args=[document_id, storage_path, job_id],
            task_id=job_id,
            queue="ingestion",
        )
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE multi_store_rag_working.ingestion_jobs SET celery_task_id = %s WHERE id = %s",
                    (task.id, job_id),
                )
    except Exception as exc:
        logger.error("Failed to dispatch ingestion task for %s: %s", document_id, exc)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE multi_store_rag_working.document_registry
                       SET status = 'failed', error_stage = 'dispatch', error_message = %s
                       WHERE id = %s""",
                    (str(exc), document_id),
                )
        raise

    return document_id, job_id


def _record_failed_file(
    filename: str,
    content_type: str,
    file_size: int,
    error: str,
    pipeline_run_id: Optional[str] = None,
) -> str:
    """
    Register a rejected file (wrong type / too large) as a failed document so it
    still appears in the pipeline run's counts and history. Returns document_id.
    """
    document_id = str(uuid.uuid4())
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO multi_store_rag_working.document_registry
                    (id, filename, original_filename, file_size_bytes, mime_type,
                     status, error_stage, error_message, pipeline_run_id)
                VALUES (%s, %s, %s, %s, %s, 'failed', 'validation', %s, %s)
                """,
                (document_id, filename, filename, file_size,
                 content_type or "unknown", error, pipeline_run_id),
            )
    return document_id


# ── Single-file upload (legacy / backward compatible) ──────────────────────
@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(file: UploadFile = File(...)):
    """Accept a single supported document, register it, and dispatch the Celery pipeline task."""
    fname = file.filename or "document"
    if not _is_allowed(fname, file.content_type or ""):
        raise InvalidFileTypeError(file.content_type or "unknown")

    content = await file.read()
    file_size = len(content)
    if file_size > settings.max_upload_bytes:
        size_mb = file_size / (1024 * 1024)
        raise FileTooLargeError(size_mb, settings.MAX_UPLOAD_SIZE_MB)

    try:
        document_id, job_id = _register_and_dispatch(
            content, fname, file.content_type
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start ingestion pipeline",
        )

    return UploadResponse(
        document_id=document_id,
        job_id=job_id,
        filename=fname,
    )


# ── Pipeline run: batch upload with taxonomy metadata ──────────────────────
@router.post("/pipeline", response_model=PipelineCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_pipeline(
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    description: Optional[str] = Form(None),
    source: str = Form("local"),
    domain: Optional[str] = Form(None),
    sub_domain: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    sub_category: Optional[str] = Form(None),
):
    """
    Create a pipeline run and ingest a batch of files under it.
    Valid PDFs are dispatched to the Celery pipeline; unsupported or oversized
    files are recorded as failed so they still appear in the run's counts.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    run_id = pipeline_runs.create_run(
        name=name.strip(),
        source=source,
        description=description,
        domain=domain,
        sub_domain=sub_domain,
        category=category,
        sub_category=sub_category,
    )

    results: list[PipelineFileResult] = []
    queued = 0
    failed = 0

    for file in files:
        fname = file.filename or "document"
        content = await file.read()
        file_size = len(content)

        # Validate type
        if not _is_allowed(fname, file.content_type or ""):
            err = (
                f"Unsupported file type: {file.content_type or 'unknown'}. "
                f"Accepted: {_SUPPORTED_TYPES_MSG}"
            )
            doc_id = _record_failed_file(fname, file.content_type, file_size, err, run_id)
            results.append(PipelineFileResult(document_id=doc_id, filename=fname, status="failed", error=err))
            failed += 1
            continue

        # Validate size
        if file_size > settings.max_upload_bytes:
            err = f"File exceeds {settings.MAX_UPLOAD_SIZE_MB}MB limit"
            doc_id = _record_failed_file(fname, file.content_type, file_size, err, run_id)
            results.append(PipelineFileResult(document_id=doc_id, filename=fname, status="failed", error=err))
            failed += 1
            continue

        # Dispatch
        try:
            doc_id, job_id = _register_and_dispatch(content, fname, file.content_type, run_id)
            results.append(PipelineFileResult(document_id=doc_id, job_id=job_id, filename=fname, status="queued"))
            queued += 1
        except Exception as exc:
            results.append(PipelineFileResult(
                document_id="", filename=fname, status="failed",
                error="Failed to start ingestion pipeline",
            ))
            failed += 1
            logger.error("Pipeline %s: dispatch failed for %s: %s", run_id, fname, exc)

    return PipelineCreateResponse(
        pipeline_run_id=run_id,
        name=name.strip(),
        files_found=len(files),
        files_queued=queued,
        files_failed=failed,
        files=results,
    )


@router.get("/pipelines", response_model=PipelineRunListResponse)
def list_pipelines(page: int = 1, limit: int = 20):
    """List pipeline runs (newest first) for the run-history table."""
    page = max(1, page)
    limit = min(max(1, limit), 100)
    offset = (page - 1) * limit

    items, total = pipeline_runs.list_runs(limit=limit, offset=offset)

    return PipelineRunListResponse(
        items=[PipelineRunSummary(**it) for it in items],
        total=total,
        page=page,
        pages=max(1, (total + limit - 1) // limit),
        limit=limit,
    )


@router.get("/pipelines/{run_id}", response_model=PipelineDetail)
def get_pipeline(run_id: str):
    """Full pipeline detail — overview + per-document stage timings."""
    data = pipeline_runs.get_run(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    docs = [PipelineDocumentDetail(**d) for d in data.pop("documents")]
    return PipelineDetail(**data, documents=docs)


@router.patch("/pipelines/{run_id}", status_code=status.HTTP_200_OK)
def rename_pipeline(run_id: str, body: dict = Body(...)):
    """Rename a pipeline run."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name must not be empty")
    found = pipeline_runs.rename_run(run_id, name)
    if not found:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return {"run_id": run_id, "name": name}


@router.post("/documents/{document_id}/reprocess", status_code=status.HTTP_200_OK)
def reprocess_document(document_id: str):
    """Re-dispatch the ingestion pipeline for an existing document inside a new pipeline run."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT storage_path, storage_bucket, original_filename FROM multi_store_rag_working.document_registry WHERE id = %s",
                (document_id,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    storage_path, _bucket, original_filename = row

    # Create a new pipeline run so the user can watch progress on /upload/{run_id}
    run_id = pipeline_runs.create_run(
        name=f"Reprocess — {original_filename}",
        description="Triggered from Documents page",
    )

    job_id = str(uuid.uuid4())

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE multi_store_rag_working.document_registry
                   SET status = 'uploaded', pipeline_run_id = %s
                   WHERE id = %s""",
                (run_id, document_id),
            )
            cur.execute(
                "INSERT INTO multi_store_rag_working.ingestion_jobs (id, document_id, current_stage) VALUES (%s, %s, 'queued')",
                (job_id, document_id),
            )

    try:
        from app.core.background_tasks import celery_app
        task = celery_app.send_task(
            "app.services.ingestion_orchestrator.ingest_document",
            args=[document_id, storage_path, job_id],
            task_id=job_id,
            queue="ingestion",
        )
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE multi_store_rag_working.ingestion_jobs SET celery_task_id = %s WHERE id = %s",
                    (task.id, job_id),
                )
    except Exception as exc:
        logger.error("Failed to dispatch reprocess task for %s: %s", document_id, exc)
        raise HTTPException(status_code=500, detail="Failed to dispatch ingestion task")

    return {"document_id": document_id, "job_id": job_id, "pipeline_run_id": run_id, "status": "queued"}


@router.delete("/pipelines", status_code=status.HTTP_200_OK)
def clear_all_pipelines():
    """Delete all pipeline runs. Documents and their chunks are preserved."""
    deleted = pipeline_runs.delete_all_runs()
    return {"deleted": deleted}


@router.delete("/pipelines/{run_id}", status_code=status.HTTP_200_OK)
def delete_pipeline(run_id: str):
    """Delete a pipeline run. Documents and their Supabase Storage files are preserved."""
    unlinked = pipeline_runs.delete_run(run_id)
    if unlinked is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    return {"deleted": True, "run_id": run_id, "documents_unlinked": unlinked}


@router.get("/status/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    """Poll the ingestion pipeline progress for a given job_id."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, document_id, current_stage, stage_progress,
                       total_chunks, processed_chunks, stage_timings,
                       error_message, queued_at, started_at, completed_at, duration_seconds,
                       stage_detail
                FROM multi_store_rag_working.ingestion_jobs
                WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()

    if not row:
        raise JobNotFoundError(job_id)

    return JobStatusResponse(
        job_id=str(row[0]),
        document_id=str(row[1]),
        current_stage=row[2],
        stage_progress=row[3] or 0,
        total_chunks=row[4],
        processed_chunks=row[5] or 0,
        stage_timings=row[6] or {},
        error_message=row[7],
        queued_at=row[8],
        started_at=row[9],
        completed_at=row[10],
        duration_seconds=row[11],
        stage_detail=row[12] or {},
    )
