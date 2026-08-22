# Multi-Store RAG — Ingestion Pipeline Architecture

## Ingestion Pipeline Architecture & Flowchart

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 0. UPLOAD & DISPATCH                                   │
│  [User: PDF/DOCX/XLSX/HTML] ──► [FastAPI /api/v1/ingest/pipeline]                     │
│                                           │                                            │
│            ┌──────────────────────────────┼──────────────────────────────┐             │
│            ▼                              ▼                              ▼             │
│  [Supabase Storage]            [PostgreSQL Registry]            [Celery Task Queue]    │
│  - Raw file in rag-documents   - document_registry (queued)     - Redis broker queue   │
│                                - ingestion_jobs (progress 0%)   - Async worker pickup  │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              1. PARSING & IMAGE EXTRACTION                             │
│                                                                                        │
│   [Supabase Download] ──► [PyMuPDF Pre-scan] ──► [Docling Layout & Table Engine]       │
│                                                          │                             │
│                                                          ▼                             │
│                                              [ParsedDocument Object]                   │
│                                            (text, tables, image coords)                │
│                                                          │                             │
│            ┌─────────────────────────────────────────────┴───────────────┐             │
│            │ (If images present)                                         │             │
│            ▼                                                             ▼             │
│   ┌────────────────────────────────┐                            ┌───────────────────┐  │
│   │   1b. Visual Image Pipeline    │                            │  2. Doc Routing   │  │
│   │   - EasyOCR Text Extraction    │                            │  - Groq 70B LLM   │  │
│   │   - ImagePrefilter Deduplication                             │  - Rule Fallback  │  │
│   │   - Groq Multimodal VLM        │                            └─────────┬─────────┘  │
│   │   - Append to image_store      │                                      │            │
│   └────────────────────────────────┘                                      │            │
└───────────────────────────────────────────────────────────────────────────┼────────────┘
                                                                            │
                                                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              3. TYPE-AWARE CHUNKING                                    │
│                                                                                        │
│                               [Document Type Decision]                                 │
│                                           │                                            │
│            ┌──────────────────────────────┴──────────────────────────────┐             │
│            ▼                                                             ▼             │
│   [financial / policy / entity / research]                            [legal]          │
│   - Semantic Sentence Breakpoint Splitter                     - Groq Clause Extractor  │
│   - BGE Embedding Distance Gates                             - Bound + Risk Profiling │
│   - Context Section Header Breadcrumbs                        - Fallback: Regex+Enrich │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                         4. TABLE PROCESSING & VECTORIZATION                            │
│                                                                                        │
│   [Table BBox Crops] ──► [Groq Table VLM] ──► [Faithfulness Gate] ──► [Table Enrich]  │
│                                                                              │         │
│   [All Processed Chunks & Structured Tables] ──► [BAAI/bge-large-en-v1.5 Embedding]    │
│                                                  (Dense 1024-dim Vectors)              │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               5. MULTI-STORE STORAGE                                   │
│                                                                                        │
│                                   [Store Router]                                       │
│                                           │                                            │
│       ┌───────────────────┬───────────────┼───────────────────┬──────────────────┐     │
│       ▼                   ▼               ▼                   ▼                  ▼     │
│ ┌───────────┐       ┌───────────┐   ┌───────────┐       ┌───────────┐      ┌───────────┐
│ │vector_    │       │clause_    │   │table_     │       │table_row_ │      │image_     │
│ │store      │       │store      │   │store      │       │store      │      │store      │
│ │pgvector   │       │pgvector + │   │Macro table│       │JSONB row  │      │Image crops│
│ │1024-dim   │       │JSONB risk │   │summaries  │       │pushdown   │      │+ OCR text │
│ └───────────┘       └───────────┘   └───────────┘       └───────────┘      └───────────┘
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               6. KNOWLEDGE GRAPH & AUDIT                               │
│                                                                                        │
│   [Groq Entity / Relation NER] ──► [Neo4j Aura Graph] ──► [Louvain Community Detect]   │
│                                                                     │                  │
│   [Lineage & Table Row Invariant Gate] ◄────────────────────────────┘                  │
│              │                                                                         │
│              ▼                                                                         │
│   [Pipeline Status = COMPLETED (Document Ready for Query)]                             │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Complete Pipeline Summary Table

| Stage | Name | Input | Processing Technology | Output & Datastore |
| :--- | :--- | :--- | :--- | :--- |
| **0** | **Upload & Dispatch** | Raw document bytes | FastAPI, Supabase Storage, Celery + Redis | `rag-documents` bucket, `document_registry`, `ingestion_jobs` |
| **1** | **Parsing** | Raw PDF/DOCX/XLSX | PyMuPDF (fast pre-scan) + Docling Engine | `ParsedDocument` (text blocks, table grids, image bboxes) |
| **1b** | **Image Extraction** | Cropped document figures | EasyOCR + Perceptual Deduplication + Groq VLM | `image_store` (OCR text, visual summaries, crop images) |
| **2** | **Document Routing** | Document excerpt & layout | Groq 70B Intent Classifier (with heuristic fallback) | `RouterResult` (`financial`, `legal`, `entity`, `policy`, `research`) |
| **3** | **Type-Aware Chunking** | Text blocks & structure | Semantic Splitter (Cosine distance) & Groq Clause Extractor | Chunk objects with context breadcrumbs & risk metadata |
| **4** | **Table Reconstruction & Vectorization** | Table crops & structured chunks | Table VLM + Numeric Faithfulness Gate + BGE Large Embeddings | 1024-dim dense vectors & structured JSON objects |
| **5** | **Multi-Store Storage** | Vectorized chunks & tables | Specialized PostgreSQL Datastores | `vector_store`, `clause_store`, `table_store`, `table_row_store` |
| **6** | **Knowledge Graph** | Chunks & extracted entities | Groq NER + Neo4j Graph DB + Louvain Community Detection | Neo4j Knowledge Graph nodes, relationship edges, communities |
| **7** | **Finalization** | Entire pipeline outputs | Lineage and table count invariant sanity verification | `status = completed`, live query ready |

---

## Detailed Stage Breakdown

### Stage 0: Upload & Asynchronous Dispatch

1. **User Upload**: Files (PDF, DOCX, XLSX, HTML, Markdown) are uploaded via the Next.js frontend to `POST /api/v1/ingest/pipeline`.
2. **Storage Write**: Raw file binaries are uploaded to the Supabase Storage bucket (`rag-documents`).
3. **Database Registration**:
   - `document_registry` creates a document record in `queued` status.
   - `ingestion_jobs` initializes tracking for live frontend stage updates.
4. **Celery Task**: `ingest_document.apply_async()` pushes the document job to the Redis `ingestion` queue. The API responds immediately with the `job_id`.

---

### Stage 1: Document Parsing (`document_parser.py`)

- **PyMuPDF Pre-scan**: Instantly determines total page count and initial image counts so the UI immediately shows the full document workload.
- **Docling Pipeline**: Parses complex layouts, multi-column reading orders, table structure grids, embedded images, section hierarchy, and bounding boxes.
- **Real-time Callbacks**: `_on_parse_progress()` updates `ingestion_jobs.stage_detail` after every page chunk so users see live page completion counters.

---

### Stage 1b: Image Extraction (`_build_image_records_parallel`)

1. **Pre-filter**: `ImagePrefilter` applies perceptual hashing and dimension gates to skip decorative icons, divider lines, and duplicates.
2. **OCR**: EasyOCR extracts verbatim text from figures and diagrams.
3. **Storage Upload**: Image crops are saved to `images/{document_id}/`.
4. **VLM Captioning**: Multimodal VLM (Groq) generates detailed structural captions and descriptions via bounded parallel workers (`ThreadPoolExecutor`).
5. **Incremental Writes**: Crops are appended to `image_store` immediately upon completion so the UI image count climbs in real time.

---

### Stage 2: Routing & Classification (`router_service.py`)

- **Groq 70B Classifier**: `classify_document()` evaluates the document excerpt, structure, and table density to determine the primary document type (`financial`, `legal`, `entity`, `policy`, `research`).
- **Rule-based Fallback**: Keyword heuristics ensure resilient classification if the LLM is unreachable.

---

### Stage 3: Type-Aware Chunking (`chunker.py` + `groq_clause_extractor.py`)

- **Text & Prose**: Split by semantic sentence boundaries using embedding cosine-distance percentile breakpoints (LlamaIndex-style) with section breadcrumb preservation.
- **Legal Documents**:
  - `extract_clauses_groq()` extracts clause boundaries and rich metadata (risk tier, governing law, obligor, obligee, monetary values) in a single pass.
  - Falls back to regex segmentation + `enrich_clauses_batch()` if required.

---

### Stage 4: Table VLM & Embedding Generation (`embedding_service.py`)

1. **Table Crop Upload**: Table bounding box crops are uploaded to Supabase Storage.
2. **VLM Reconstruction**: `reconstruct_tables_with_vlm()` cleans OCR errors and recovers merged cells.
3. **Faithfulness Gate**: Reconciles Docling grid output against VLM output — VLM wins only if numeric fidelity is strictly maintained.
4. **Table Enrichment**: Computes `fiscal_year`, `currency`, `table_category`, and `detected_units`.
5. **Embedding**: `BAAI/bge-large-en-v1.5` produces dense 1024-dimensional vectors for text chunks, table parent summaries, and structured table content.

---

### Stage 5: Multi-Store Write (`storage_service.py`)

Chunks and structured data are routed to their designated specialized datastores:

| Datastore | Target Data | Storage Structure | Purpose |
|---|---|---|---|
| `vector_store` | Prose, handbooks, SOPs, research | pgvector HNSW 1024-dim | Hybrid semantic + BM25 keyword search |
| `clause_store` | Contracts, MSAs, legal clauses | pgvector + JSONB metadata | Risk filtering + clause similarity |
| `table_store` | Macro tables, financial matrices | Summary text + pgvector | Table-level matching and overview |
| `table_row_store` | Every individual table row | Typed `row_numeric` & `row_data` JSONB | Single-pass SQL pushdown (`SUM`, `AVG`, `WHERE > 50K`) |
| `image_store` | Figures, charts, diagrams | Supabase Storage + OCR text | Visual grounding & figure citations |

---

### Stage 6: Knowledge Graph Generation (`graph_build_service.py`)

1. **Entity Extraction**: Groq extracts named entities (organizations, people, locations, metrics) and relationships.
2. **Neo4j Graph Write**: Merges nodes and creates typed relationship edges across documents.
3. **Louvain Community Detection**: Detects dense clusters of connected entities and computes hierarchical summaries for global graph reasoning.

---

### Finalization & Integrity Gates

Before updating document status to `completed`:
1. **Lineage Completeness**: Verifies all table crop candidates successfully registered a `source_image_id`.
2. **Count Invariant Check**: Ensures the count of parsed tables matches the exact number of `table_store` rows inserted.
