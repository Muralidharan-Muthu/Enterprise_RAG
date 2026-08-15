"""
Graph Store API — introspection and testing endpoints.

Endpoints:
    GET  /graph/status                  — Neo4j connectivity + node/edge counts
    GET  /graph/entities                — paginated Entity node list
    GET  /graph/search?query=...&hops=2 — multi-hop entity search (raw results)
    POST /graph/rebuild/{document_id}   — trigger graph rebuild for a document
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Response models ───────────────────────────────────────────────────────────

class GraphStatusResponse(BaseModel):
    enabled: bool
    available: bool
    uri: str
    database: str
    graphrag_enabled: bool
    node_counts: dict
    message: str


class EntityItem(BaseModel):
    key: str
    name: str
    type: str
    description: str
    doc_count: int


class GraphSearchHit(BaseModel):
    pg_id: str
    document_id: str
    store: str
    score: float
    hop_distance: int


class GraphSearchResponse(BaseModel):
    query: str
    entity_keys_used: list[str]
    hops: int
    hits: list[GraphSearchHit]
    total_hits: int


class RebuildResponse(BaseModel):
    document_id: str
    status: str
    message: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status", response_model=GraphStatusResponse, summary="Neo4j graph store connectivity status")
async def graph_status():
    """Return Neo4j connectivity status and node/edge counts.

    Works even when NEO4J_ENABLED=false (returns available=false with zero counts).
    """
    from app.services import graph_service

    enabled = settings.NEO4J_ENABLED
    available = await asyncio.to_thread(graph_service.is_available)

    node_counts = {}
    if available:
        node_counts = await asyncio.to_thread(graph_service.get_graph_stats)
    else:
        node_counts = {"entities": 0, "chunks": 0, "documents": 0, "communities": 0, "relationships": 0}

    if not enabled:
        message = "Neo4j is disabled (NEO4J_ENABLED=false). Set NEO4J_ENABLED=true and restart."
    elif not available:
        message = f"Neo4j is enabled but unreachable at {settings.NEO4J_URI}. Check credentials and network."
    else:
        e = node_counts.get("entities", 0)
        c = node_counts.get("chunks", 0)
        d = node_counts.get("documents", 0)
        r = node_counts.get("relationships", 0)
        com = node_counts.get("communities", 0)
        message = (
            f"Connected to Neo4j Aura. Graph contains {d} documents, "
            f"{e} entities, {r} relationships, {com} communities "
            f"(legacy chunk nodes: {c})."
        )

    return GraphStatusResponse(
        enabled=enabled,
        available=available,
        uri=settings.NEO4J_URI,
        database=settings.NEO4J_DATABASE or "(default)",
        graphrag_enabled=settings.GRAPHRAG_ENABLED,
        node_counts=node_counts,
        message=message,
    )


@router.get("/entities", response_model=list[EntityItem], summary="List entities in the graph")
async def list_graph_entities(
    limit: int = Query(default=50, ge=1, le=200, description="Max entities to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
):
    """Return a page of Entity nodes, ordered by document frequency (most-mentioned first).

    Returns empty list when Neo4j is unavailable.
    """
    from app.services import graph_service

    if not settings.NEO4J_ENABLED:
        return []

    entities = await asyncio.to_thread(graph_service.list_entities, limit, offset)
    return [
        EntityItem(
            key=e["key"],
            name=e["name"],
            type=e.get("type") or "misc",
            description=e.get("description") or "",
            doc_count=e.get("doc_count") or 0,
        )
        for e in entities
    ]


@router.get("/search", response_model=GraphSearchResponse, summary="Multi-hop graph search")
async def graph_search(
    query: str = Query(..., min_length=1, max_length=1000, description="Natural language query"),
    hops: int = Query(default=2, ge=0, le=3, description="Number of RELATES_TO hops to traverse (0=seed only, 2=default)"),
    limit: int = Query(default=20, ge=1, le=100, description="Max chunk hits to return"),
):
    """Run a multi-hop entity search against the Neo4j graph.

    Extracts entities from the query, finds the chunks (pg_ids) that mention them
    via MENTIONED_IN, then traverses entity→entity relationship edges up to `hops`
    steps to reach related entities in other documents and returns their chunks.
    Returns raw graph hits with hop_distance for inspection.

    Useful for testing whether the graph store is populated and correctly
    connecting entities across documents.
    """
    if not settings.NEO4J_ENABLED:
        return GraphSearchResponse(
            query=query, entity_keys_used=[], hops=hops, hits=[], total_hits=0,
        )

    from app.services import graph_service
    if not await asyncio.to_thread(graph_service.is_available):
        return GraphSearchResponse(
            query=query, entity_keys_used=[], hops=hops, hits=[], total_hits=0,
        )

    try:
        from app.services.entity_service import extract_entities, canonicalize
        raw_entities = await asyncio.to_thread(
            extract_entities, query, settings.GRAPHRAG_LOCAL_TOP_ENTITIES,
        )
        entity_keys = []
        seen: set = set()
        for e in raw_entities:
            k = canonicalize(e.get("name", ""))
            if k and k not in seen:
                seen.add(k)
                entity_keys.append(k)
    except Exception as exc:
        logger.warning("graph_search entity extraction failed: %s", exc)
        entity_keys = []

    if not entity_keys:
        return GraphSearchResponse(
            query=query, entity_keys_used=[], hops=hops, hits=[], total_hits=0,
        )

    hits_raw = await asyncio.to_thread(
        graph_service.local_neighborhood, entity_keys, hops, limit,
    )

    hits = [
        GraphSearchHit(
            pg_id=h["pg_id"],
            document_id=h["document_id"],
            store=h.get("store") or "",
            score=round(h["score"], 4),
            hop_distance=h.get("hop_distance", 0),
        )
        for h in hits_raw
    ]

    return GraphSearchResponse(
        query=query,
        entity_keys_used=entity_keys,
        hops=hops,
        hits=hits,
        total_hits=len(hits),
    )


@router.post("/rebuild/{document_id}", response_model=RebuildResponse, summary="Rebuild the entity graph for a document from its stored chunks")
async def rebuild_document_graph(document_id: str):
    """Rebuild this document's entity graph straight from its already-stored
    Postgres chunks — no re-upload/re-parse needed.

    Use this when the graph stage silently failed to persist during the
    original ingestion (e.g. a dropped Neo4j connection mid-run left the
    ingestion log reporting success while nothing actually landed in Neo4j),
    or after fixing a graph-building bug and wanting existing documents to
    pick it up. Clears this document's prior graph contribution first (without
    touching entities other documents still share), then re-extracts and
    re-writes, then verifies the write against Neo4j directly.
    """
    if not settings.NEO4J_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="Neo4j is disabled (NEO4J_ENABLED=false). Enable it first.",
        )
    if not settings.GRAPHRAG_ENABLED:
        raise HTTPException(
            status_code=400,
            detail="GraphRAG is disabled (GRAPHRAG_ENABLED=false). Enable it first.",
        )

    from app.services import graph_service
    if not await asyncio.to_thread(graph_service.is_available):
        raise HTTPException(
            status_code=503,
            detail="Neo4j is unavailable. Check connection and credentials.",
        )

    try:
        from app.services.graph_build_service import rebuild_graph_for_document
        counts = await asyncio.to_thread(rebuild_graph_for_document, document_id)

        if counts.get("error"):
            raise HTTPException(status_code=404, detail=counts["error"])

        return RebuildResponse(
            document_id=document_id,
            status="rebuilt",
            message=(
                f"Graph rebuilt for document '{document_id}': "
                f"{counts.get('chunks_processed', 0)} chunks written, "
                f"{counts.get('entities_total', 0)} entities, "
                f"{counts.get('relationships_total', 0)} relationships, "
                f"{counts.get('chunks_failed_to_write', 0)} chunks failed to write, "
                f"{counts.get('verified_entities', 0)} entities verified present in Neo4j."
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Graph rebuild failed for %s: %s", document_id, exc)
        raise HTTPException(status_code=500, detail=f"Graph rebuild failed: {exc}")
