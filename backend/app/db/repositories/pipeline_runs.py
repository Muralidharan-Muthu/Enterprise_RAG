import logging
import uuid
from typing import Optional

from app.db.connection import get_db

logger = logging.getLogger(__name__)


def create_run(
    name: str,
    source: str = "local",
    description: Optional[str] = None,
    domain: Optional[str] = None,
    sub_domain: Optional[str] = None,
    category: Optional[str] = None,
    sub_category: Optional[str] = None,
) -> str:
    """Insert a pipeline_runs row and return its id."""
    run_id = str(uuid.uuid4())
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO multi_store_rag_working.pipeline_runs
                    (id, name, description, source)
                VALUES (%s, %s, %s, %s)
                """,
                (run_id, name, description, source),
            )
    return run_id


def get_run(run_id: str) -> dict | None:
    """Return full pipeline detail with all documents and their job status."""
    with get_db() as conn:
        with conn.cursor() as cur:
            # Pipeline overview
            cur.execute(
                """
                SELECT id, name, description, source, started_at, created_at,
                       files_found, files_processed, files_failed, status
                FROM multi_store_rag_working.pipeline_run_overview
                WHERE id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return None

            run = {
                "id": str(row[0]), "name": row[1], "description": row[2],
                "source": row[3],
                "started_at": row[4], "created_at": row[5],
                "files_found": row[6] or 0, "files_processed": row[7] or 0,
                "files_failed": row[8] or 0, "status": row[9],
            }

            # Documents + job status
            cur.execute(
                """
                SELECT
                  dr.id, dr.original_filename, dr.file_size_bytes, dr.status,
                  dr.document_type, dr.router_confidence, dr.doc_title,
                  dr.page_count, dr.word_count, dr.error_stage, dr.error_message,
                  ij.id, ij.current_stage, ij.stage_progress, ij.total_chunks,
                  ij.processed_chunks, ij.stage_timings,
                  ij.queued_at, ij.started_at, ij.completed_at, ij.duration_seconds,
                  COALESCE(vs.n, 0), COALESCE(ts.n, 0),
                  COALESCE(cs.n, 0), 0,
                  ij.stage_detail
                FROM multi_store_rag_working.document_registry dr
                LEFT JOIN LATERAL (
                    SELECT * FROM multi_store_rag_working.ingestion_jobs
                    WHERE document_id = dr.id
                    ORDER BY queued_at DESC NULLS LAST
                    LIMIT 1
                ) ij ON TRUE
                LEFT JOIN (SELECT document_id, COUNT(*) n FROM multi_store_rag_working.vector_store  GROUP BY document_id) vs ON vs.document_id = dr.id
                LEFT JOIN (SELECT document_id, COUNT(*) n FROM multi_store_rag_working.table_store    GROUP BY document_id) ts ON ts.document_id = dr.id
                LEFT JOIN (SELECT document_id, COUNT(*) n FROM multi_store_rag_working.clause_store   GROUP BY document_id) cs ON cs.document_id = dr.id
                WHERE dr.pipeline_run_id = %s
                ORDER BY dr.created_at ASC
                """,
                (run_id,),
            )
            docs = []
            for r in cur.fetchall():
                docs.append({
                    "document_id": str(r[0]), "original_filename": r[1],
                    "file_size_bytes": r[2] or 0, "doc_status": r[3],
                    "document_type": r[4], "router_confidence": r[5],
                    "doc_title": r[6], "page_count": r[7], "word_count": r[8],
                    "error_stage": r[9], "error_message": r[10],
                    "job_id": str(r[11]) if r[11] else None,
                    "current_stage": r[12], "stage_progress": r[13] or 0,
                    "total_chunks": r[14], "processed_chunks": r[15] or 0,
                    "stage_timings": r[16] or {}, "queued_at": r[17],
                    "started_at": r[18], "completed_at": r[19],
                    "duration_seconds": r[20],
                    "vector_chunks": int(r[21]), "table_count": int(r[22]),
                    "clause_count": int(r[23]), "research_chunks": 0,
                    "stage_detail": r[25] or {},
                })

            run["documents"] = docs
            return run


def rename_run(run_id: str, name: str) -> bool:
    """Rename a pipeline run. Returns False if the run does not exist."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE multi_store_rag_working.pipeline_runs SET name = %s WHERE id = %s",
                (name.strip(), run_id),
            )
            return cur.rowcount > 0


def delete_run(run_id: str) -> int | None:
    """
    Detach all documents from this pipeline run (pipeline_run_id → NULL), then
    delete the pipeline_run row. Documents and their Supabase Storage files are
    kept intact. Returns None if the run does not exist, otherwise the count of
    detached documents.
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM multi_store_rag_working.pipeline_runs WHERE id = %s",
                (run_id,),
            )
            if not cur.fetchone():
                return None

            cur.execute(
                """
                UPDATE multi_store_rag_working.document_registry
                SET pipeline_run_id = NULL
                WHERE pipeline_run_id = %s
                """,
                (run_id,),
            )
            unlinked = cur.rowcount

            cur.execute(
                "DELETE FROM multi_store_rag_working.pipeline_runs WHERE id = %s",
                (run_id,),
            )
    return unlinked


def delete_all_runs() -> int:
    """
    Detach all documents from every pipeline run (pipeline_run_id → NULL), then
    delete all pipeline_run rows. Documents and their stored chunks are preserved.
    Returns the total number of pipeline runs deleted.
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE multi_store_rag_working.document_registry SET pipeline_run_id = NULL WHERE pipeline_run_id IS NOT NULL"
            )
            cur.execute("DELETE FROM multi_store_rag_working.pipeline_runs")
            return cur.rowcount


def list_runs(limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """Return (rows, total) from pipeline_run_overview, newest first."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM multi_store_rag_working.pipeline_run_overview")
            total = cur.fetchone()[0]

            cur.execute(
                """
                SELECT id, name, description, source, started_at, created_at,
                       files_found, files_processed, files_failed, status
                FROM multi_store_rag_working.pipeline_run_overview
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()

    items = [
        {
            "id": str(r[0]),
            "name": r[1],
            "description": r[2],
            "source": r[3],
            "started_at": r[4],
            "created_at": r[5],
            "files_found": r[6] or 0,
            "files_processed": r[7] or 0,
            "files_failed": r[8] or 0,
            "status": r[9],
        }
        for r in rows
    ]
    return items, total
