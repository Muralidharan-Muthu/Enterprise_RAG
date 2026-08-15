# Phase 4 — Multi-PDF Connection via Neo4j Graph (spec + plan)

**Date:** 2026-06-19 · **Branch:** `feat/phase4-neo4j-graph` · Phase 4 of 4 (final).

## Goal

Connect PDFs that share entities so the system can answer across documents
("which contracts mention Acme?", "what do all policies say about retention?").
Build the real **Neo4j** graph the UI already depicts, and use it to expand
retrieval to *related* documents — not just the single best-matching one.

## Architecture

A `(:Document)-[:MENTIONS {count}]->(:Entity)` graph in Neo4j. Two documents are
"connected" when they MENTION a shared Entity. Retrieval expands from the query's
entities to documents that mention them, pulling those documents' chunks into the
rerank pool.

**Graceful degradation is mandatory** (Docker/Neo4j is not always running here):
- Ingestion: graph write is best-effort; failure logs + continues (chunks still stored).
- Query: graph expansion is best-effort; if Neo4j is unreachable, retrieval falls
  back to exactly today's Phase-2 behavior. Neo4j down ⇒ system still fully works.

## Components

- **Infra:** `neo4j:5` service in docker-compose (bolt 7687, http 7474, auth via env,
  volume); `neo4j>=5,<6` Python driver (installed); config `NEO4J_URI`, `NEO4J_USER`,
  `NEO4J_PASSWORD`, `NEO4J_ENABLED`; health check reports neo4j status.
- **`graph_service.py`:** lazy driver singleton; `upsert_document(doc_id, filename, doc_type)`;
  `upsert_entities(doc_id, entities)` (MERGE Entity nodes + MENTIONS edges);
  `related_documents(entity_names, exclude_doc_id=None, limit=5) -> list[str]` (Cypher);
  `is_available() -> bool`. Every call wrapped so a down Neo4j returns empty/no-op, never raises.
- **`entity_service.py`:** `extract_entities(text, max=20) -> list[{name,type}]` via Gemma NER
  (JSON) with a rule-based fallback (capitalized multiword sequences, dedup, stoplist);
  `canonicalize(name) -> str` (lowercase/trim) for stable MERGE keys.
- **Ingestion wiring (orchestrator):** new best-effort "graph" stage — extract entities from
  `raw_text`, `upsert_document` + `upsert_entities`, and store the entity name list on the
  doc (populate `document_store.entities_mentioned` where applicable / a registry field).
- **Graph-expanded retrieval:** in the query path, extract entities from the query, call
  `related_documents`, fetch those documents' top chunks (reuse `retrieve(document_id=...)`),
  and merge them into the balanced pool before reranking. Capped + deduped; off when Neo4j down.

## Decisions

- Entity match key = canonicalized name (case-insensitive). Entity `type` stored as a property.
- Expansion is 1-hop (Document→Entity→Document); enough for "shared topic/party" linking.
- No migration to Postgres tables for the graph — Neo4j is the graph store of record.
  `document_store.entities_mentioned` is populated for display/back-compat only.

## Tasks (TDD, inline; live steps gated on Docker+Neo4j up)

- **P4T1 Infra+config+health** — docker-compose `neo4j` service; `requirements.txt += neo4j`;
  `config.py` NEO4J_* + `NEO4J_ENABLED` (default false so nothing breaks until opted in);
  health endpoint reports neo4j (`disabled`/`ok`/`unreachable`). Unit-test config defaults + health mapping.
- **P4T2 graph_service** — driver singleton + `upsert_document`/`upsert_entities`/`related_documents`/`is_available`,
  all degradation-safe. Unit-test with a mocked driver/session: assert Cypher + params, and that a
  raising driver yields `[]`/no-op (not an exception).
- **P4T3 entity_service** — `extract_entities` (Gemma + rule fallback) + `canonicalize`. Unit-test
  rule fallback, JSON parse, canonicalization, dedup.
- **P4T4 ingestion wiring** — orchestrator best-effort graph stage; pure helper
  `_build_graph_inputs(parsed_doc, doc_id) -> (doc_meta, entities)` unit-tested; stage wrapped non-fatal.
- **P4T5 graph-expanded retrieval** — `graph_expanded_chunks(query, primary_results, ...)` helper:
  query entities → related docs → their chunks → merged/deduped; no-op when Neo4j down. Unit-test
  with mocked graph_service + retrieve.
- **P4T6 live verify** — (needs Docker+Neo4j) ingest 2 docs sharing an entity → graph has the edge →
  cross-doc query surfaces the neighbor doc's chunk. Document as gated/manual.

## Acceptance

- With Neo4j up: ingesting docs builds `(:Document)-[:MENTIONS]->(:Entity)`; a query whose entity
  appears in multiple docs pulls chunks from the related docs into the answer.
- With Neo4j down/disabled: ingestion and query behave exactly as Phase 3 (no errors, graph no-ops).
- Health endpoint reports neo4j status; `NEO4J_ENABLED=false` fully bypasses graph code.
