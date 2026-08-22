# Enterprise RAG — High-Level System Architecture

This document provides high-level architectural diagrams, ASCII workflows, and technical specifications for the entire **Enterprise RAG** platform.

---

## 1. End-to-End System Architecture

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    1. PRESENTATION LAYER (Next.js 14 + Tailwind CSS)                             │
├───────────────────────────────┬───────────────────────────────┬──────────────────────────┬───────────────────────┤
│    Dashboard & Analytics      │   Upload & Pipeline Tracker   │    Document Registry     │  Enterprise RAG Chat  │
│        (/dashboard)           │     (/upload, /pipelines)     │  (/documents, /docs/[id])│        (/chat)        │
└───────────────┬───────────────┴───────────────┬───────────────┴─────────────┬────────────┴───────────┬───────────┘
                │                               │                             │                        │
                ▼                               ▼                             ▼                        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       2. API GATEWAY & ROUTING LAYER (FastAPI)                                   │
├───────────────────────────────┬───────────────────────────────┬──────────────────────────┬───────────────────────┤
│       /api/v1/analytics       │       /api/v1/ingest          │    /api/v1/documents     │     /api/v1/chat      │
│   (System & Store Health)     │ (Pipeline Run Orchestration)  │   (Upload & Metadata)    │ (Multi-Agent RAG Q&A) │
└───────────────┬───────────────┴───────────────┬───────────────┴─────────────┬────────────┴───────────┬───────────┘
                │                               │                             │                        │
                ▼                               ▼                             ▼                        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     3. BACKGROUND & ASYNC PROCESSING LAYER                                       │
├───────────────────────────────────────────────────────────────┬──────────────────────────────────────────────────┤
│             Celery Worker + Redis Message Broker              │       Async Orchestrator (Direct Pipeline)       │
└───────────────────────────────┬───────────────────────────────┴──────────────────────────┬───────────────────┘
                                │                                                          │
                                ▼                                                          ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   4. AGENTIC AI & MULTI-STORE REASONING ENGINE                                   │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  [1] RAVEN Agent (groq/compound-mini - 70K TPM) ──► Query Reframing & Sub-Query Decomposition                     │
│  [2] Semantic & Intent Router (groq/compound-mini) ──► Target Store Selection (vector, clause, table, graph)     │
│  [3] Multi-Store Hybrid Retriever ──► Parallel ANN Vector Search + SQL Pushdown + Graph Traversal                │
│  [4] Cross-Encoder Reranker (ms-marco-MiniLM-L-6-v2) ──► Calibrated Candidate Re-Scoring                          │
│  [5] SPYDER Agent (groq/compound - 70K TPM) ──► Post-Rerank Sufficiency & Corrective Self-RAG Judge              │
│  [6] Synthesis Engine (openai/gpt-oss-120b) ──► 120B Multi-Document Reasoning & Citation Grounding               │
└───────────────────────────────┬──────────────────────────────────────────────────────────┬───────────────────────┘
                                │                                                          │
                                ▼                                                          ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     5. MULTI-STORE PARTITIONED DATA TIER                                         │
├───────────────────────────────────────────────────────────────┬──────────────────────────┬───────────────────────┤
│                  PostgreSQL (Supabase)                        │     Neo4j Aura Graph     │    Supabase Storage   │
├───────────────────────────────────────────────────────────────┼──────────────────────────┼───────────────────────┤
│ • document_registry : Document Master Catalog & Lifecycle     │ • Nodes:                 │ • PDF Documents       │
│ • vector_store      : Dense Prose Chunks (1024-d BGE HNSW)    │   (:Document), (:Chunk), │   (Raw Uploads)       │
│ • clause_store      : Legal Clauses, Types & Risk Ratings     │   (:Entity), (:Community)│ • Image Storage       │
│ • table_store       : Macro Markdown Tables & Summaries       │ • Typed Relationships:   │   (Extracted Crops    │
│ • table_row_store   : Micro JSONB Data, Numerics & Vectors    │   [:OPERATES], [:BOUND_BY│    & Tables)          │
│ • image_store       : Visual Charts, OCR & VLM Text           │   [:OWNS], [:PARTNERS_WITH               │
└───────────────────────────────────────────────────────────────┴──────────────────────────┴───────────────────────┘
                                ▲                                                          ▲
                                │                                                          │
┌───────────────────────────────┴──────────────────────────────────────────────────────────┴───────────────────────┐
│                                   6. MULTI-MODEL LLM LAYER (Groq Cloud API)                                      │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ • Global Rate Limiter & Request Pacer: Enforces ~2.0s spacing and adaptive 'Retry-After' handling (max 30 RPM)   │
│ • openai/gpt-oss-120b  : 120B Flagship Model for Deep Analytical Synthesis & Multi-Document Citations          │
│ • qwen/qwen3.6-27b     : 27B Powerhouse for Knowledge Graph Entity Extraction, Typed Relations & Cypher          │
│ • groq/compound        : High-Throughput (70K TPM) Agent Model for SPYDER Retrieval Sufficiency Judging          │
│ • groq/compound-mini   : Ultra-Fast (70K TPM) Model for RAVEN Query Reframing & Store Intent Routing             │
│ • openai/gpt-oss-20b   : 20B Lightweight Model for Ingestion Chunk Metadata & Legal Clause Risk Enrichment       │
│ • BAAI/bge-large-en-v1.5: Local Embedding Model generating 1024-dimensional dense passage vectors                │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Ingestion & Multi-Store Partitioning Pipeline

```text
                                  [ User Uploads PDF Document ]
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: STAGING & VALIDATION                                                                  │
│ • Store original file binary into Supabase PDF Storage Bucket                                  │
│ • Register initial status in document_registry ('uploaded' -> 'parsing')                       │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 2: MULTIMODAL DOCUMENT PARSING                                                           │
│ • PyMuPDF / Docling structural parsing                                                         │
│ • Table extraction with bounding boxes and markdown table reconstruction                       │
│ • Visual chart / figure extraction into image_store with VLM descriptions                      │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 3: MULTI-STORE ROUTING                                                                   │
│ • Categorize Document & Subtypes: Policy, Legal Contract, Financial Report, Corporate Entity   │
│ • Partition extracted elements into Vector, Clause, Table, and Image streams                   │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 4: CHUNKING & METADATA ENRICHMENT                                                        │
│ • Semantic prose chunking with page-range tracking (for vector_store)                          │
│ • Legal clause boundary detection, categorization & risk scoring (for clause_store)           │
│ • Table row parsing: JSONB row_data + typed numeric metrics (for table_row_store)              │
│ • Image OCR & VLM semantic extraction (for image_store)                                        │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 5: DENSE VECTOR EMBEDDING (BAAI/bge-large-en-v1.5)                                       │
│ • Compute 1024-d dense vectors for prose chunks                                                │
│ • Compute 1024-d dense vectors for legal clauses                                               │
│ • Compute 1024-d dense vectors for table summaries                                             │
│ • Compute 1024-d dense vectors for individual table rows                                       │
│ • Store all partitioned records in PostgreSQL / Supabase with HNSW cosine indexes              │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STAGE 6: KNOWLEDGE GRAPH EXTRACTION & NEURAL BUILD (qwen/qwen3.6-27b)                          │
│ • Extract domain-typed entities: Organization, Person, Legal Clause, Financial Metric, Location│
│ • Extract typed semantic relationships: OPERATES, BOUND_BY, OWNS, PARTNERS_WITH, AMENDS       │
│ • Upsert (:Entity), (:Chunk), (:Document) nodes & edges to Neo4j Aura Graph                    │
│ • Compute Graph Community partitions and hierarchical community summaries                      │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
                                   [ Ingestion Complete (Status: 200 OK) ]
```

---

## 3. Query Retrieval & Corrective Self-RAG Flow (RAVEN + SPYDER)

```text
[ USER QUERY ]
      │
      ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. RAVEN AGENT — Pre-Retrieval Query Reframing (groq/compound-mini | 70K TPM)                  │
│ • Analyzes query syntax, expands acronyms, and generates clean search-optimized restatements   │
│ • Decomposes complex multi-part questions into 1-3 targeted sub-queries                        │
│ • Emits initial store hints for target domains                                                 │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. SEMANTIC & INTENT ROUTER (groq/compound-mini + Rule-Based Fast Path)                        │
│ • Classifies target search spaces:                                                             │
│   ├─ Policy / Narrative Text ──► vector_store                                                  │
│   ├─ Contractual / Legal      ──► clause_store                                                 │
│   ├─ Structured / Numeric     ──► table_store & table_row_store (SQL Pushdown)                 │
│   └─ Relationship / Multi-Hop ──► Neo4j Aura Graph (GraphRAG)                                  │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 3. MULTI-STORE PARALLEL HYBRID RETRIEVAL                                                       │
│ ┌───────────────────────────┬───────────────────────────┬────────────────────────────────────┐ │
│ │       Vector Search       │    Clause Vector Search   │   Table Row ANN & SQL Pushdown     │ │
│ │ (vector_store cosine ANN) │ (clause_store cosine ANN) │ (table_row_store & table_compiler) │ │
│ └─────────────┬─────────────┴─────────────┬─────────────┴──────────────────┬─────────────────┘ │
│               │                           │                                │                   │
│               └───────────────────────────┼────────────────────────────────┘                   │
│                                           │                                                    │
│                        ┌──────────────────┴──────────────────┐                                 │
│                        │   GraphRAG Neo4j Aura Traversal     │                                 │
│                        │ (Local Entity & Global Communities) │                                 │
│                        └──────────────────┬──────────────────┘                                 │
└───────────────────────────────────────────┼────────────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 4. CROSS-ENCODER RERANKER (cross-encoder/ms-marco-MiniLM-L-6-v2)                               │
│ • Performs cross-attention scoring between user query and all retrieved candidate chunks       │
│ • Normalizes scores across diverse stores and calibrates relevance ranking                      │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 5. SPYDER AGENT — Corrective Post-Rerank Quality Judge (groq/compound | 70K TPM)               │
│ • Evaluates if top-ranked chunks have sufficient concrete values & facts to answer query       │
│                                                                                                │
│   [Is Context Sufficient?]                                                                     │
│         ├───────── NO (Missing specific figures/data) ────────────────────────┐                │
│         │                                                                     ▼                │
│         │                                                     [Reformulate query & Loop Back]  │
│         ▼ YES (Complete context verified)                                                      │
└─────────┬──────────────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 6. MULTI-DOCUMENT SYNTHESIS ENGINE (openai/gpt-oss-120b | 120B Flagship)                       │
│ • Deep multi-store analytical reasoning and mathematical verification                          │
│ • Generates grounded markdown responses with inline citations, store badges, and source links  │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                │
                                                ▼
                                    [ Streamed Answer to User ]
```

---

## 4. Multi-Store Data Schema & Relationship Topology

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              DOCUMENT REGISTRY (PostgreSQL)                            │
│  • id (UUID, PK)                                                                       │
│  • original_filename, document_type, status, page_count, word_count                    │
│  • storage_path, storage_bucket, created_at, completed_at                              │
└──────┬──────────────────────┬──────────────────────┬──────────────────────┬────────────┘
       │ (1:N)                │ (1:N)                │ (1:N)                │ (1:N)
       ▼                      ▼                      ▼                      ▼
┌──────────────┐       ┌──────────────┐       ┌──────────────┐       ┌──────────────┐
│ vector_store │       │ clause_store │       │ table_store  │       │ image_store  │
├──────────────┤       ├──────────────┤       ├──────────────┤       ├──────────────┤
│ • id (PK)    │       │ • id (PK)    │       │ • id (PK)    │       │ • id (PK)    │
│ • doc_id (FK)│       │ • doc_id (FK)│       │ • doc_id (FK)│       │ • doc_id (FK)│
│ • chunk_idx  │       │ • clause_text│       │ • table_idx  │       │ • image_idx  │
│ • chunk_text │       │ • clause_type│       │ • table_title│       │ • structured │
│ • page_num   │       │ • risk_level │       │ • summary    │       │ • ocr_text   │
│ • embedding  │       │ • page_num   │       │ • markdown   │       │ • page_num   │
│   (1024-d)   │       │ • embedding  │       │ • bbox       │       │ • storage_path│
└──────────────┘       │   (1024-d)   │       │ • embedding  │       └──────────────┘
                       └──────────────┘       │   (1024-d)   │
                                              └──────┬───────┘
                                                     │ (1:N)
                                                     ▼
                                              ┌──────────────────┐
                                              │ table_row_store  │
                                              ├──────────────────┤
                                              │ • id (PK)        │
                                              │ • doc_id (FK)    │
                                              │ • table_id (FK)  │
                                              │ • row_index      │
                                              │ • row_data (JSON)│
                                              │ • row_num (JSON) │
                                              │ • row_text (TEXT)│
                                              │ • embedding      │
                                              │   (1024-d)       │
                                              └──────────────────┘

                                 NEO4J AURA GRAPH SCHEMA
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                        │
│     (:Document) ◄──────[:EXTRACTED_FROM]────── (:Chunk)                                │
│          ▲                                        │                                    │
│          │                                        │                                    │
│    [:MENTIONED_IN]                            [:MENTIONS]                              │
│          │                                        │                                    │
│          │                                        ▼                                    │
│     (:Entity) ◄═════════[:OPERATES / :BOUND_BY / :OWNS / :PARTNERS_WITH]═════► (:Entity)│
│          │                                                                             │
│    [:IN_COMMUNITY]                                                                     │
│          │                                                                             │
│          ▼                                                                             │
│     (:Community)                                                                       │
│                                                                                        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Intelligent Multi-Model Allocation Architecture

| Application Workload | Allocated Groq Model | Parameters / Specs | Architectural Justification |
| :--- | :--- | :--- | :--- |
| **RAG Answer Synthesis** | `openai/gpt-oss-120b` | **120 Billion**<br>30 RPM • 8K TPM | **Flagship 120B Model**: Unrivaled analytical reasoning, multi-document synthesis, mathematical calculation, and strict citation fidelity. |
| **Knowledge Graph Extraction** | `qwen/qwen3.6-27b` | **27 Billion**<br>30 RPM • 8K TPM | **27B Powerhouse**: Superior structured JSON schema adherence and NER precision for domain entity extraction and typed Cypher relationships. |
| **SPYDER Agent (Self-RAG)** | `groq/compound` | **Compound Agent**<br>30 RPM • **70K TPM** | **High-Throughput Agent**: 70K TPM capacity allows evaluating large multi-chunk context windows, judging answer sufficiency, and formulating corrective queries. |
| **RAVEN Agent & Store Router** | `groq/compound-mini` | **Mini Compound**<br>30 RPM • **70K TPM** | **Ultra-Fast Pre-Processor**: Reframes user queries, decomposes them into focused sub-queries, and routes intents with near-zero latency. |
| **Ingestion Metadata Enrichment** | `openai/gpt-oss-20b` | **20 Billion**<br>30 RPM • 8K TPM | **Lightweight Batch Worker**: Fast, cost-effective model for batch chunk categorization, clause risk scoring, and table summaries. |
| **Vector Passage Embeddings** | `BAAI/bge-large-en-v1.5` | **1024 Dimension**<br>Local Execution | **Dense Vector Model**: High-precision dense semantic embeddings for text, clauses, table summaries, and individual table rows. |
| **Candidate Re-Scoring** | `ms-marco-MiniLM-L-6-v2` | **Cross-Encoder**<br>Local Execution | **Calibrated Reranker**: Performs full query-document cross-attention across all retrieved store candidates. |
