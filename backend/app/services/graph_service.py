"""
Neo4j knowledge graph — entity/relationship model (no Chunk nodes).

The graph stores KNOWLEDGE extracted from chunks, not the chunks themselves.
Chunk text + embeddings live only in Postgres (vector_store/clause_store/
document_store/table_store); Neo4j holds entities, their typed relationships,
and lightweight metadata that traces each entity/relationship back to the
originating document + chunk (pg_id) for retrieval hydration.

Graph model:
    ``(:Document {id, filename, doc_type})``
    ``(:Entity {key, name, type, description, doc_ids[], source_documents[]})``
    ``(:Entity)-[:MENTIONED_IN {chunk_ids[], stores[], page_numbers[], confidence}]->(:Document)``
        chunk_ids[] are Postgres pg_ids; stores[] the owning table; parallel arrays
        aligned by index. This is the traceability link — NOT a Chunk node.
    ``(:Entity)-[:<DYNAMIC_TYPE> {rel_type, description, weight, doc_ids[],
        chunk_ids[], page_numbers[], source_documents[], confidence}]->(:Entity)``
        DYNAMIC_TYPE is the extracted verb phrase (CONTAINS, MAY_CAUSE, REGULATES…).
    ``(:Community {id, level, title, summary, summary_embedding, version})``
    ``(:Entity)-[:IN_COMMUNITY]->(:Community)``

Entity dedup / cross-document connection: every Entity is MERGEd by canonical
`key`, so the same entity mentioned in different documents resolves to ONE node —
shared entities automatically bridge documents (multi-hop across docs).

**Degradation is mandatory.** Neo4j is not always running here, so every public
call is wrapped: a disabled (``NEO4J_ENABLED=false``) or unreachable Neo4j makes
writes no-op and reads return ``[]`` — never an exception.
"""
import logging
import re
import time

from app.config import settings
from app.services.entity_service import canonicalize

# Sanitize an extracted relationship label into a safe Neo4j relationship type
# token. Whitelists [A-Z0-9_] and requires a leading letter/underscore so the
# value can be backtick-interpolated into Cypher without injection risk. Anything
# unusable falls back to RELATES_TO.
_REL_TYPE_RE = re.compile(r"[^A-Z0-9_]")


def _safe_rel_type(raw: str) -> str:
    t = _REL_TYPE_RE.sub("", (raw or "").strip().upper().replace(" ", "_"))
    if not t or not re.match(r"^[A-Z_]", t):
        return "RELATES_TO"
    return t[:64]

logger = logging.getLogger(__name__)

_driver = None          # cached neo4j.Driver once created
_driver_failed = False  # set True after a creation failure so we stop retrying
_schema_ensured = False  # True after ensure_schema() succeeds once

# Health-check circuit breaker: is_available() does a live network round-trip
# (verify_connectivity()), which is wasteful to repeat on every call within a
# single request (route_graphrag/local_search/global_search each call it) and
# across requests while Neo4j is down. Cache the last result for
# NEO4J_HEALTH_CACHE_TTL_SECONDS; only re-check once the cache expires.
_last_check_at = 0.0
_last_check_result = False


def _get_driver():
    """Lazy singleton. Returns a neo4j Driver, or None when graph is disabled or
    the driver can't be constructed (so callers degrade silently)."""
    global _driver, _driver_failed
    if not settings.NEO4J_ENABLED:
        return None
    if _driver is not None:
        return _driver
    if _driver_failed:
        return None
    try:
        from neo4j import GraphDatabase
        # Aura-tailored connection pool options:
        # 1. max_connection_lifetime: Recycles connections before Aura's load balancer drops idle TCP sockets (120s).
        # 2. liveness_check_timeout: Proactively tests socket health if idle > 15s before running queries.
        # 3. keep_alive: Enables TCP keep-alive probes to prevent intermediate TLS proxy timeouts.
        # 4. Suppress benign neo4j.io EOF disconnect logs on idle connection drop.
        logging.getLogger("neo4j.io").setLevel(logging.CRITICAL)
        logging.getLogger("neo4j.pool").setLevel(logging.WARNING)

        _driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD),
            max_connection_lifetime=120,
            liveness_check_timeout=15,
            keep_alive=True,
            max_connection_pool_size=50,
            connection_acquisition_timeout=30.0,
        )
        return _driver
    except Exception as exc:
        logger.warning("Neo4j driver init failed (%s) — graph features disabled", exc)
        _driver_failed = True
        return None


def _session(drv):
    """Open a session, specifying database when NEO4J_DATABASE is set (required for Aura)."""
    if settings.NEO4J_DATABASE:
        return drv.session(database=settings.NEO4J_DATABASE)
    return drv.session()


def is_available() -> bool:
    """True only if the graph is enabled AND a live connection verifies.

    Result is cached for NEO4J_HEALTH_CACHE_TTL_SECONDS so repeated calls
    (multiple per request, plus every request while Neo4j is down) don't each
    pay for a live verify_connectivity() network round-trip.
    """
    global _last_check_at, _last_check_result

    if not settings.NEO4J_ENABLED:
        return False

    now = time.monotonic()
    if now - _last_check_at < settings.NEO4J_HEALTH_CACHE_TTL_SECONDS:
        return _last_check_result

    drv = _get_driver()
    if drv is None:
        result = False
    else:
        try:
            drv.verify_connectivity()
            result = True
        except Exception as exc:
            logger.warning("Neo4j connectivity check failed: %s", exc)
            result = False

    if result != _last_check_result:
        logger.info("Neo4j availability changed: %s -> %s", _last_check_result, result)
    _last_check_at = now
    _last_check_result = result
    return result


def upsert_document(doc_id: str, filename: str, doc_type: str) -> None:
    """MERGE a Document node. No-op when graph is down."""
    drv = _get_driver()
    if drv is None or not doc_id:
        return
    try:
        with _session(drv) as session:
            session.run(
                "MERGE (d:Document {id: $id}) "
                "SET d.filename = $filename, d.doc_type = $doc_type",
                id=doc_id, filename=filename or "", doc_type=doc_type or "",
            )
    except Exception as exc:
        logger.warning("[%s] graph upsert_document failed (non-fatal): %s", doc_id, exc)


def upsert_entities(doc_id: str, entities: list[dict], filename: str = "") -> None:
    """MERGE Entity nodes (canonical key) + (Entity)-[:MENTIONED_IN]->(Document)
    edges at document granularity (no chunk metadata).

    Used by the lightweight legacy path (GRAPHRAG_ENABLED=False) so entities and
    cross-document connectivity still exist without per-chunk relationship
    extraction. No Chunk nodes. No-op when graph is down or no entities."""
    drv = _get_driver()
    if drv is None or not doc_id or not entities:
        return
    rows = []
    for e in entities:
        name = str(e.get("name", "")).strip()
        key = canonicalize(name)
        if not key:
            continue
        rows.append({"key": key, "name": name, "type": str(e.get("type", "misc")) or "misc"})
    if not rows:
        return
    try:
        with _session(drv) as session:
            session.run(
                """
                MERGE (d:Document {id: $doc_id})
                  ON CREATE SET d.filename = $filename
                WITH d
                UNWIND $rows AS row
                MERGE (e:Entity {key: row.key})
                  ON CREATE SET e.name = row.name, e.type = row.type,
                                e.doc_ids = [$doc_id], e.source_documents = [$filename]
                  ON MATCH SET e.doc_ids = CASE WHEN $doc_id IN coalesce(e.doc_ids, [])
                                 THEN e.doc_ids ELSE coalesce(e.doc_ids, []) + $doc_id END,
                               e.source_documents = CASE WHEN $filename IN coalesce(e.source_documents, [])
                                 THEN e.source_documents ELSE coalesce(e.source_documents, []) + $filename END
                MERGE (e)-[m:MENTIONED_IN]->(d)
                  ON CREATE SET m.count = 1
                  ON MATCH  SET m.count = coalesce(m.count, 0) + 1
                """,
                doc_id=doc_id, rows=rows, filename=filename or "",
            )
    except Exception as exc:
        logger.warning("[%s] graph upsert_entities failed (non-fatal): %s", doc_id, exc)


def related_documents(entity_names: list[str], exclude_doc_id: str | None = None,
                      limit: int = 5) -> list[str]:
    """Document ids that MENTION any of `entity_names` (canonicalized), ranked by
    shared-mention weight. Excludes `exclude_doc_id`. Returns [] when graph is down.

    Matching is fuzzy (substring, either direction) rather than exact-key
    equality: query-time NER and ingestion-time NER are two independent Groq
    calls and routinely phrase the SAME real-world entity slightly differently
    ("EAM" at ingestion vs "EAM system" in a question, or "Element 8" vs the
    full heading "Element 8. Identification of Resources (E8)"). Exact equality
    silently missed these and made the entity graph invisible for exactly the
    compound/cross-document questions it exists to answer.
    """
    drv = _get_driver()
    if drv is None or not entity_names:
        return []
    keys = []
    for n in entity_names:
        k = canonicalize(n)
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return []
    try:
        with _session(drv) as session:
            result = session.run(
                """
                MATCH (e:Entity)-[:MENTIONED_IN]->(d:Document)
                WHERE any(k IN $keys WHERE e.key CONTAINS k OR k CONTAINS e.key)
                  AND ($exclude IS NULL OR d.id <> $exclude)
                WITH d, count(DISTINCT e) AS score
                RETURN d.id AS id
                ORDER BY score DESC
                LIMIT $limit
                """,
                keys=keys, exclude=exclude_doc_id, limit=limit,
            )
            return [record["id"] for record in result]
    except Exception as exc:
        logger.warning("graph related_documents failed (non-fatal): %s", exc)
        return []


def close() -> None:
    """Close the driver (worker/app shutdown)."""
    global _driver
    if _driver is not None:
        try:
            _driver.close()
        except Exception:
            pass
        _driver = None


# ── Feature 1.3 GraphRAG additions ───────────────────────────────────────────

def ensure_schema() -> None:
    """Idempotent: create constraints and indexes for GraphRAG node types.
    Called once after driver init (cached via _schema_ensured). No-op on failure."""
    global _schema_ensured
    if _schema_ensured:
        return
    drv = _get_driver()
    if drv is None:
        return
    try:
        with _session(drv) as session:
            # Entity uniqueness — the dedup/MERGE key that fuses the same entity
            # across documents into one node.
            session.run(
                "CREATE CONSTRAINT entity_key_unique IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.key IS UNIQUE"
            )
            # Document uniqueness (traceability anchor for MENTIONED_IN).
            session.run(
                "CREATE CONSTRAINT document_id_unique IF NOT EXISTS "
                "FOR (d:Document) REQUIRE d.id IS UNIQUE"
            )
            # Community uniqueness
            session.run(
                "CREATE CONSTRAINT community_id_unique IF NOT EXISTS "
                "FOR (com:Community) REQUIRE com.id IS UNIQUE"
            )
            # Index: Entity.community for community lookup
            session.run(
                "CREATE INDEX entity_community_idx IF NOT EXISTS "
                "FOR (e:Entity) ON (e.community)"
            )
        _schema_ensured = True
        logger.info("Neo4j GraphRAG schema ensured")
    except Exception as exc:
        logger.warning("ensure_schema failed (non-fatal): %s", exc)


def upsert_entity_graph(
    document_id: str,
    filename: str,
    doc_type: str,
    chunk_meta: dict,
    entities: list[dict],
    relationships: list[dict],
) -> bool:
    """Upsert the entities + relationships extracted from ONE chunk into the
    knowledge graph. Creates NO Chunk node — the chunk's identity survives only
    as traceability metadata (pg_id/store/page) on the MENTIONED_IN edge and on
    each entity-entity relationship.

    chunk_meta: {"pg_id", "store", "chunk_index", "page_number"} — where this
        chunk lives in Postgres, so retrieval can hydrate the real text later.
    entities: [{"name", "key", "type", "description", "confidence"?}, ...]
    relationships: [{"source_key", "target_key", "type", "description", "confidence"?}, ...]

    Entities are MERGEd by canonical key (dedup / cross-document fusion).
    Relationship types are the extracted verb phrases, sanitized into real Neo4j
    relationship types (CONTAINS, MAY_CAUSE, …).

    Returns True on a confirmed write, False on any failure (driver down, or the
    write itself errored — e.g. a dropped connection mid-ingestion). Callers
    MUST check this return value rather than assume success: silently trusting
    "no exception propagated" here previously let an entire document's graph
    write fail with zero visible signal (build_document_graph's chunk/entity
    counters came from EXTRACTION results, not confirmed persistence, and this
    function used to return None unconditionally).
    """
    drv = _get_driver()
    if drv is None or not document_id:
        return False

    pg_id = str(chunk_meta.get("pg_id") or "")
    store = str(chunk_meta.get("store") or "vector_store")
    page_number = chunk_meta.get("page_number")
    # Neo4j lists cannot hold null — coerce a missing page to a -1 sentinel so it
    # stays index-aligned with chunk_ids/stores on the MENTIONED_IN edge.
    page_number = int(page_number) if page_number is not None else -1
    fname = filename or ""

    try:
        with _session(drv) as session:
            # 1. Document anchor (traceability only — not a chunk).
            session.run(
                """
                MERGE (d:Document {id: $doc_id})
                  ON CREATE SET d.filename = $filename, d.doc_type = $doc_type
                  ON MATCH  SET d.filename = coalesce(d.filename, $filename),
                                d.doc_type = coalesce(d.doc_type, $doc_type)
                """,
                doc_id=document_id, filename=fname, doc_type=doc_type or "",
            )

            # 2. Entities → MERGE by key (dedup) + (Entity)-[:MENTIONED_IN]->(Document)
            #    carrying per-chunk pg_id/store/page as index-aligned arrays.
            entity_rows = [
                {
                    "key": e["key"],
                    "name": e.get("name") or e["key"],
                    "type": e.get("type", "misc") or "misc",
                    "description": e.get("description", "") or "",
                    "confidence": e.get("confidence"),
                }
                for e in entities if e.get("key")
            ]
            if entity_rows:
                session.run(
                    """
                    MATCH (d:Document {id: $doc_id})
                    UNWIND $rows AS row
                    MERGE (e:Entity {key: row.key})
                      ON CREATE SET e.name = row.name, e.type = row.type,
                                    e.description = row.description,
                                    e.doc_ids = [$doc_id], e.source_documents = [$filename]
                      ON MATCH SET e.description = CASE
                                     WHEN (e.description IS NULL OR e.description = '')
                                     THEN row.description ELSE e.description END,
                                   e.doc_ids = CASE WHEN $doc_id IN coalesce(e.doc_ids, [])
                                     THEN e.doc_ids ELSE coalesce(e.doc_ids, []) + $doc_id END,
                                   e.source_documents = CASE WHEN $filename IN coalesce(e.source_documents, [])
                                     THEN e.source_documents ELSE coalesce(e.source_documents, []) + $filename END
                    MERGE (e)-[m:MENTIONED_IN]->(d)
                    WITH m, row, ($pg_id <> '' AND $pg_id IN coalesce(m.chunk_ids, [])) AS already
                    SET m.chunk_ids = CASE WHEN already OR $pg_id = '' THEN coalesce(m.chunk_ids, [])
                                        ELSE coalesce(m.chunk_ids, []) + $pg_id END,
                        m.stores = CASE WHEN already OR $pg_id = '' THEN coalesce(m.stores, [])
                                     ELSE coalesce(m.stores, []) + $store END,
                        m.page_numbers = CASE WHEN already OR $pg_id = '' THEN coalesce(m.page_numbers, [])
                                          ELSE coalesce(m.page_numbers, []) + $page_number END,
                        m.confidence = coalesce(m.confidence, row.confidence)
                    """,
                    doc_id=document_id, rows=entity_rows, filename=fname,
                    pg_id=pg_id, store=store, page_number=page_number,
                )

            # 3. Entity→Entity relationships as REAL typed edges (dynamic type).
            #    One statement per sanitized type; type is backtick-quoted after
            #    whitelisting so interpolation is injection-safe.
            for r in relationships:
                src = r.get("source_key")
                tgt = r.get("target_key")
                if not src or not tgt or src == tgt:
                    continue
                rel_type = _safe_rel_type(r.get("type", "RELATES_TO"))
                session.run(
                    f"""
                    MERGE (src:Entity {{key: $src}})
                      ON CREATE SET src.name = $src, src.type = 'misc',
                                    src.doc_ids = [$doc_id], src.source_documents = [$filename]
                    MERGE (tgt:Entity {{key: $tgt}})
                      ON CREATE SET tgt.name = $tgt, tgt.type = 'misc',
                                    tgt.doc_ids = [$doc_id], tgt.source_documents = [$filename]
                    MERGE (src)-[r:`{rel_type}`]->(tgt)
                    WITH r, ($doc_id IN coalesce(r.doc_ids, [])) AS had_doc,
                            ($pg_id <> '' AND $pg_id IN coalesce(r.chunk_ids, [])) AS had_chunk
                    SET r.rel_type = $rel_type,
                        r.weight = coalesce(r.weight, 0) + 1,
                        r.description = CASE WHEN (r.description IS NULL OR r.description = '')
                                          THEN $description ELSE r.description END,
                        r.doc_ids = CASE WHEN had_doc THEN r.doc_ids
                                      ELSE coalesce(r.doc_ids, []) + $doc_id END,
                        r.source_documents = CASE WHEN $filename IN coalesce(r.source_documents, [])
                                      THEN r.source_documents ELSE coalesce(r.source_documents, []) + $filename END,
                        r.chunk_ids = CASE WHEN had_chunk OR $pg_id = '' THEN coalesce(r.chunk_ids, [])
                                        ELSE coalesce(r.chunk_ids, []) + $pg_id END,
                        r.page_numbers = CASE WHEN had_chunk OR $pg_id = '' THEN coalesce(r.page_numbers, [])
                                           ELSE coalesce(r.page_numbers, []) + $page_number END,
                        r.confidence = coalesce(r.confidence, $confidence)
                    """,
                    src=src, tgt=tgt, doc_id=document_id, filename=fname,
                    pg_id=pg_id, page_number=page_number, rel_type=rel_type,
                    description=r.get("description", "") or "",
                    confidence=r.get("confidence"),
                )
        return True

    except Exception as exc:
        # ERROR (not warning) — a silently-dropped graph write is exactly the
        # failure mode that let an entire document's entities go missing while
        # the ingestion log reported "success". Also drop the cached driver on
        # anything that looks like a dead/broken connection so the NEXT chunk
        # in this same document gets a fresh connection instead of repeating
        # the same failure for the rest of the run.
        logger.error(
            "[%s] upsert_entity_graph FAILED — chunk pg_id=%s NOT written to Neo4j: %s",
            document_id, pg_id, exc,
        )
        _mark_driver_dead_if_connection_error(exc)
        return False


_CONNECTION_ERROR_MARKERS = (
    "serviceunavailable", "sessionexpired", "connectionreset", "brokenpipe",
    "defunct", "closed connection", "connection error", "routing table",
)


def _mark_driver_dead_if_connection_error(exc: Exception) -> None:
    """Reset the cached driver singleton if `exc` looks like a dead/broken
    connection, so the next call builds a fresh one instead of every subsequent
    write in the same ingestion run failing against the same dead socket."""
    global _driver
    text = f"{type(exc).__name__} {exc}".lower()
    if any(marker in text for marker in _CONNECTION_ERROR_MARKERS):
        logger.warning("Resetting Neo4j driver after connection-class error: %s", exc)
        try:
            if _driver is not None:
                _driver.close()
        except Exception:
            pass
        _driver = None


def fetch_entity_graph() -> tuple[list, list]:
    """Fetch all Entity nodes and RELATES_TO edges for Python-side clustering.

    Returns:
        nodes: list of entity keys (str)
        edges: list of (src_key, tgt_key, weight) tuples

    Returns ([], []) when graph is down.
    """
    drv = _get_driver()
    if drv is None:
        return [], []
    try:
        nodes: list[str] = []
        edges: list[tuple] = []
        with _session(drv) as session:
            # All entity keys
            result = session.run("MATCH (e:Entity) RETURN e.key AS key")
            for record in result:
                key = record["key"]
                if key:
                    nodes.append(key)

            # All entity→entity knowledge edges (dynamic types) with weight.
            # Both endpoints are :Entity, so MENTIONED_IN (→Document) and
            # IN_COMMUNITY (→Community) are excluded automatically.
            result = session.run(
                """
                MATCH (a:Entity)-[r]->(b:Entity)
                RETURN a.key AS src, b.key AS tgt, coalesce(r.weight, 1) AS weight
                """
            )
            for record in result:
                src = record["src"]
                tgt = record["tgt"]
                w = record["weight"] or 1
                if src and tgt:
                    edges.append((src, tgt, float(w)))

        return nodes, edges
    except Exception as exc:
        logger.warning("fetch_entity_graph failed (non-fatal): %s", exc)
        return [], []


def write_communities(
    assignments: dict,
    summaries: dict,
    version: int,
) -> None:
    """Write Community nodes and Entity-[:IN_COMMUNITY]->Community edges.

    assignments: {entity_key: community_id (int)}
    summaries: {community_id: {"title": str, "summary": str, "embedding": list|None}}
    version: monotonically increasing int for atomic swap

    After writing, deletes Community nodes from prior versions. No-op on failure.
    """
    drv = _get_driver()
    if drv is None or not assignments:
        return

    try:
        with _session(drv) as session:
            # Collect unique community ids
            community_ids = set(assignments.values())

            # Upsert Community nodes
            for cid in community_ids:
                info = summaries.get(cid) or summaries.get(str(cid)) or {}
                title = info.get("title", f"Community {cid}")
                summary = info.get("summary", "")
                emb = info.get("embedding")  # list[float] or None

                session.run(
                    """
                    MERGE (com:Community {id: $id})
                    SET com.level = 0,
                        com.title = $title,
                        com.summary = $summary,
                        com.version = $version
                    """,
                    id=str(cid),
                    title=title,
                    summary=summary,
                    version=version,
                )
                if emb is not None:
                    # Store embedding as a property (list of floats)
                    session.run(
                        """
                        MATCH (com:Community {id: $id})
                        SET com.summary_embedding = $emb
                        """,
                        id=str(cid),
                        emb=emb,
                    )

            # Link entities to communities
            assignment_rows = [
                {"entity_key": k, "community_id": str(v)}
                for k, v in assignments.items()
            ]
            session.run(
                """
                UNWIND $rows AS row
                MATCH (e:Entity {key: row.entity_key})
                MATCH (com:Community {id: row.community_id})
                MERGE (e)-[:IN_COMMUNITY]->(com)
                """,
                rows=assignment_rows,
            )

            # Delete old-version Community nodes (atomic-ish swap)
            session.run(
                """
                MATCH (com:Community)
                WHERE com.version < $version
                DETACH DELETE com
                """,
                version=version,
            )

        logger.info(
            "write_communities: %d communities, %d entity assignments (version=%d)",
            len(community_ids), len(assignments), version,
        )
    except Exception as exc:
        logger.warning("write_communities failed (non-fatal): %s", exc)


def local_neighborhood(
    entity_keys: list[str],
    hops: int = 2,
    limit: int = 40,
) -> list[dict]:
    """Return the Postgres chunks (pg_ids) that mention the seed entities, plus
    those mentioning entities reachable within N relationship hops.

    Multi-hop is real relationship traversal over entity→entity knowledge edges
    (dynamic types), NOT co-mention: e.g. seed "Drug X" →CONTAINS→ "Penicillin"
    →MAY_CAUSE→ "Allergic Reaction", crossing document boundaries wherever those
    entities are shared. pg_ids come from the MENTIONED_IN edge's index-aligned
    chunk_ids/stores arrays (no Chunk nodes).

    Supports up to 3 hops. Returns list of {pg_id, document_id, store, score,
    hop_distance} where score = 1/(1+hop_distance). Returns [] on failure.
    """
    drv = _get_driver()
    if drv is None or not entity_keys:
        return []
    try:
        results: list[dict] = []
        seen_pg_ids: set[str] = set()

        def _collect(records, hop_distance: int, score: float):
            for record in records:
                pg_id = record["pg_id"]
                if not pg_id or pg_id in seen_pg_ids:
                    continue
                seen_pg_ids.add(pg_id)
                results.append({
                    "pg_id": pg_id,
                    "document_id": record["document_id"],
                    "store": record["store"] or "vector_store",
                    "score": score,
                    "hop_distance": hop_distance,
                })

        with _session(drv) as session:
            # Hop 0: chunks that directly mention the seed entities. UNWIND the
            # index-aligned arrays so each (pg_id, store) pair becomes a row.
            # Fuzzy (substring, either direction) match — see related_documents()
            # docstring for why exact e.key IN $keys silently misses real entities.
            result = session.run(
                """
                MATCH (e:Entity)-[m:MENTIONED_IN]->(d:Document)
                WHERE any(k IN $keys WHERE e.key CONTAINS k OR k CONTAINS e.key)
                  AND m.chunk_ids IS NOT NULL
                UNWIND range(0, size(m.chunk_ids) - 1) AS i
                RETURN m.chunk_ids[i] AS pg_id, d.id AS document_id,
                       CASE WHEN m.stores IS NULL OR i >= size(m.stores)
                            THEN 'vector_store' ELSE m.stores[i] END AS store
                LIMIT $limit
                """,
                keys=entity_keys, limit=limit,
            )
            _collect(result, 0, 1.0)

            # Hops 1..N: neighbours reachable via H entity→entity edges. Every
            # intermediate node is pinned to :Entity, so only knowledge edges are
            # traversed (MENTIONED_IN/IN_COMMUNITY can't be — their far end isn't
            # an Entity).
            for hop in range(1, min(hops, 3) + 1):
                middle = "-[]-(:Entity)" * (hop - 1)
                cypher = f"""
                MATCH (e:Entity){middle}-[]-(nb:Entity)-[m:MENTIONED_IN]->(d:Document)
                WHERE any(k IN $keys WHERE e.key CONTAINS k OR k CONTAINS e.key)
                  AND NOT any(k IN $keys WHERE nb.key CONTAINS k OR k CONTAINS nb.key)
                  AND m.chunk_ids IS NOT NULL
                UNWIND range(0, size(m.chunk_ids) - 1) AS i
                RETURN m.chunk_ids[i] AS pg_id, d.id AS document_id,
                       CASE WHEN m.stores IS NULL OR i >= size(m.stores)
                            THEN 'vector_store' ELSE m.stores[i] END AS store
                LIMIT $limit
                """
                result = session.run(cypher, keys=entity_keys, limit=limit)
                _collect(result, hop, 1.0 / (1.0 + hop))

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    except Exception as exc:
        logger.warning("local_neighborhood failed (non-fatal): %s", exc)
        return []


def top_communities(limit: int = 10) -> list[dict]:
    """Return top communities ordered by summary length (size proxy).

    Returns list of {id, title, summary, summary_embedding}. Returns [] on failure.
    """
    drv = _get_driver()
    if drv is None:
        return []
    try:
        with _session(drv) as session:
            result = session.run(
                """
                MATCH (com:Community)
                RETURN com.id AS id, com.title AS title, com.summary AS summary,
                       com.summary_embedding AS embedding
                ORDER BY size(com.summary) DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            out = []
            for record in result:
                out.append({
                    "id": record["id"],
                    "title": record["title"] or "",
                    "summary": record["summary"] or "",
                    "summary_embedding": record["embedding"],
                })
            return out
    except Exception as exc:
        logger.warning("top_communities failed (non-fatal): %s", exc)
        return []


def communities_by_embedding(query_emb: list, limit: int = 8) -> list[dict]:
    """Return communities closest to query_emb by cosine similarity.

    Falls back to top_communities if no embeddings stored. Returns [] on failure.
    """
    drv = _get_driver()
    if drv is None:
        return []
    try:
        with _session(drv) as session:
            result = session.run(
                """
                MATCH (com:Community)
                WHERE com.summary_embedding IS NOT NULL
                RETURN com.id AS id, com.title AS title, com.summary AS summary,
                       com.summary_embedding AS embedding
                LIMIT 50
                """
            )
            records = [
                {
                    "id": r["id"],
                    "title": r["title"] or "",
                    "summary": r["summary"] or "",
                    "embedding": r["embedding"],
                }
                for r in result
            ]

        if not records:
            # No embeddings stored — fall back to top_communities
            return top_communities(limit)

        # Score by dot product (both normalized)
        import math
        def _dot(a, b):
            return sum(x * y for x, y in zip(a, b))

        qn = math.sqrt(sum(x * x for x in query_emb)) or 1.0
        q_unit = [x / qn for x in query_emb]

        scored = []
        for rec in records:
            emb = rec["embedding"]
            if not emb:
                scored.append((0.0, rec))
                continue
            en = math.sqrt(sum(x * x for x in emb)) or 1.0
            e_unit = [x / en for x in emb]
            sim = _dot(q_unit, e_unit)
            scored.append((sim, rec))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    except Exception as exc:
        logger.warning("communities_by_embedding failed (non-fatal): %s", exc)
        return []


def clear_document_graph(document_id: str) -> None:
    """Remove this document's contribution to the graph before re-ingestion.

    Because entities are shared across documents (MERGEd by key), we do NOT
    delete entities another document still references — we only unlink THIS
    document: drop its MENTIONED_IN edges, strip its id from every entity/
    relationship metadata array, delete knowledge edges left with no owning
    document, and finally delete entities orphaned (no MENTIONED_IN left).
    Also purges any legacy Chunk nodes from the old schema. No-op on failure.
    """
    drv = _get_driver()
    if drv is None or not document_id:
        return
    try:
        with _session(drv) as session:
            # 1. Drop this doc's traceability edges, but first collect WHICH
            #    entities they touched — every later step is scoped to exactly
            #    this set, so entities/edges belonging only to OTHER documents
            #    (including legacy pre-refactor entities with no MENTIONED_IN at
            #    all) are never touched. This was the bug: earlier steps 3/5 ran
            #    unscoped across the whole database and deleted unrelated data.
            result = session.run(
                """
                MATCH (e:Entity)-[m:MENTIONED_IN]->(:Document {id: $doc_id})
                WITH e, m
                DELETE m
                RETURN DISTINCT e.key AS key
                """,
                doc_id=document_id,
            )
            touched_keys = [r["key"] for r in result if r["key"]]

            if touched_keys:
                # 2. Strip this doc from relationship metadata + weight, scoped to
                #    edges touching a touched entity.
                session.run(
                    """
                    MATCH (a:Entity)-[r]->(b:Entity)
                    WHERE (a.key IN $keys OR b.key IN $keys)
                      AND $doc_id IN coalesce(r.doc_ids, [])
                    SET r.doc_ids = [x IN r.doc_ids WHERE x <> $doc_id],
                        r.weight = CASE WHEN coalesce(r.weight, 1) > 1 THEN r.weight - 1 ELSE 1 END
                    """,
                    keys=touched_keys, doc_id=document_id,
                )
                # 3. Delete knowledge edges (among touched entities) no document
                #    references anymore.
                session.run(
                    """
                    MATCH (a:Entity)-[r]->(b:Entity)
                    WHERE (a.key IN $keys OR b.key IN $keys)
                      AND r.doc_ids IS NOT NULL AND size(r.doc_ids) = 0
                    DELETE r
                    """,
                    keys=touched_keys,
                )
                # 4. Strip this doc from touched entities' metadata arrays.
                session.run(
                    """
                    MATCH (e:Entity) WHERE e.key IN $keys
                    SET e.doc_ids = [x IN coalesce(e.doc_ids, []) WHERE x <> $doc_id]
                    """,
                    keys=touched_keys, doc_id=document_id,
                )
                # 5. Delete touched entities now orphaned (no document mentions
                #    them anymore) — scoped, never touches other documents' entities.
                session.run(
                    """
                    MATCH (e:Entity) WHERE e.key IN $keys
                      AND NOT (e)-[:MENTIONED_IN]->(:Document)
                    DETACH DELETE e
                    """,
                    keys=touched_keys,
                )

            # 6. Legacy cleanup: purge Chunk nodes from the pre-refactor schema
            #    for THIS document only.
            session.run(
                "MATCH (c:Chunk {document_id: $doc_id}) DETACH DELETE c",
                doc_id=document_id,
            )
            # 7. Delete the Document node itself.
            session.run(
                "MATCH (d:Document {id: $doc_id}) DETACH DELETE d",
                doc_id=document_id,
            )
        logger.debug("[%s] clear_document_graph done (%d entities touched)", document_id, len(touched_keys))
    except Exception as exc:
        logger.warning("[%s] clear_document_graph failed (non-fatal): %s", document_id, exc)


def get_graph_stats() -> dict:
    """Return counts of Entity, Chunk, Document, and Community nodes.
    Used by the /graph/status endpoint. Returns zeroed dict on failure.
    """
    drv = _get_driver()
    if drv is None:
        return {"entities": 0, "chunks": 0, "documents": 0, "communities": 0, "relationships": 0}
    try:
        with _session(drv) as session:
            counts = {}
            for label in ["Entity", "Chunk", "Document", "Community"]:
                result = session.run(f"MATCH (n:{label}) RETURN count(n) AS cnt")
                record = result.single()
                counts[label.lower() + "s"] = record["cnt"] if record else 0
            # Count entity→entity knowledge edges (dynamic types; both ends Entity
            # excludes MENTIONED_IN / IN_COMMUNITY).
            result = session.run("MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS cnt")
            record = result.single()
            counts["relationships"] = record["cnt"] if record else 0
        return counts
    except Exception as exc:
        logger.warning("get_graph_stats failed (non-fatal): %s", exc)
        return {"entities": 0, "chunks": 0, "documents": 0, "communities": 0, "relationships": 0}


def list_entities(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return a page of Entity nodes for graph inspection.
    Returns [] on failure.
    """
    drv = _get_driver()
    if drv is None:
        return []
    try:
        with _session(drv) as session:
            result = session.run(
                """
                MATCH (e:Entity)
                OPTIONAL MATCH (e)-[:MENTIONED_IN]->(d:Document)
                WITH e, count(DISTINCT d) AS doc_count
                RETURN e.key AS key, e.name AS name, e.type AS type,
                       e.description AS description, doc_count
                ORDER BY doc_count DESC, e.key
                SKIP $offset LIMIT $limit
                """,
                limit=limit, offset=offset,
            )
            return [
                {
                    "key": r["key"],
                    "name": r["name"],
                    "type": r["type"],
                    "description": r["description"] or "",
                    "doc_count": r["doc_count"],
                }
                for r in result
            ]
    except Exception as exc:
        logger.warning("list_entities failed (non-fatal): %s", exc)
        return []
