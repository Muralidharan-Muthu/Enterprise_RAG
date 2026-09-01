# Enterprise Multi-Store RAG Architecture

This document provides a high-level, step-by-step flowchart and tool mapping for both the **Ingestion Pipeline** and the **Retrieval & Query Pipeline**.

---

## 1. Ingestion Pipeline Architecture (Top to End)

```mermaid
flowchart TD
    A[1. Document Upload] -->|PDF Document| B[2. API Ingestion Dispatch]
    B -->|Persist File| S1[(Supabase Storage)]
    B -->|Enqueue Job| C[3. Task Queue Broker]
    
    C -->|Parse Queue| D[4. Document Parsing & OCR]
    D -->|Extracted Blocks, Tables, Crops| E[5. Multimodal Vision Analysis]
    
    E -->|Visual Data & OCR Captions| F[6. Document Type Routing]
    F -->|Classified Intent| G[7. Semantic & Layout Chunking]
    
    G -->|Text & Clause Chunks| H[8. Dense Vector Embeddings]
    
    H -->|Store Distribution| I1[(Vector Store)]
    H -->|Store Distribution| I2[(Table Store)]
    H -->|Store Distribution| I3[(Table Row Store)]
    H -->|Store Distribution| I4[(Clause Store)]
    H -->|Store Distribution| I5[(Image Store)]
    
    H -->|Extracted Entities & Relations| J[9. GraphRAG Knowledge Graph]
    J -->|Nodes, Edges, Communities| I6[(Neo4j Graph Database)]
```

### Ingestion Step-by-Step Tool Mapping

| Step | Stage Name | Tool / Technology Used | Description |
| :--- | :--- | :--- | :--- |
| **1** | **Document Upload** | **Next.js 14 (React / TypeScript)** | Drag-and-drop file upload interface with batch staging and taxonomy tagging. |
| **2** | **API Ingestion Dispatch** | **FastAPI (Python 3.11)** | Receives uploads, generates UUIDs, saves original files to Supabase Storage, and dispatches background tasks. |
| **3** | **Task Queue & Broker** | **Redis + Celery** | Asynchronous message broker that manages dedicated task queues (`parse`, `embed`, `graph`). |
| **4** | **Document Parsing & OCR** | **Docling (IBM Granite Layout Models)** | High-precision PDF layout analysis, bounding box extraction, table structure parsing, and image cropping. |
| **5** | **Multimodal Vision Analysis** | **Groq VLM (`qwen/qwen3.8-27b`)** | Analyzes extracted figures, charts, and table crops with sub-3s visual transcription and structured JSON generation. |
| **6** | **Document Routing** | **Groq LLM (`groq/compound-mini`)** | Classifies documents into specialized domains (Financial, Legal, Technical, Policy, General). |
| **7** | **Semantic & Layout Chunking** | **Custom Python Layout Chunker** | Partitions text along natural section boundaries and regex clause patterns with sub-second performance. |
| **8** | **Dense Vector Embeddings** | **BAAI BGE (`BAAI/bge-large-en-v1.5`)** | Computes 1024-dimensional semantic embeddings on CPU/GPU for chunks and clauses. |
| **9** | **Multi-Store Persistence** | **PostgreSQL (Supabase) + pgvector** | Stores vectors in `vector_store`, macro tables in `table_store`, rows in `table_row_store`, clauses in `clause_store`, and images in `image_store`. |
| **10** | **GraphRAG Entity Extraction** | **Groq NER (`qwen/qwen3.6-27b`) + Neo4j** | Extracts entities and typed relationships, building cross-document knowledge graphs and Leiden community summaries. |

---

## 2. Retrieval & Query Pipeline Architecture (Top to End)

```mermaid
flowchart TD
    Q1[1. User Query & Chat] --> Q2[2. Query Reframing & Decomposition]
    Q2 --> Q3[3. Multi-Store Intent & Semantic Routing]
    
    Q3 -->|Semantic Vector Search| R1[(Vector Store - pgvector)]
    Q3 -->|Exact Keyword Search| R2[(Full-Text Search - tsvector)]
    Q3 -->|SQL Pushdown Aggregation| R3[(Table Store & Table Row Store)]
    Q3 -->|Contract Clause Retrieval| R4[(Clause Store)]
    Q3 -->|Knowledge Graph Traversal| R5[(Neo4j Graph Database)]
    
    R1 & R2 & R3 & R4 & R5 --> Q4[4. Reciprocal Rank Fusion - RRF]
    Q4 --> Q5[5. Cross-Encoder Reranking]
    Q5 --> Q6[6. Extractive Context Compression]
    
    Q6 --> Q7[7. Corrective Self-RAG - SPYDER]
    Q7 -->|Sufficient Context| Q8[8. Multi-Document Synthesis]
    Q7 -->|Insufficient / Missing Info| Q2
    
    Q8 -->|Stream Token by Token| Q9[9. SSE Streaming UI & Citations]
```

### Retrieval Step-by-Step Tool Mapping

| Step | Stage Name | Tool / Technology Used | Description |
| :--- | :--- | :--- | :--- |
| **1** | **User Query & Chat** | **Next.js 14 Frontend** | Conversational UI supporting multi-turn chat, markdown rendering, and interactive citations. |
| **2** | **Query Reframing & Decomp** | **RAVEN (`groq/compound-mini`)** | Reframes conversational context, generates sub-queries, and resolves ambiguous pronouns. |
| **3** | **Intent & Semantic Routing** | **Embedding Centroids + Groq Router** | Routes queries to the optimal stores using cosine similarity against store centroids and keyword rules. |
| **4** | **Hybrid Multi-Store Search** | **PostgreSQL (`pgvector` + `tsvector`) + Neo4j** | Executes dense vector search, BM25 full-text keyword search, structured SQL queries, and multi-hop graph traversals. |
| **5** | **Reciprocal Rank Fusion** | **RRF Algorithm ($k=60$)** | Blends and normalizes rank scores from disparate semantic and keyword search channels. |
| **6** | **Cross-Encoder Reranking** | **`cross-encoder/ms-marco-MiniLM-L-6-v2`** | Performs deep query-passage cross-attention scoring to re-order top retrieval candidates. |
| **7** | **Context Compression** | **Extractive Relevance Filter** | Prunes irrelevant sentences from retrieved chunks while preserving verbatim text and citation anchors. |
| **8** | **Corrective Self-RAG** | **SPYDER (`groq/compound`)** | Evaluates retrieval sufficiency and flags missing information to trigger targeted follow-up queries if needed. |
| **9** | **Multi-Document Synthesis** | **Groq LLM (`openai/gpt-oss-120b`)** | Synthesizes complex multi-document answers with verifiable citations (`[Doc: X, Page: Y]`). |
| **10** | **Streaming Delivery** | **Server-Sent Events (SSE) + FastAPI** | Streams generated markdown and citations to the frontend in real time. |

---

## 3. Technology Stack Overview

| Category | Component / Tool |
| :--- | :--- |
| **Frontend** | Next.js 14 (App Router), React, TailwindCSS, Lucide Icons, Poppins Typography |
| **Backend API** | FastAPI, Python 3.11, Pydantic v2, Uvicorn |
| **Background Processing** | Celery 5.4, Redis 7 (Docker) |
| **Document Parsing** | Docling (IBM Granite), EasyOCR (CPU, optional) |
| **Multimodal / Vision AI** | Groq LPU Inference (`qwen/qwen3.8-27b`) |
| **Large Language Models** | Groq (`openai/gpt-oss-120b`, `groq/compound`, `groq/compound-mini`, `qwen/qwen3.6-27b`) |
| **Embeddings & Reranking** | BAAI BGE-Large-EN-v1.5 (1024-dim), Cross-Encoder MS-Marco MiniLM-L-6-v2 |
| **Relational & Vector DB** | PostgreSQL (Supabase) + `pgvector` + `tsvector` |
| **Knowledge Graph DB** | Neo4j Aura (Cypher Query Language) |
| **File Storage** | Supabase Storage (Private S3-compatible buckets) |
