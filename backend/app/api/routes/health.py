import logging
from datetime import datetime, timezone
 
import httpx
import redis as redis_client
from fastapi import APIRouter
 
from app.config import settings
from app.db.connection import check_db_health
from app.models.responses import HealthResponse
 
logger = logging.getLogger(__name__)
router = APIRouter()
 
 
def _check_redis() -> str:
    try:
        r = redis_client.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        r.ping()
        return "ok"
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        return "unreachable"
 
 
def _check_neo4j() -> str:
    """'disabled' when graph opted out, 'ok' when reachable, else 'unreachable'.
    Never raises — the graph is best-effort and must not break health."""
    if not settings.NEO4J_ENABLED:
        return "disabled"
    try:
        from app.services import graph_service
        return "ok" if graph_service.is_available() else "unreachable"
    except Exception as exc:
        logger.warning("Neo4j health check failed: %s", exc)
        return "unreachable"
 
 
def _check_gemma() -> str:
    if not settings.GEMMA4_BASE_URL:
        return "not_configured"
    try:
        with httpx.Client(timeout=5) as client:
            # Try a lightweight endpoint — most LLM servers expose /health or /
            url = settings.GEMMA4_BASE_URL.rstrip("/") + "/health"
            r = client.get(url)
            return "ok" if r.status_code < 500 else "error"
    except Exception as exc:
        logger.warning("Gemma health check failed: %s", exc)
        return "unreachable"
 
 
@router.get("/health", response_model=HealthResponse, tags=["health"])
def health_check():
    db_status = "ok" if check_db_health() else "unreachable"
    redis_status = _check_redis()
    gemma_status = _check_gemma()
    neo4j_status = _check_neo4j()
 
    # neo4j is best-effort (degradation-safe) so it never drags overall health down.
    overall = (
        "ok"
        if db_status == "ok" and redis_status == "ok"
        else "degraded"
    )
 
    return HealthResponse(
        status=overall,
        api="ok",
        database=db_status,
        redis=redis_status,
        gemma_endpoint=gemma_status,
        neo4j=neo4j_status,
        timestamp=datetime.now(timezone.utc),
        embedding_model=settings.BGE_MODEL_NAME.split("/")[-1],
        reranker_name=settings.RERANKER_NAME.split("/")[-1],
        gemma_model=settings.GEMMA4_MODEL_NAME,
    )