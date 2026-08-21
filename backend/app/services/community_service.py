"""
Community detection + summarization — Feature 1.3 GraphRAG.

recompute_communities():
    1. Fetch entity graph from Neo4j (nodes + weighted RELATES_TO edges)
    2. Detect communities with Louvain (python-louvain) or label propagation fallback
    3. Summarize each community via Gemma
    4. Optionally embed summaries via BGE
    5. Write communities to Neo4j with a bumped version (atomic-ish swap)

networkx and python-louvain are imported LAZILY inside functions so the app
starts cleanly when GRAPHRAG is off or packages are not installed.

The recompute_communities_task Celery task includes a Redis-based debounce:
it rebuilds only if dirty AND (elapsed >= GRAPHRAG_COMMUNITY_MIN_INTERVAL_SEC
OR dirty_doc_count >= GRAPHRAG_COMMUNITY_DIRTY_DOCS).

run_graph_stage() in graph_build_service just sets the dirty flag + enqueues
this task; the task itself decides whether to actually rebuild.
"""
from __future__ import annotations

import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

# Redis keys (same as in graph_build_service._signal_community_recompute)
_DIRTY_KEY = "graphrag:community:dirty_docs"
_TS_KEY = "graphrag:community:last_build_ts"
_VERSION_KEY = "graphrag:community:version"
# Single-flight lock: only one recompute runs at a time. Duplicate/redelivered
# tasks that find the lock held skip immediately (they get acked and drain from
# the queue) instead of each re-running the full-graph, per-community Gemma
# summarization — which is what previously flooded the CDAC endpoint with a
# continuous stream of chat/completions calls after every worker restart.
_LOCK_KEY = "graphrag:community:recompute_lock"


# ── Public task-callable entry point ─────────────────────────────────────────

def recompute_communities(level_summaries: bool = True) -> int:
    """Fetch entity graph, detect communities, summarize, write to Neo4j.

    Returns the number of communities written (0 on skip/failure).
    """
    if not settings.GRAPHRAG_ENABLED:
        logger.debug("recompute_communities skipped (GRAPHRAG_ENABLED=False)")
        return 0

    from app.services import graph_service
    if not graph_service.is_available():
        logger.debug("recompute_communities skipped (Neo4j unavailable)")
        return 0

    logger.info("recompute_communities: starting")
    t0 = time.monotonic()

    try:
        # 1. Fetch graph
        nodes, edges = graph_service.fetch_entity_graph()
        if not nodes:
            logger.info("recompute_communities: no entity nodes found, skipping")
            return 0

        logger.info("recompute_communities: %d nodes, %d edges", len(nodes), len(edges))

        # 2. Detect communities
        assignments = _detect_communities(nodes, edges)
        if not assignments:
            logger.info("recompute_communities: no communities detected")
            return 0

        # 3. Summarize each community
        community_ids = set(assignments.values())
        logger.info("recompute_communities: %d communities detected", len(community_ids))

        # Build community member lists
        community_members: dict[int, list[str]] = {}
        for entity_key, cid in assignments.items():
            community_members.setdefault(cid, []).append(entity_key)

        # Decide which communities are worth an LLM summary. Only communities
        # with >= MIN_SIZE members qualify, and at most MAX_SUMMARIES of them
        # (largest first). Everything else gets a cheap member-list summary with
        # NO Groq call — this is what stops a fragmented partition (hundreds of
        # singleton/pair communities) from firing one chat/completions per node.
        min_size = settings.GRAPHRAG_COMMUNITY_MIN_SIZE
        max_summaries = settings.GRAPHRAG_COMMUNITY_MAX_SUMMARIES
        ranked = sorted(
            community_ids,
            key=lambda c: len(community_members.get(c, [])),
            reverse=True,
        )
        groq_targets = {
            cid for cid in ranked
            if len(community_members.get(cid, [])) >= min_size
        }
        if max_summaries > 0:
            groq_targets = set(
                [c for c in ranked if c in groq_targets][:max_summaries]
            )
        logger.info(
            "recompute_communities: %d/%d communities qualify for LLM summary "
            "(min_size=%d, cap=%d); the rest get cheap summaries",
            len(groq_targets), len(community_ids), min_size, max_summaries,
        )

        summaries: dict = {}
        for cid in community_ids:
            members = community_members.get(cid, [])
            try:
                summary_info = _summarize_community(
                    cid, members, edges, use_groq=cid in groq_targets,
                )
                if level_summaries and summary_info.get("summary"):
                    # Optionally embed the summary
                    try:
                        from app.services.embedding_service import embed_passages
                        emb = embed_passages([summary_info["summary"]])
                        summary_info["embedding"] = emb[0].tolist()
                    except Exception as emb_exc:
                        logger.debug("Community summary embedding failed: %s", emb_exc)
                        summary_info["embedding"] = None
                summaries[cid] = summary_info
            except Exception as exc:
                logger.warning("Community %d summarization failed: %s", cid, exc)
                summaries[cid] = {"title": f"Community {cid}", "summary": "", "embedding": None}

        # 4. Bump version
        version = _bump_version()

        # 5. Write to Neo4j
        graph_service.write_communities(assignments, summaries, version)

        # 6. Reset dirty flag and record last build timestamp
        _mark_rebuilt()

        elapsed = time.monotonic() - t0
        logger.info(
            "recompute_communities: done (%d communities, version=%d, %.1fs)",
            len(community_ids), version, elapsed,
        )
        return len(community_ids)

    except Exception as exc:
        logger.warning("recompute_communities failed (non-fatal): %s", exc)
        return 0


# ── Community detection ───────────────────────────────────────────────────────

def _detect_communities(nodes: list[str], edges: list[tuple]) -> dict[str, int]:
    """Detect communities using Louvain (python-louvain) or label propagation.

    Returns {entity_key: community_id (int)}.
    """
    # Lazy import networkx
    try:
        import networkx as nx
    except ImportError:
        logger.warning("networkx not installed — community detection skipped")
        return {}

    G = nx.Graph()
    G.add_nodes_from(nodes)
    for src, tgt, weight in edges:
        if G.has_edge(src, tgt):
            G[src][tgt]["weight"] = G[src][tgt].get("weight", 0) + weight
        else:
            G.add_edge(src, tgt, weight=weight)

    # Filter out zero-weight edges (safety)
    zero_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("weight", 1) <= 0]
    G.remove_edges_from(zero_edges)

    algo = settings.GRAPHRAG_COMMUNITY_ALGO

    if algo != "label_propagation":
        try:
            import community as community_louvain  # python-louvain
            assignments = community_louvain.best_partition(G, weight="weight")
            logger.info("Community detection: Louvain → %d communities", len(set(assignments.values())))
            return assignments
        except ImportError:
            logger.info("python-louvain not installed — falling back to label propagation")
        except Exception as exc:
            logger.warning("Louvain failed (%s) — falling back to label propagation", exc)

    # Label propagation fallback
    try:
        communities_gen = nx.community.label_propagation_communities(G)
        assignments: dict[str, int] = {}
        for cid, community_set in enumerate(communities_gen):
            for node in community_set:
                assignments[node] = cid
        logger.info("Community detection: label propagation → %d communities", len(set(assignments.values())))
        return assignments
    except Exception as exc:
        logger.warning("Label propagation failed: %s", exc)
        return {}


# ── Community summarization ───────────────────────────────────────────────────

def _cheap_summary(community_id: int, member_keys: list[str]) -> dict:
    """Non-LLM summary for trivial/low-priority communities. No Groq call."""
    return {
        "title": f"Community {community_id}",
        "summary": f"Contains {len(member_keys)} related entities: "
        f"{', '.join(member_keys[:10])}",
    }


def _summarize_community(
    community_id: int,
    member_keys: list[str],
    edges: list[tuple],
    use_groq: bool = True,
) -> dict:
    """Summarize a community. Uses Groq when use_groq is True and Groq is
    configured; otherwise returns a cheap member-list summary (no LLM call)."""
    if not use_groq or not settings.GROQ_BASE_URL:
        return _cheap_summary(community_id, member_keys)

    # Build a short context: member names + relationships between them
    member_set = set(member_keys)
    relevant_edges = [
        (s, t, w) for s, t, w in edges
        if s in member_set and t in member_set
    ]

    members_text = ", ".join(member_keys[:20])
    edges_text = "; ".join(
        f"{s} → {t} (weight {int(w)})"
        for s, t, w in sorted(relevant_edges, key=lambda x: -x[2])[:15]
    )

    prompt = (
        f"You are a knowledge graph analyst. A community of related entities was detected.\n\n"
        f"Entities ({len(member_keys)} total): {members_text}\n"
        f"Key relationships: {edges_text or 'none'}\n\n"
        "Provide a JSON response with:\n"
        '{"title": "<short 3-8 word title describing this community>", '
        '"summary": "<2-4 sentence description of what these entities have in common and their relationships>"}\n'
        "Respond with ONLY the JSON, no markdown."
    )

    try:
        from app.services.groq_client import chat
        raw = chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.0,
            model=settings.GROQ_EXTRACTION_MODEL,
        )
        import json
        import re
        cleaned = re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            obj = json.loads(m.group()) if m else {}

        title = str(obj.get("title", f"Community {community_id}")).strip()
        summary = str(obj.get("summary", "")).strip()
        return {"title": title or f"Community {community_id}", "summary": summary}

    except Exception as exc:
        logger.warning("Community %d summarization via Groq failed: %s", community_id, exc)
        return {
            "title": f"Community {community_id}",
            "summary": f"Contains {len(member_keys)} related entities: {members_text[:200]}",
        }


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _bump_version() -> int:
    """Increment and return the community version counter from Redis."""
    try:
        import redis as _redis
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        return int(r.incr(_VERSION_KEY))
    except Exception:
        # Fallback: use unix timestamp as version
        return int(time.time())


def _mark_rebuilt() -> None:
    """Reset dirty flag and record last build timestamp."""
    try:
        import redis as _redis
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.set(_DIRTY_KEY, 0)
        r.set(_TS_KEY, str(time.time()))
    except Exception as exc:
        logger.debug("_mark_rebuilt failed (non-fatal): %s", exc)


def _acquire_recompute_lock() -> bool:
    """Try to acquire the single-flight recompute lock. Returns True if this
    caller now holds it (and must release via _release_recompute_lock), False if
    another recompute already holds it. If Redis is unreachable, returns True so
    the recompute still runs (fail-open — correctness over dedup)."""
    try:
        import redis as _redis
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        return bool(
            r.set(_LOCK_KEY, "1", nx=True, ex=settings.GRAPHRAG_COMMUNITY_LOCK_TTL_SEC)
        )
    except Exception as exc:
        logger.debug("recompute lock acquire failed (running anyway): %s", exc)
        return True


def _release_recompute_lock() -> None:
    try:
        import redis as _redis
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.delete(_LOCK_KEY)
    except Exception as exc:
        logger.debug("recompute lock release failed (non-fatal): %s", exc)


def _should_rebuild() -> bool:
    """Check Redis dirty flag + timing to decide if a rebuild is warranted."""
    try:
        import redis as _redis
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True)
        dirty_count = int(r.get(_DIRTY_KEY) or 0)
        if dirty_count <= 0:
            return False
        last_build_ts = float(r.get(_TS_KEY) or 0.0)
        elapsed = time.time() - last_build_ts
        min_interval = settings.GRAPHRAG_COMMUNITY_MIN_INTERVAL_SEC
        dirty_threshold = settings.GRAPHRAG_COMMUNITY_DIRTY_DOCS
        return elapsed >= min_interval or dirty_count >= dirty_threshold
    except Exception:
        return True  # if Redis is down, allow rebuild


# ── Celery task ───────────────────────────────────────────────────────────────

def _register_task():
    """Register recompute_communities_task with Celery. Called at module import."""
    from app.core.background_tasks import celery_app

    @celery_app.task(
        name="app.services.community_service.recompute_communities_task",
        bind=True,
        queue="graph",
        max_retries=1,
        default_retry_delay=300,
        ignore_result=True,
    )
    def recompute_communities_task(self, document_id: str = "") -> dict:
        """Celery task: debounced community recompute.

        Checks Redis dirty flag + timing before doing expensive work.
        document_id is informational only (for logging).
        """
        logger.info(
            "recompute_communities_task triggered (document_id=%s)", document_id or "n/a"
        )

        # Single-flight: if another recompute is already running (or a duplicate
        # / redelivered copy of this task is in flight), skip immediately instead
        # of launching a second full-graph summarization. Without this, the 7
        # copies that pile up from an ingestion burst — plus any redelivered on
        # worker restart (task_acks_late) — each re-run the per-community Gemma
        # summarization back-to-back, flooding the endpoint.
        if not _acquire_recompute_lock():
            logger.info(
                "recompute_communities_task: another recompute in progress — skipping"
            )
            return {"skipped": "locked"}

        try:
            if not _should_rebuild():
                logger.info(
                    "recompute_communities_task: debounce skipped (dirty flag not met)"
                )
                return {"skipped": True}

            n = recompute_communities(level_summaries=True)
            return {"communities": n, "document_id": document_id}
        except Exception as exc:
            logger.warning("recompute_communities_task failed: %s", exc)
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            return {"error": str(exc)}
        finally:
            _release_recompute_lock()

    return recompute_communities_task


# Register the Celery task at module import time so workers pick it up.
recompute_communities_task = _register_task()
