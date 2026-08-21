"""
GraphRAG retriever — Feature 1.3.

route_graphrag(query) -> "local" | "global" | "none"
    Cheap entity + cue-word routing. Always "none" when graph unavailable or
    GRAPHRAG_ENABLED=False.

local_search(query, document_types, ...) -> list[dict]
    Query entities → canonicalize → graph_service.local_neighborhood
    → [{pg_id, document_id, store, score}]

global_search(query, max_communities) -> dict
    Select communities → MAP (chat_async per summary → partial answer)
    → REDUCE (final synthesis) → {answer, community_points, used_communities}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Aggregation-style query cues → prefer global search
_GLOBAL_CUES = {
    "theme", "themes", "overall", "across", "compare", "comparison",
    "summarize", "summary", "overview", "generally", "broadly",
    "in general", "all documents", "all files", "common", "pattern",
    "patterns", "trend", "trends",
    # Superlative / extremum cues — "find the X with the highest Y" has no
    # named entity to seed local_search (the CPU/model is the unknown being
    # asked for), so without a cue here it fell through to "none" and never
    # touched the graph at all.
    "highest", "lowest", "most", "least", "maximum", "minimum", "max", "min",
    "best", "worst", "top", "fastest", "slowest", "greatest", "largest",
    "smallest", "rank", "ranking",
}


# Gemma decides whether a question actually WANTS the entity graph. The prior
# heuristic — "does any noun-phrase in the query fuzzy-match an Entity node?" —
# over-triggered badly: a plain value lookup like "What is the revenue of FY
# 2023-24?" names a period ("FY 2023-24") and a metric ("revenue") that both
# exist as graph nodes (financial docs are graph-extracted), so it was wrongly
# routed "local" and dragged in unrelated graph chunks. The graph is for
# questions ABOUT entities and their relationships, not for attribute/value
# lookups that merely mention one.
_ENTITY_INTENT_PROMPT = (
    "You are a query classifier for a retrieval system that has a knowledge GRAPH "
    "of named entities (people, organizations, products, locations, laws, projects) "
    "and the typed relationships between them.\n\n"
    "Decide whether answering the user's question genuinely NEEDS that "
    "entity/relationship graph.\n\n"
    'Answer "graph" ONLY when the question is about a specific named entity\'s '
    "identity or role, or about how entities relate to / connect with one another — "
    'e.g. "Who is <person>?", "What is <person>\'s role at <org>?", '
    '"How is <X> related to <Y>?", "Which products use <technology>?", '
    '"What companies partner with <org>?".\n\n'
    'Answer "search" for everything else — especially factual value or metric '
    "lookups (revenue, cost, amount, quantity, percentage, price, date, count), "
    "definitions, explanations, summaries, procedures, and yes/no questions — EVEN "
    "IF they mention a named period, category, product, or organization. For "
    'example "What is the revenue of FY 2023-24 - Planned?" is "search", not "graph".\n\n'
    'Respond with ONLY compact JSON, no markdown: {"mode": "graph" | "search"}.'
)


def _is_entity_query(query: str) -> bool:
    """Ask Groq whether `query` is an entity/relationship question that warrants
    graph traversal (vs. a plain factual/value lookup).

    Returns False on any failure or when Groq is not configured, so graph
    augmentation only ever fires on an explicit positive signal — a flaky or
    absent classifier degrades to "no graph", never to spurious graph chunks.
    """
    if not settings.GROQ_BASE_URL:
        return False
    try:
        from app.services.groq_client import chat
        raw = chat(
            messages=[
                {"role": "system", "content": _ENTITY_INTENT_PROMPT},
                {"role": "user", "content": query[:1000]},
            ],
            max_tokens=20,
            temperature=0.0,
        )
        import json
        import re
        cleaned = re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            obj = json.loads(m.group()) if m else {}
        mode = str(obj.get("mode", "")).strip().lower()
        return mode == "graph"
    except Exception as exc:
        logger.debug("entity-intent classification failed (%s) — treating as non-entity", exc)
        return False


def route_graphrag(query: str) -> str:
    """Return "local", "global", or "none".

    Logic:
    - If GRAPHRAG_ENABLED=False or graph unavailable → "none"
    - Gemma classifies the query: only genuine entity/relationship questions are
      eligible for "local" — plain factual/value lookups that merely mention an
      entity are NOT (see _is_entity_query / _ENTITY_INTENT_PROMPT).
    - If eligible AND the query's entities exist as nodes in Neo4j → "local"
    - If query has aggregation cue words (and isn't a local hit) → "global"
    - Else → "none"
    """
    if not settings.GRAPHRAG_ENABLED:
        return "none"

    from app.services import graph_service
    if not graph_service.is_available():
        return "none"

    try:
        from app.services.entity_service import extract_entities, canonicalize

        query_lower = query.lower()
        has_global_cue = any(cue in query_lower for cue in _GLOBAL_CUES)

        # Gemma gate: unless this is genuinely an entity/relationship question,
        # local graph traversal must not fire. Global (community) aggregation is
        # still allowed for cue-word questions, since that path is opt-in via the
        # cue set and returns a graph-only answer, not merged local chunks.
        if not _is_entity_query(query):
            return "global" if has_global_cue else "none"

        entities = extract_entities(query, max_entities=settings.GRAPHRAG_LOCAL_TOP_ENTITIES)
        if not entities:
            return "global" if has_global_cue else "none"

        # Check if any extracted entities exist as nodes in Neo4j. Fuzzy
        # (substring, either direction) match — query-time NER and ingestion-time
        # NER are independent Gemma calls that routinely phrase the same
        # real-world entity slightly differently ("EAM" vs "EAM system"), so
        # exact key equality silently missed real matches and fell through to
        # "none"/"global" for exactly the entity-specific questions this local
        # mode exists to answer.
        entity_keys = [canonicalize(e["name"]) for e in entities if e.get("name")]
        entity_keys = [k for k in entity_keys if k]

        if entity_keys:
            from app.services.graph_service import _get_driver, _session
            drv = _get_driver()
            if drv:
                with _session(drv) as session:
                    result = session.run(
                        "MATCH (e:Entity) WHERE any(k IN $keys WHERE e.key CONTAINS k OR k CONTAINS e.key) "
                        "RETURN count(e) AS cnt",
                        keys=entity_keys,
                    )
                    record = result.single()
                    cnt = record["cnt"] if record else 0
                if cnt > 0:
                    return "local"

        return "global" if has_global_cue else "none"

    except Exception as exc:
        logger.debug("route_graphrag error (returning 'none'): %s", exc)
        return "none"


def local_search(
    query: str,
    document_types: Optional[list] = None,
    top_k_entities: Optional[int] = None,
    hops: Optional[int] = None,
) -> list[dict]:
    """Entity-centric local search — multi-hop, cross-document.

    Seeds from the query's entities and walks up to `hops` entity→entity
    relationship edges (default from GRAPHRAG_LOCAL_HOPS), collecting the
    Postgres chunks that mention any reachable entity. Because entities are
    MERGEd by canonical key, a hop can cross document boundaries wherever an
    entity is shared, so this surfaces chunks from *other* documents connected
    to the query's entities through the graph.

    Returns list of {pg_id, document_id, store, score, hop_distance}.
    Returns [] on any failure.
    """
    if not settings.GRAPHRAG_ENABLED:
        return []

    from app.services import graph_service
    if not graph_service.is_available():
        return []

    try:
        from app.services.entity_service import extract_entities, canonicalize

        # Config is the single source of truth. These previously defaulted to the
        # literals 10 / 1, and `x or settings.X` left the literal winning
        # (`1 or 2 == 1`), so GRAPHRAG_LOCAL_HOPS was dead config and every live
        # query silently ran at a single hop — no real multi-hop traversal.
        # Resolve from settings whenever the caller passed no explicit override
        # (`is None`, not truthiness, so an intentional 0 would be respected).
        top_k_entities = (
            settings.GRAPHRAG_LOCAL_TOP_ENTITIES if top_k_entities is None else top_k_entities
        )
        hops = settings.GRAPHRAG_LOCAL_HOPS if hops is None else hops

        entities = extract_entities(query, max_entities=top_k_entities)
        if not entities:
            return []

        entity_keys = []
        seen: set[str] = set()
        for e in entities:
            k = canonicalize(e.get("name", ""))
            if k and k not in seen:
                seen.add(k)
                entity_keys.append(k)

        if not entity_keys:
            return []

        results = graph_service.local_neighborhood(
            entity_keys=entity_keys,
            hops=hops,
            limit=top_k_entities * 5,
        )
        return results

    except Exception as exc:
        logger.warning("local_search failed (non-fatal): %s", exc)
        return []


async def global_search(
    query: str,
    max_communities: int = 8,
) -> dict:
    """Community map-reduce global search.

    MAP: chat_async per community summary → partial answer + self-rated relevance
    REDUCE: chat_async final synthesis over partial answers

    Returns:
        {
            "answer": str,
            "community_points": list[str],
            "used_communities": list[str],  # community ids
        }

    Returns {"answer": "", "community_points": [], "used_communities": []} on failure.
    """
    _empty = {"answer": "", "community_points": [], "used_communities": []}

    if not settings.GRAPHRAG_ENABLED:
        return _empty

    from app.services import graph_service
    if not graph_service.is_available():
        return _empty

    try:
        max_communities = max_communities or settings.GRAPHRAG_GLOBAL_MAX_COMMUNITIES

        # Select communities (try by embedding if possible, else top by summary size)
        communities = []
        if settings.GROQ_BASE_URL:
            try:
                from app.services.embedding_service import embed_query
                q_emb = embed_query(query).tolist()
                communities = graph_service.communities_by_embedding(q_emb, limit=max_communities)
            except Exception:
                pass

        if not communities:
            communities = graph_service.top_communities(limit=max_communities)

        if not communities:
            return _empty

        # MAP phase: ask Gemma to answer from each community summary
        from app.services.groq_client import chat_async

        async def _map_community(comm: dict) -> dict | None:
            summary = comm.get("summary", "")
            if not summary:
                return None
            prompt = (
                f"Community context: {comm.get('title', '')}\n\n"
                f"{summary}\n\n"
                f"Question: {query}\n\n"
                "Based ONLY on the community context above, provide a JSON response:\n"
                '{"relevant": true|false, "relevance_score": 0.0-1.0, "answer": "<partial answer or empty>"}\n'
                "If the context does not help answer the question, set relevant=false and answer=''.\n"
                "Respond with ONLY the JSON, no markdown."
            )
            try:
                raw = await chat_async(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=300,
                    temperature=0.0,
                )
                import json
                import re
                cleaned = re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()
                try:
                    obj = json.loads(cleaned)
                except json.JSONDecodeError:
                    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
                    obj = json.loads(m.group()) if m else {}
                if obj.get("relevant") and obj.get("answer"):
                    return {
                        "community_id": str(comm.get("id", "")),
                        "title": comm.get("title", ""),
                        "answer": str(obj["answer"]).strip(),
                        "score": float(obj.get("relevance_score", 0.5)),
                    }
                return None
            except Exception as exc:
                logger.debug("MAP community %s failed: %s", comm.get("id"), exc)
                return None

        # Run MAP concurrently
        map_tasks = [_map_community(c) for c in communities]
        map_results_raw = await asyncio.gather(*map_tasks, return_exceptions=True)

        partial_answers = [
            r for r in map_results_raw
            if r is not None and not isinstance(r, Exception)
        ]
        partial_answers.sort(key=lambda x: x.get("score", 0), reverse=True)

        if not partial_answers:
            return _empty

        # REDUCE phase: synthesize partial answers
        points_text = "\n".join(
            f"[{p['title']}] {p['answer']}"
            for p in partial_answers
        )
        reduce_prompt = (
            f"You have collected partial answers from multiple knowledge communities "
            f"about the following question:\n\n"
            f"Question: {query}\n\n"
            f"Partial answers:\n{points_text}\n\n"
            "Synthesize a comprehensive final answer. Be concise and accurate. "
            "Do not repeat redundant information."
        )
        try:
            final_answer = await chat_async(
                messages=[{"role": "user", "content": reduce_prompt}],
                max_tokens=500,
                temperature=0.1,
            )
        except Exception as exc:
            logger.warning("REDUCE phase failed: %s", exc)
            final_answer = "\n".join(p["answer"] for p in partial_answers)

        return {
            "answer": final_answer.strip(),
            "community_points": [p["answer"] for p in partial_answers],
            "used_communities": [p["community_id"] for p in partial_answers],
        }

    except Exception as exc:
        logger.warning("global_search failed (non-fatal): %s", exc)
        return _empty
