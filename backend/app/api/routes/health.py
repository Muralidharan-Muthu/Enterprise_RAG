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
 
 
def _check_groq() -> str:
    base_url = settings.GROQ_BASE_URL or settings.GROQ_BASE_URL
    api_key = settings.GROQ_API_KEY or settings.GROQ_API_KEY
    if not base_url or not api_key:
        return "not_configured"
    try:
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "MultiStoreRAG/1.0"}
        with httpx.Client(timeout=5) as client:
            url = f"{base_url.rstrip('/')}/models"
            r = client.get(url, headers=headers)
            return "ok" if r.status_code == 200 else "error"
    except Exception as exc:
        logger.warning("Groq health check failed: %s", exc)
        return "unreachable"
 
 
@router.get("/health", response_model=HealthResponse, tags=["health"])
def health_check():
    db_status = "ok" if check_db_health() else "unreachable"
    redis_status = _check_redis()
    groq_status = _check_groq()
    neo4j_status = _check_neo4j()
 
    # neo4j is best-effort (degradation-safe) so it never drags overall health down.
    overall = (
        "ok"
        if db_status == "ok" and redis_status == "ok" and groq_status == "ok"
        else "degraded"
    )
 
    llm_model = settings.GROQ_MODEL_NAME
 
    return HealthResponse(
        status=overall,
        api="ok",
        database=db_status,
        redis=redis_status,
        groq_endpoint=groq_status,
        neo4j=neo4j_status,
        timestamp=datetime.now(timezone.utc),
        embedding_model=settings.BGE_MODEL_NAME.split("/")[-1],
        reranker_name=settings.RERANKER_NAME.split("/")[-1],
        groq_model=llm_model,
    )