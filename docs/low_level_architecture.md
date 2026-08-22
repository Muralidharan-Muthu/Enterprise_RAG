# Enterprise RAG — Low-Level Architecture (LLD)

This document provides low-level architectural blueprints, database schemas, internal data structures, algorithms, and component execution specifications for the **Enterprise RAG** platform.

---

## Table of Contents
1. [Codebase & Module Topology](#1-codebase--module-topology)
2. [Database Engine & Physical Schema (DDL)](#2-database-engine--physical-schema-ddl)
3. [Neo4j Knowledge Graph Schema & Cypher Specifications](#3-neo4j-knowledge-graph-schema--cypher-specifications)
4. [Ingestion Pipeline Execution Specifications](#4-ingestion-pipeline-execution-specifications)
5. [Multi-Agent Retrieval & Self-RAG Mechanics](#5-multi-agent-retrieval--self-rag-mechanics)
6. [Rate Limiting, Concurrency & API Pacing](#6-rate-limiting-concurrency--api-pacing)
7. [API Contracts & Data Transfer Objects (DTOs)](#7-api-contracts--data-transfer-objects-dtos)
8. [Fail-Open & Circuit Breaking Matrix](#8-fail-open--circuit-breaking-matrix)

---

## 1. Codebase & Module Topology

```text
c:\Users\mural\Desktop\Multi_Store-Rag\AI_CHATBOT\
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── routes/
│   │   │   │   ├── chat.py             # Multi-store chat, history, streaming Q&A
│   │   │   │   ├── documents.py        # Upload, metadata, delete cascade
│   │   │   │   ├── ingestion.py        # Pipeline tracking, runs, stages
│   │   │   │   └── analytics.py        # Store metrics, document distribution
│   │   │   └── router.py               # Root API router (/api/v1)
│   │   ├── core/
│   │   │   ├── exceptions.py           # Domain exception classes
│   │   │   └── logging.py              # Structured logging configuration
│   │   ├── db/
│   │   │   ├── connection.py           # ThreadedConnectionPool (minconn=2, maxconn=20)
│   │   │   ├── migrations/             # SQL Migrations (001 -> 023)
│   │   │   └── repositories/
│   │   │       ├── document_registry.py# Master document CRUD & cascade
│   │   │       ├── vector_store.py     # Dense passage vectors
│   │   │       ├── clause_store.py     # Legal contract clauses
│   │   │       ├── table_store.py      # Macro markdown tables & summaries
│   │   │       ├── table_cell_store.py # Micro table_row_store rows & embeddings
│   │   │       └── image_store.py      # Visual charts, OCR & VLM extractions
│   │   ├── services/
│   │   │   ├── agents/
│   │   │   │   ├── raven_agent.py      # Pre-retrieval query reframing & decomposition
│   │   │   │   └── spyder_agent.py     # Post-rerank sufficiency & self-RAG judge
│   │   │   ├── groq_client.py          # Global request pacer, adaptive 429 backoff
│   │   │   ├── document_parser.py      # PyMuPDF + Docling multi-modal parser
│   │   │   ├── router_service.py       # Multi-label document type classifier
│   │   │   ├── chunker.py              # Semantic sentence & prose chunker
│   │   │   ├── clause_enrichment_service.py # Clause extraction & risk scoring
│   │   │   ├── table_schema_service.py # Unified row builder (data, numeric, text, emb)
│   │   │   ├── table_sql_compiler.py   # SQL AST compiler for table pushdown
│   │   │   ├── embedding_service.py    # Local BGE-large-en-v1.5 dense embedder
│   │   │   ├── retriever_service.py    # Parallel multi-store hybrid retriever
│   │   │   ├── reranker_service.py     # Cross-encoder re-scoring (ms-marco-MiniLM)
│   │   │   ├── synthesis_service.py    # 120B multi-doc citation synthesizer
│   │   │   ├── graph_service.py        # Neo4j driver & Cypher executor
│   │   │   ├── graph_extraction_service.py # Entity/relation extraction & JSON repair
│   │   │   ├── graph_build_service.py  # Graph pipeline orchestrator
│   │   │   ├── graphrag_retriever.py   # Local/Global GraphRAG search
│   │   │   └── community_service.py    # Leiden/Louvain community detection
│   │   └── config.py                   # Pydantic Settings & multi-model mapping
│   ├── tests/                          # Pytest suite (68+ tests)
│   └── scripts/                        # Database migrations & backfills
└── frontend/
    └── src/
        ├── app/
        │   ├── (dashboard)/
        │   │   ├── dashboard/          # Analytics & store distribution
        │   │   ├── documents/          # Document manager & detail inspector
        │   │   ├── pipelines/          # Pipeline runs & stage tracker
        │   │   ├── upload/             # Drag-and-drop ingestion uploader
        │   │   └── chat/               # Conversational multi-store interface
        ├── components/
        │   ├── chat/                   # MessageStream, Citations, StoreBadges
        │   ├── documents/              # DocumentTable, DocumentDetail, PDFViewer
        │   └── ui/                     # ConfirmDialog, Modal, Progress, Toast
        └── lib/
            ├── api/                    # API client layer (Axios / Fetch)
            └── types.ts                # TypeScript domain models & DTOs
```

---

## 2. Database Engine & Physical Schema (DDL)

The relational and vector persistence tier is hosted on PostgreSQL (Supabase) under the `multi_store_rag_working` schema.

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       POSTGRESQL PHYSICAL TABLE DEFINITIONS                                    │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 `document_registry` (Master Catalog)
```sql
CREATE TABLE IF NOT EXISTS multi_store_rag_working.document_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_filename TEXT NOT NULL,
    storage_path TEXT,
    storage_bucket TEXT DEFAULT 'rag-documents',
    file_hash TEXT UNIQUE,
    file_size_bytes BIGINT,
    mime_type TEXT,
    document_type TEXT,                -- 'policy' | 'financial' | 'legal' | 'entity'
    document_types TEXT[],             -- Multi-label classification partitions
    document_subtype TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded', -- 'uploaded'|'parsing'|'routing'|'chunking'|'embedding'|'storing'|'completed'|'failed'
    page_count INTEGER,
    word_count INTEGER,
    router_confidence FLOAT,
    router_reasoning TEXT,
    doc_title TEXT,
    doc_summary TEXT,
    doc_metadata JSONB DEFAULT '{}',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_document_registry_status ON multi_store_rag_working.document_registry(status);
CREATE INDEX idx_document_registry_created ON multi_store_rag_working.document_registry(created_at DESC);
```

### 2.2 `vector_store` (Dense Narrative Chunks)
```sql
CREATE TABLE IF NOT EXISTS multi_store_rag_working.vector_store (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES multi_store_rag_working.document_registry(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    token_count INTEGER,
    page_number INTEGER,
    page_number_end INTEGER,
    section_header TEXT,
    chunk_metadata JSONB DEFAULT '{}',
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX idx_vector_store_doc ON multi_store_rag_working.vector_store(document_id);
CREATE INDEX idx_vector_store_embedding ON multi_store_rag_working.vector_store USING hnsw (embedding vector_cosine_ops);
```

### 2.3 `clause_store` (Legal & Contractual Clauses)
```sql
CREATE TABLE IF NOT EXISTS multi_store_rag_working.clause_store (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES multi_store_rag_working.document_registry(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    clause_title TEXT,
    clause_text TEXT NOT NULL,
    clause_type TEXT,                  -- 'termination'|'indemnity'|'liability'|'governing_law'|'confidentiality'|'misc'
    risk_level TEXT DEFAULT 'low',     -- 'low' | 'medium' | 'high' | 'critical'
    page_number INTEGER,
    clause_metadata JSONB DEFAULT '{}',
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX idx_clause_store_doc ON multi_store_rag_working.clause_store(document_id);
CREATE INDEX idx_clause_store_type ON multi_store_rag_working.clause_store(clause_type);
CREATE INDEX idx_clause_store_risk ON multi_store_rag_working.clause_store(risk_level);
CREATE INDEX idx_clause_store_embedding ON multi_store_rag_working.clause_store USING hnsw (embedding vector_cosine_ops);
```

### 2.4 `table_store` (Macro Tables)
```sql
CREATE TABLE IF NOT EXISTS multi_store_rag_working.table_store (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES multi_store_rag_working.document_registry(id) ON DELETE CASCADE,
    table_index INTEGER NOT NULL,
    table_title TEXT,
    table_summary TEXT,
    markdown_text TEXT NOT NULL,
    raw_text TEXT,
    structured_content JSONB,
    page_number INTEGER,
    bbox JSONB,                        -- {"x0": 50, "y0": 100, "x1": 500, "y1": 400}
    column_headers TEXT[],
    row_count INTEGER,
    column_count INTEGER,
    has_numeric_data BOOLEAN DEFAULT TRUE,
    extraction_quality TEXT DEFAULT 'high',
    embedding vector(1024),
    structured_content_embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, table_index)
);

CREATE INDEX idx_table_store_doc ON multi_store_rag_working.table_store(document_id);
CREATE INDEX idx_table_store_embedding ON multi_store_rag_working.table_store USING hnsw (embedding vector_cosine_ops);
```

### 2.5 `table_row_store` (Micro Rows for SQL Pushdown & Vector Search)
```sql
CREATE TABLE IF NOT EXISTS multi_store_rag_working.table_row_store (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES multi_store_rag_working.document_registry(id) ON DELETE CASCADE,
    table_id UUID NOT NULL REFERENCES multi_store_rag_working.table_store(id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    row_data JSONB NOT NULL DEFAULT '{}',       -- Raw key/value mapping: {"Metric": "Revenue", "2024": "$100M"}
    row_numeric JSONB NOT NULL DEFAULT '{}',    -- Clean numeric metrics: {"2024": 100000000.0}
    row_text TEXT NOT NULL,                     -- Text formatted: "Metric: Revenue | 2024: $100M"
    embedding vector(1024),                     -- 1024-d dense vector for row-level ANN search
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(table_id, row_index)
);

CREATE INDEX idx_table_row_store_doc ON multi_store_rag_working.table_row_store(document_id);
CREATE INDEX idx_table_row_store_table ON multi_store_rag_working.table_row_store(table_id);
CREATE INDEX idx_table_row_store_data_gin ON multi_store_rag_working.table_row_store USING gin (row_data);
CREATE INDEX idx_table_row_store_numeric_gin ON multi_store_rag_working.table_row_store USING gin (row_numeric jsonb_path_ops);
CREATE INDEX idx_table_row_store_embedding ON multi_store_rag_working.table_row_store USING hnsw (embedding vector_cosine_ops);
```

### 2.6 `image_store` (Visual Charts, OCR & VLM)
```sql
CREATE TABLE IF NOT EXISTS multi_store_rag_working.image_store (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES multi_store_rag_working.document_registry(id) ON DELETE CASCADE,
    image_index INTEGER NOT NULL,
    storage_path TEXT,
    page_number INTEGER,
    bbox JSONB,
    ocr_text TEXT,
    structured_content TEXT,           -- VLM extraction of charts/diagrams
    processing_status TEXT DEFAULT 'COMPLETED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, image_index)
);

CREATE INDEX idx_image_store_doc ON multi_store_rag_working.image_store(document_id);
```

---

## 3. Neo4j Knowledge Graph Schema & Cypher Specifications

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       NEO4J AURA GRAPH TOPOLOGY                                │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Node Labels & Property Definitions
* `(:Document)`: `{id: string, filename: string, doc_type: string, created_at: datetime}`
* `(:Chunk)`: `{id: string, document_id: string, store: string, chunk_index: int, page_number: int, text: string}`
* `(:Entity)`: `{name: string, canonical_name: string, type: string, description: string, doc_ids: list[string]}`
  - *Entity Types*: `organization`, `person`, `legal_clause`, `financial_metric`, `location`, `product`, `policy`.
* `(:Community)`: `{id: int, level: int, title: string, summary: string, member_count: int}`

### 3.2 Edge Types & Constraints
* `(:Chunk)-[:EXTRACTED_FROM]->(:Document)`
* `(:Chunk)-[:MENTIONS {confidence: float}]->(:Entity)`
* `(:Entity)-[:MENTIONED_IN]->(:Document)`
* `(:Entity)-[:IN_COMMUNITY]->(:Community)`
* `(:Entity)-[:OPERATES | :BOUND_BY | :OWNS | :PARTNERS_WITH | :CONTRIBUTES_TO | :AMENDS | :SUPPLIES | :REGULATES {description: string, confidence: float, document_id: string}]->(:Entity)`

### 3.3 Graph Merge Cypher Execution (`graph_service.py`)
```cypher
// 1. Merge Document Node
MERGE (d:Document {id: $doc_id})
SET d.filename = $filename, d.doc_type = $doc_type;

// 2. Merge Entity Nodes & Connect to Document
UNWIND $entities AS ent
MERGE (e:Entity {canonical_name: ent.canonical_name})
ON CREATE SET e.name = ent.name, e.type = ent.type, e.description = ent.description, e.doc_ids = [$doc_id]
ON MATCH SET e.doc_ids = CASE WHEN $doc_id IN e.doc_ids THEN e.doc_ids ELSE e.doc_ids + $doc_id END
MERGE (e)-[:MENTIONED_IN]->(d);

// 3. Merge Dynamic Domain-Typed Relationships
UNWIND $relationships AS rel
MATCH (src:Entity {canonical_name: rel.src_key})
MATCH (tgt:Entity {canonical_name: rel.tgt_key})
MERGE (src)-[r:OPERATES {document_id: $doc_id}]->(tgt)
SET r.description = rel.description, r.confidence = rel.confidence;
```

---

## 4. Ingestion Pipeline Execution Specifications

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│                               6-STAGE INGESTION EXECUTION PIPELINE                             │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Stage ID | Primary Module | Core Function | Input Artifact | Output Artifact / Destination |
| :--- | :--- | :--- | :--- | :--- |
| **Stage 1** | `ingestion_orchestrator.py` | `stage_1_staging()` | PDF Upload Stream | Supabase Storage (`rag-documents/<uuid>.pdf`) + `document_registry` record |
| **Stage 2** | `document_parser.py` | `parse_document()` | Staged PDF File | `ParsedDocument(text, tables=[...], images=[...])` |
| **Stage 3** | `router_service.py` | `classify_document()` | `ParsedDocument.text[:4000]` | `RouterResult(document_type, document_types, confidence)` |
| **Stage 4** | `chunker.py`<br>`clause_enrichment_service.py`<br>`table_schema_service.py` | `chunk_document()`<br>`enrich_clauses()`<br>`build_unified_rows()` | `ParsedDocument` + `RouterResult` | • `list[TextChunk]` (Prose)<br>• `list[LegalClause]` (Clauses)<br>• `list[UnifiedRow]` (Table Rows) |
| **Stage 5** | `embedding_service.py`<br>`table_cell_store.py`<br>`clause_store.py` | `embed_passages()`<br>`insert_table_rows()` | Text batches (batch=32) | • `vector_store` (1024-d HNSW)<br>• `clause_store` (1024-d HNSW)<br>• `table_store` & `table_row_store` |
| **Stage 6** | `graph_extraction_service.py`<br>`graph_build_service.py` | `extract_graph_elements()`<br>`build_document_graph()` | All chunk texts | Neo4j Aura Graph Nodes, Typed Edges & Leiden Communities |

---

## 5. Multi-Agent Retrieval & Self-RAG Mechanics

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│                           MULTI-AGENT RETRIEVAL & VERIFICATION PIPELINE                        │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 RAVEN Agent (`raven_agent.py`)
* **Role**: Query pre-processing, syntax disambiguation, and sub-query fan-out.
* **Model**: `groq/compound-mini` (70,000 TPM capacity, max_tokens=256, temp=0.0).
* **Algorithm**:
  ```python
  async def reframe(query: str) -> dict:
      # Prompts model for search-optimized restatement and 0-3 sub-questions
      # Output: {"reframed": str, "sub_queries": list[str], "store_hint": dict | None}
      # Fail-Open: On timeout/error, returns {"reframed": query, "sub_queries": [], "used_fallback": True}
  ```

### 5.2 SPYDER Agent (`spyder_agent.py`)
* **Role**: Post-rerank sufficiency judge (Corrective Self-RAG).
* **Model**: `groq/compound` (70,000 TPM capacity, max_tokens=300, temp=0.0).
* **Sufficiency Logic**:
  - Inspects top-$K$ reranked context blocks.
  - Verifies presence of concrete values (e.g. monetary amounts, dates, percentages) vs. descriptive metadata.
  - Returns `{"sufficient": bool, "confidence": float, "missing": str, "reframed_query": str | None}`.
  - If `sufficient == False` and loop counter $< 2$, triggers follow-up retrieval with `reframed_query`.

### 5.3 Multi-Store Hybrid Retriever (`retriever_service.py`)
```python
def retrieve(query: str, top_k: int = 5, intent: dict = None) -> list[RetrievedChunk]:
    # 1. Query Embedding (BGE-Large-v1.5, 1024-d)
    q_emb = embed_query(query)
    
    # 2. Parallel Store Dispatch (ThreadPoolExecutor)
    futures = []
    if "vector" in target_stores:
        futures.append(pool.submit(_query_vector_store, conn, q_emb, top_k))
    if "clause" in target_stores:
        futures.append(pool.submit(_query_clause_store, conn, q_emb, top_k))
    if "table" in target_stores:
        futures.append(pool.submit(_query_table_store_parent_only, conn, q_emb, top_k))
    
    # 3. Micro Table Row Search:
    # Executes: (table_row_store.embedding <=> q_emb::vector) for exact row match
    
    # 4. GraphRAG Traversal (Local Entity Neighborhood + Global Community Summaries)
    
    # 5. Cross-Encoder Reranking:
    # Reranks combined candidates with ms-marco-MiniLM-L-6-v2
```

---

## 6. Rate Limiting, Concurrency & API Pacing

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│                         GROQ RATE LIMITER & ADAPTIVE 429 BACKOFF ENGINE                        │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

In [`groq_client.py`](file:///c:/Users/mural/Desktop/Multi_Store-Rag/AI_CHATBOT/backend/app/services/groq_client.py), a global cross-thread and cross-coroutine rate limiter coordinates all requests to prevent exceeding Groq tier limits (30 RPM):

```text
           Caller Thread 1 ──┐
           Caller Thread 2 ──┼──► [ _pace_request_sync() / Lock ] ──► (Sleep if elapsed < 2.0s) ──► Groq API
           Async Coroutine ──┘             ▲
                                           │
                                  [ Adaptive 429 Handler ]
                                  • Parses 'retry-after' header
                                  • Parses regex "Please try again in X.XXs"
                                  • Exponential backoff with jitter: [2.5s, 5.0s, 10.0s, 20.0s, 35.0s]
                                  • Retries up to 6 attempts before failing open
```

---

## 7. API Contracts & Data Transfer Objects (DTOs)

### 7.1 `POST /api/v1/chat/message`
```json
// Request Body
{
  "message": "What is the EBITDA contribution of the O2C segment and what are the governing gas terms?",
  "session_id": "8f3b2e1a-4c5d-6e7f-8a9b-0c1d2e3f4a5b",
  "document_id": null,
  "stream": true
}

// Streamed Response Events (SSE)
event: status
data: {"stage": "reframing", "detail": "RAVEN decomposing query"}

event: status
data: {"stage": "retrieving", "stores": ["table", "clause", "graph"]}

event: token
data: {"token": "According"}

event: token
data: {"token": " to"}

event: citations
data: [
  {
    "store": "table",
    "document_filename": "Annual_Report_2024.pdf",
    "page_number": 42,
    "text": "Business Segment: Oil to Chemicals (O2C) | EBITDA: $10B | Contribution: 60%"
  },
  {
    "store": "clause",
    "document_filename": "Gas_Supply_Agreement.pdf",
    "page_number": 12,
    "clause_type": "governing_law",
    "text": "This Agreement shall be governed by the laws of England and Wales."
  }
]
```

### 7.2 `POST /api/v1/documents/upload`
```json
// Multipart Form-Data: file=@contract.pdf

// Response (201 Created)
{
  "document_id": "efbd5bcf-7a8e-4d84-8a2f-bc3d98546ef5",
  "filename": "contract.pdf",
  "status": "uploaded",
  "pipeline_run_id": "0d296f28-2801-44bb-a6fd-91d377e461b5",
  "created_at": "2026-08-22T18:37:00.000Z"
}
```

---

## 8. Fail-Open & Circuit Breaking Matrix

| Component | Failure Mode | Circuit Breaker / Fallback Action | Impact on User Experience |
| :--- | :--- | :--- | :--- |
| **Groq API** | 429 Rate Limit | Sleep exact `Retry-After` duration + retry up to 6 times. | Request experiences minor latency delay, completes with 200 OK. |
| **RAVEN Agent** | Timeout / Bad JSON | Fail-open: returns raw user query without sub-query decomposition. | Retrieval proceeds directly with raw query. |
| **SPYDER Agent** | Timeout / Bad JSON | Fail-open: marks `sufficient=True` (avoids infinite loop). | Synthesizes response immediately using top-ranked chunks. |
| **Neo4j Aura** | Connection Failure | Auto-retry with 30s timeout; falls back to relational vector stores (`vector_store`, `clause_store`, `table_store`). | Chat responses answer from Postgres without graph hops. |
| **Table Row Embeddings** | GPU/Memory Error | Fall back to parent `table_store` summary embedding. | Macro table content retrieved rather than micro row. |
| **VLM / OCR** | Unsupported image format | Skip image OCR and proceed with text/table extraction. | Document ingestion finishes without chart caption. |
