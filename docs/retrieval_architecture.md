# Multi-Store RAG — Retrieval Pipeline Architecture

This document describes the end-to-end multi-store retrieval and generation architecture, detailing query routing, hybrid search, GraphRAG reasoning, cross-encoder reranking, and citation synthesis.

---

## 1. Retrieval Pipeline Architecture & Flowchart

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       0. USER QUERY INGESTION                                           │
│  [User Query: "What is the FY24 revenue and termination penalty for Vendor X?"]                         │
│                                                   │                                                     │
│                                                   ▼                                                     │
│                                    [Query Router & Global Gate]                                         │
│                      (Detects broad global aggregation vs. specific factual query)                      │
│                                                   │                                                     │
│                 ┌─────────────────────────────────┴─────────────────────────────────┐                   │
│                 │ (If broad global aggregation e.g. "Summarize all themes")         │ (Factual / Store) │
│                 ▼                                                                   ▼                   │
│      ┌───────────────────────┐                                          ┌───────────────────────┐       │
│      │ GraphRAG Global Search│                                          │  1. Query Reframing   │       │
│      │ - Community Summaries │                                          │     & Intent Router   │       │
│      │ - Map-Reduce Synthesiz│                                          │ - RAVEN Sub-queries   │       │
│      └───────────────────────┘                                          │ - Table Intent Detect │       │
│                                                                         └───────────┬───────────┘       │
└─────────────────────────────────────────────────────────────────────────────────────┼───────────────────┘
                                                                                      │
                                                                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 2. PARALLEL MULTI-STORE RETRIEVAL FAN-OUT                               │
│                                                                                                         │
│            ┌────────────────────────────────┬───────────────────────────────┬───────────────────────┐   │
│            ▼                                ▼                               ▼                       ▼   │
│   ┌───────────────────┐            ┌───────────────────┐           ┌───────────────────┐    ┌───────┴──┐│
│   │ 2a. Table Store   │            │ 2b. Clause Store  │           │ 2c. Vector Store  │    │ 2d. Sparse││
│   │ - Tier 1: SQL/Py  │            │ - Legal Provisions│           │ - Dense Embeddings│    │ - BM25 /  ││
│   │   Structured Query│            │ - Risk & Badges   │           │ - Narrative Text  │    │   Fulltext││
│   │ - Tier 2: ANN pgv │            │ - Contract Terms  │           │ - Image Analyses  │    │   Search  ││
│   │ - Row Windows     │            │ - Vector ANN pgv  │           │ - Vector ANN pgv  │    │           ││
│   └────────┬──────────┘            └─────────┬─────────┘           └─────────┬─────────┘    └─────┬─────┘│
│            └─────────────────────────────────┼───────────────────────────────┴────────────────────┘     │
│                                              ▼                                                          │
│                                     [Raw Retrieved Chunks]                                              │
└──────────────────────────────────────────────┬──────────────────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                              3. GRAPHRAG LOCAL MULTI-HOP EXPANSION (NEO4J)                              │
│                                                                                                         │
│   [Query Entities: "Vendor X"] ──► [Neo4j 1-3 Hop Graph Traversal] ──► [Connected Entity Chunks]        │
│                                                                                   │                     │
│                                                                                   ▼                     │
│                                                                        [Postgres Hydration]             │
│                                                                        (Fetch real chunk text           │
│                                                                         from Clause & Vector store)     │
└──────────────────────────────────────────────┬──────────────────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                              4. POOL BALANCING & CROSS-ENCODER RERANKING                                │
│                                                                                                         │
│   [Candidate Pool: Table Chunks + Clause Chunks + Vector Chunks + Graph-Expanded Chunks]                │
│                                              │                                                          │
│                                              ▼                                                          │
│                                  [Balanced Per-Store Pool]                                              │
│                        (Prevents any single store from being starved)                                   │
│                                              │                                                          │
│                                              ▼                                                          │
│                             [BAAI/bge-reranker-large Cross-Encoder]                                     │
│                     (Deep cross-attention scoring between Query & Chunk Content)                        │
│                                              │                                                          │
│                                              ▼                                                          │
│                                 [Top-K High-Confidence Chunks]                                          │
└──────────────────────────────────────────────┬──────────────────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                            5. CONTEXT COMPRESSION & STREAMING SYNTHESIS                                 │
│                                                                                                         │
│   [Context Compression Engine] ──► [LLM Generator: Llama-3-70B / Groq] ──► [SSE Streaming Response]    │
│   - Selective sentence extract       - System instruction enforcement        - Token by token stream    │
│   - Noise elimination                - Fact-grounded reasoning               - Interactive citations    │
│                                      - Source citation tags                  - Markdown tables & images │
└──────────────────────────────────────────────┬──────────────────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                6. SPYDER SUFFICIENCY EVALUATION LOOP                                    │
│                                                                                                         │
│   [SPYDER Evaluator] ──► Sufficient? ──► YES ──► Output Final Answer to User                            │
│                               │                                                                         │
│                               └──► NO  ──► Refine Query ──► Re-enter Retrieval Loop (Max 2 Loops)       │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Step-by-Step Retrieval Lifecycle

### Phase 0: Query Ingestion & Global Routing
1. **Global Aggregation Check**:
   - For broad, document-spanning questions (e.g. *"What are the primary recurring themes across all uploaded documents?"*), queries trigger **GraphRAG Global Search**.
   - Global Search queries hierarchical `:Community` summaries in Neo4j and synthesizes a holistic report using Map-Reduce.
2. **Targeted / Factual Mode**:
   - For specific questions (factual, contractual, tabular, procedural), the query is routed to the multi-store parallel retrieval engine.

### Phase 1: Query Reframing & Intent Routing (RAVEN)
1. **RAVEN Agent**:
   - Analyzes the user's prompt, resolves pronouns, reframes conversational history, and generates targeted sub-queries for compound questions.
2. **Table Intent Classifier**:
   - Detects structured/numerical queries (e.g. *"revenue"*, *"operating margin"*, *"FY23 vs FY24"*, *"highest EBITDA"*) and activates the SQL/Tabular execution engine.

### Phase 2: Parallel Multi-Store Retrieval Fan-Out
Queries are executed **concurrently across separate database connection threads** to minimize latency:
- **`table_store` & `table_row_store`**:
  - Executes structured SQL/Python filters for exact numerical lookups.
  - Queries `table_store` summary embeddings and `table_chunk_store` row windows via `pgvector` HNSW cosine similarity.
- **`clause_store`**:
  - Searches structured legal clauses (e.g., Termination, Liability, Jurisdiction, Indemnification) using 1024-dim dense embeddings.
- **`vector_store`**:
  - Searches narrative prose paragraphs, lists, procedures, and image descriptions.
- **Hybrid Search (BM25 / Trigram Sparse Search)**:
  - Merges dense vector results with sparse keyword matches via Reciprocal Rank Fusion (RRF).

### Phase 3: GraphRAG Local Multi-Hop Expansion (Neo4j)
1. **Entity Extraction**:
   - Identifies named entities in the query (e.g. *"Reliance Retail"*, *"Vendor X"*).
2. **Graph Neighborhood Traversal**:
   - Walks 1–3 hops across relationships (`[:RELATES_TO]`, `[:BOUND_BY]`, `[:SUPPLIES]`) in Neo4j to find connected entities across disparate documents.
3. **PostgreSQL Hydration**:
   - Uses the resulting document and chunk IDs to pull the real textual content from PostgreSQL (`clause_store`/`vector_store`), making cross-document facts available to the pipeline.

### Phase 4: Pool Balancing & Cross-Encoder Reranking
1. **Balanced Pooling (`balanced_pool`)**:
   - Gathers top candidate chunks from each store type (`table`, `clause`, `vector`, `graph`) so that no store is starved due to incomparable raw vector distances.
2. **Deep Cross-Encoder (`bge-reranker-large`)**:
   - Every candidate chunk is paired with the query and evaluated through full cross-attention.
   - Assigns a precise semantic relevance score ($0.0 \dots 1.0$) and ranks chunks strictly by relevance.

### Phase 5: Context Compression & Streaming Synthesis
1. **Extractive Context Compression**:
   - Trims noisy, irrelevant surrounding sentences from prose chunks while keeping numbers, headers, and tables intact.
2. **LLM Synthesis (Groq / Llama 3 70B)**:
   - Generates a grounded, factual response citing exact document IDs, filenames, page numbers, and bounding boxes.
3. **SSE Streaming**:
   - Streams text, formatted markdown tables, and signed preview URLs to the UI.

### Phase 6: SPYDER Feedback & Sufficiency Evaluation
1. **Sufficiency Check**:
   - The SPYDER evaluator checks if the retrieved context answered all facets of the user's question.
2. **Adaptive Re-querying**:
   - If missing information is detected, SPYDER reframes the query to target the missing sub-question and executes a targeted secondary retrieval loop (up to `AGENTIC_MAX_LOOPS`).

---

## 3. How the Document Knowledge Graph is Created

```
Raw Document Text ──► Chunks & Clauses ──► LLM Entity & Relationship Mining ──► Neo4j Graph Assembly ──► Community Clustering
```

1. **Chunk-Level Extraction**:
   - During Stage 6 of ingestion (`graph_build_service.py`), each chunk is analyzed by Groq LLM to extract:
     - **Entities (`:Entity`)**: People, Organizations, Regulations, Products, Metrics, Clause Codes.
     - **Relationships (`[:RELATES_TO]`)**: `[OBLIGATED_TO]`, `[TERMINATES]`, `[GOVERNED_BY]`, `[OWNS]`, `[SUPPLIES]`.
2. **Document Traceability**:
   - A `(:Document)` node is created with `(Entity)-[:MENTIONED_IN]->(Document)` links.
3. **Canonical Merging**:
   - Entities are merged by normalized key across different chunks and PDFs. If Document 1 and Document 2 both mention *"Vendor X"*, they link to the exact same `:Entity` node, forging a bridge between the two documents.
4. **Hierarchical Community Clustering (Louvain/Leiden)**:
   - Densely connected clusters of entities are grouped into `:Community` nodes with auto-generated summary titles and descriptions.

---

## 4. Why Neo4j is Queried Alongside Stores (No Data Loss Guarantee)

### Q: Why does the system query Neo4j? If Neo4j returns an answer when `table_store` has the real answer, is data loss occurring?

### Answer:
**No data loss occurs. The system does NOT use Neo4j instead of `table_store`.**

Here is why:

1. **Parallel Fan-Out, Not Sequential Replacement**:
   - When a question is received, `table_store`, `clause_store`, and `vector_store` are queried **simultaneously in parallel database threads**.
   - If a question asks about tabular data (e.g. revenue, expenses, headcount), `table_store` retrieves the exact structured rows and table chunks.

2. **Neo4j is an Expansion Engine, Not an Isolated Answering Silo**:
   - Neo4j only provides **graph-guided chunk suggestions** (identifying which other chunks in PostgreSQL are related by multi-hop entity connections).
   - Neo4j graph results are hydrated into full chunks from PostgreSQL and placed into the candidate pool **alongside** the `table_store` chunks.

3. **Cross-Encoder Reranker Guarantees Best Store Selection**:
   - All retrieved chunks from all stores pass into the **`bge-reranker-large` Cross-Encoder**.
   - If `table_store` contains the exact tabular numbers matching the user's question, the Cross-Encoder scores the `table_store` chunk highest (e.g. 0.95 vs 0.60 for generic prose) and places the table at **Rank #1**.
   - The LLM synthesizes its answer directly from the top-ranked `table_store` data with full table markdown and cell citations.
