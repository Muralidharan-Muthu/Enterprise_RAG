
## Diagrams

### Ingestion — high level (staged two-task chain)

```mermaid
flowchart TD
    A[User uploads document] --> B[ingestion.py: register document_registry\n+ ingestion_jobs, dispatch chain]
    B --> P[parse_document_task]
    P --> P1[Parse: Docling extracts text + tables\nmerge multi-page continuations]
    P1 --> P2[Stage ParsedDocument to Supabase Storage\nstatus = parsed]
    P2 --> Q[chunk_embed_store_task]
    Q --> Q1[Load staged ParsedDocument]
    Q1 --> Q2[Route: document_type via Gemma 4]
    Q2 --> Q3[Chunk text + row-window tables 25 rows]
    Q3 --> Q4[Embed: BGE for text, table summaries,\nbig-table windows small tables skipped]
    Q4 --> Q5[Reconstruct + enrich tables:\nVLM transcription, fiscal_year/currency/category]
    Q5 --> R{"table row_count > 25?"}
    R -- "no (small)" --> R1[table_store row:\nstructured_content + embedding here\nNO table_chunk_store rows]
    R -- "yes (big)" --> R2[table_store row: structured_content NULL\n+ table_chunk_store: one row per 25-row window\nwith per-window structured_content + embedding]
    R1 --> Z[Graph stage + cleanup\nstatus = completed]
    R2 --> Z
```

### Ingestion — low level

```mermaid
flowchart TD
    A[Upload file] --> A1[ingestion.py: validate, upload to Storage,\ncreate document_registry + ingestion_jobs]
    A1 --> A2[dispatch_ingestion: chain\nparse_document_task -> chunk_embed_store_task]

    subgraph PARSE[parse_document_task]
        A2 --> B{PDF?}
        B -- yes --> B1[parse_document_chunked\npage-chunked Docling]
        B1 -- fails --> B2[_parse_with_docling whole-doc]
        B2 -- fails --> B3[_parse_fallback PyMuPDF]
        B -- no --> B4[_parse_non_pdf single Docling pass]
        B1 --> C[_extract_tables]
        B2 --> C
        B4 --> C
        C --> C1[_detect_merged_cells + _parse_table_data]
        C1 --> C2[_merge_continued_tables\ncross-page continuation merge]
        C2 --> C3[ExtractedTable: raw_text, markdown_text,\ncrop image, caption]
        C3 --> C4[save_parsed -> Storage staging\nstatus = parsed]
    end

    C4 --> D[chunk_embed_store_task: load_parsed]

    subgraph EMBED[chunk_embed_store_task]
        D --> E[classify_document Gemma 4 -> document_type]
        E --> F[chunk_document: text chunks]
        F --> G[chunk_tables + build_row_windows\nrow-count windows of 25 rows]
        G --> G1[Drop windows of small tables\nrow_count <= 25 before embedding]
        G1 --> H[embed_passages: text chunks +\nparent summaries + big-table windows]
        H --> I[reconstruct_tables_with_vlm + enrich_table]

        I --> M{"row_count > 25?"}
        M -- "no (small)" --> M1[_store_tables: table_store row\nstructured_content + sc_embedding SET]
        M -- "yes (big)" --> M2[_store_tables: table_store row\nstructured_content + sc_embedding NULL]
        M1 --> Z[status = completed]
        M2 --> N[build_window_structured_content per window\nbatch-embed slices]
        N --> N1[insert_table_chunks: table_chunk_store rows\nserialized_text + embedding +\nstructured_content JSON slice + sc_embedding\narity-padded to 12 cols if caller short]
        N1 --> Z
    end
```

### Retrieval — high level

```mermaid
flowchart TD
    A[User asks question in UI] --> B[Backend embeds query\nBGE + query instruction prefix]
    B --> C[Select relevant stores\nvector / clause / research / table]
    C --> D[Search table_store + table_chunk_store\nvector ANN search]
    D --> E[Pool + rerank results\nacross all active stores]
    E --> F[Synthesize answer via Gemma 4\nusing top ranked chunks]
    F --> G[Return answer + citations to UI]
```

### Retrieval — low level

```mermaid
flowchart TD
    A[UI submits query] --> A1[Next.js proxy -> POST /api/v1/query]
    A1 --> B[query.py: parse QueryRequest\ntop_k, document_types, table_filters, use_reranker]
    B --> C[embed_query: prefix + BGE 1024-dim vector]

    C --> D{document_types given?}
    D -- no --> D1[classify_intent -> _select_stores]
    D -- yes --> D2[use given types directly]
    D1 --> E[Concurrent per-store queries]
    D2 --> E

    E --> F[_query_table_store]
    F --> F1[Child ANN on COALESCE\nstructured_content_embedding, embedding\ntext = COALESCE structured_content, serialized_text\n+ WHERE status=completed, type/doc/table_filters]
    F1 --> F2[Join table_chunk_store -> table_store -> document_registry]
    F2 --> F3[Dedup per table_id:\nkeep top TABLE_MAX_WINDOWS_PER_QUERY_RESULT]
    F3 --> F5[_query_table_store_parent_only\nANN on COALESCE table_store.sc_embedding, embedding\ncovers small tables + big tables with no child hit]
    F5 --> G[Table store candidate chunks\nbig-table windows + small-table parents]

    G --> H[balanced_pool: cap per-store\nRERANK_PER_STORE_CAP=8]
    H --> I{use_reranker?}
    I -- yes --> I1[rerank: BGE-reranker-large\ntop 40, score 0.3*sigmoid + 0.7*minmax]
    I -- no --> I2[Reciprocal Rank Fusion]
    I1 --> J[Final ranked top_k chunks]
    I2 --> J

    J --> K{relevance below threshold\nor zero chunks?}
    K -- yes --> K1[Fallback: no-results\nor off-topic reply]
    K -- no --> L[_build_context: numbered blocks\ntable chunks include table_markdown]
    L --> Mx[synthesize via Gemma 4]
    Mx -- error --> M1[_fallback: concatenate chunk texts]
    Mx --> Nx[_citation_from_chunk: filename, page,\ntable_markdown, bbox, signed URL]
    K1 --> Nx
    M1 --> Nx
    Nx --> O[Return answer, confidence, sources_used]
```

### `table_store` / `table_chunk_store` internal architecture — high level

Parent/child split driven by table size (`TABLE_CHUNK_MAX_ROWS = 25`).

```mermaid
flowchart TD
    T[Extracted table\nheaders + rows] --> Q{"row_count > 25?"}

    Q -- "no (≤25 rows)" --> S1[table_store row]
    S1 --> S1a[embedding = parent summary]
    S1 --> S1b[structured_content = VLM/markdown\nstructured_content_embedding SET]
    S1 --> S1c[NO table_chunk_store rows]

    Q -- "yes (>25 rows)" --> B1[table_store row]
    B1 --> B1a[embedding = parent summary]
    B1 --> B1b[structured_content = NULL\nstructured_content_embedding = NULL]
    B1 --> C1["table_chunk_store: N = ceil(rows/25) windows"]
    C1 --> C1a[each window: 25 rows\nserialized_text + embedding]
    C1 --> C1b[each window: structured_content JSON slice\n+ structured_content_embedding]

    S1 -. "query: parent-only ANN" .-> QP[COALESCE\nstructured_content_embedding, embedding]
    C1 -. "query: child ANN" .-> QC[COALESCE\nstructured_content_embedding, embedding]
```

### `table_store` / `table_chunk_store` internal architecture — low level

```mermaid
flowchart LR
    subgraph TS["table_store — one row per logical table"]
        direction TB
        TSk[id PK, document_id FK, table_index\nrow_count, col_count, page_number, bbox]
        TSt[raw_text, markdown_text, json_data,\ncsv_data, table_summary, enrichment fields]
        TSe[embedding vector1024\n= parent summary]
        TSs["structured_content + structured_content_embedding\nSET when row_count ≤ 25, else NULL"]
    end

    subgraph TCS["table_chunk_store — big tables only, N windows"]
        direction TB
        TCk[id PK, document_id FK,\ntable_id FK -> table_store.id, table_index]
        TCw[chunk_index, row_start, row_end\ninclusive 25-row window]
        TCt["serialized_text\nCol: val; ... per row"]
        TCe[embedding vector1024\n= serialized_text]
        TCs[structured_content JSON slice indent=2\n+ structured_content_embedding vector1024\nHNSW idx_table_chunk_store_sc_embedding]
    end

    TSk -->|"table_id FK (row_count > 25 only)\nON DELETE CASCADE"| TCk

    QRY[Query embedding] -->|"child ANN\nCOALESCE(sc_embedding, embedding)"| TCe
    QRY -->|"parent-only ANN\nCOALESCE(sc_embedding, embedding)"| TSe
    TCs -. "returned text\nCOALESCE(structured_content, serialized_text)" .-> OUT[Ranked chunk -> synthesis]
    TSs -. "small-table text" .-> OUT
```
