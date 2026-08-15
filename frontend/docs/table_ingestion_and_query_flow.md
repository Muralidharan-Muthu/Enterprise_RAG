# Table Document: Ingestion → Storage → Query Flow

Schema: `multi_store_rag_working`. Covers documents containing tables end-to-end: (1) ingestion until data lands in `table_store` / `table_chunk_store`, (2) query-time retrieval from those stores to the final answer.

> **Pipeline shape.** Ingestion runs as a **staged two-task Celery chain** in `app/services/ingestion_tasks.py`, not one monolithic task. `ingest_document` (in `ingestion_orchestrator.py`) only **delegates** — it calls `dispatch_ingestion()` which builds the chain `parse_document_task → chunk_embed_store_task` and returns `{delegated: True}`. The `ParsedDocument` is handed between the two tasks via Supabase Storage staging (`parse_staging_service`), so the two tasks can run on different workers.

---

## Part 1 — Ingestion: upload → `table_store` + `table_chunk_store`

### Stage 0: Upload, registration & dispatch
`app/api/routes/ingestion.py` — `_register_and_dispatch()`
File uploaded, MIME/size validated, pushed to Supabase Storage (`SUPABASE_STORAGE_BUCKET`, default `rag-documents`). Rows created in `document_registry` and `ingestion_jobs`. `dispatch_ingestion()` (`ingestion_tasks.py:766`) builds the chain with `task_id == job_id`:
```
parse_document_task(document_id, storage_path, job_id)
  → chunk_embed_store_task(prev, document_id, job_id)
```

### Task A — `parse_document_task` (`ingestion_tasks.py:68`)

**Stage 1: Parsing.** `current_stage='parsing'`. Downloads the source file to a local tempfile, runs `document_parser.parse_document()`:
- PDFs: `parse_document_chunked()` (page-chunked Docling, live progress) → fallback `_parse_with_docling()` (whole-doc) → fallback `_parse_fallback()` (PyMuPDF text dump).
- Non-PDF (.docx/.pptx/.xlsx/.html/.md): single Docling pass via `_parse_non_pdf()`.
- Produces `ParsedDocument` with `.tables` (list of `ExtractedTable`), `.text_blocks`, metadata.

**Table extraction** — `document_parser._extract_tables()`:
- per Docling table: `_parse_table_data()` (headers/rows), `_detect_merged_cells()` (span map)
- skips empty 0×0 grids (chart/figure placeholders)
- image crop via `table.get_image(doc)`, fallback `_render_table_crop_fitz()` (renders bbox from the PDF)
- caption via `table.caption_text(doc)`; builds `raw_text` + `markdown_text`
- `_merge_continued_tables()` — detects identical headers across consecutive pages and merges page-split fragments into **one logical table** (`table_metadata` records the merged pages; e.g. a 6-page table → one `ExtractedTable` with all rows).

**Staging.** `parse_staging_service.save_parsed()` serializes the `ParsedDocument` to `staging/{document_id}/parsed.json` (+ `table_{i}.png` crops) in Supabase Storage. `document_registry.status='parsed'`.

### Task B — `chunk_embed_store_task` (`ingestion_tasks.py:235`)

Loads the staged `ParsedDocument` back from storage (`load_parsed()`). `store_chunks()` clears any existing chunks first, so this task is idempotent / re-runnable.

**Stage 1b: Images.** OCR/VLM handling of embedded images (not table-specific).

**Stage 2: Routing.** `current_stage='routing'` → `router_service.classify_document()`. Gemma 4 (CDAC, `GEMMA4_BASE_URL`) classifies `document_type` + confidence; `_rule_based_classify()` fallback when unavailable / low confidence. **Tables are stored regardless of `document_type`** — table storage is not limited to `financial` docs.

**Stage 3: Chunking.** `chunker.chunk_document()` → text chunks (legal branch extracts clauses; not table-relevant).

**Stage 4: Embedding.** `embedding_service.embed_passages()` (`BAAI/bge-large-en-v1.5`, 1024-dim, no prefix on passages — prefix is query-side only). For tables:
- `table_chunker.chunk_tables()` → parent summary text + child row-windows.
- **Row-count windowing** (`build_row_windows()`): windows split by **row count** (`TABLE_CHUNK_MAX_ROWS`, default **25**), NOT token budget. A 200-row table → exactly `ceil(200/25) = 8` windows of 25 rows. `max_tokens` only bounds a single pathologically-wide row (per-row truncation ceiling = `max_tokens*4` chars). Wide vs. narrow rows no longer change the window count.
- **Small-table skip** (`ingestion_tasks.py:429-443`): child windows of tables with `row_count ≤ 25` are **dropped before embedding** — a small table is a single window = the whole table, already fully represented in `table_store`. Only tables with `row_count > 25` keep children (and get embedded).
- Parent summaries embedded for **every** table; child windows embedded for **big tables only**.

**Stage 5: Table reconstruction & enrichment** (`ingestion_tasks.py:492-546`):
- Table crop PNGs re-uploaded to `tables/{document_id}/{table_index}.png`.
- `reconstruct_tables_with_vlm()` (bounded parallel, `VLM_MAX_CONCURRENCY`) → per-table `{method, confidence, extraction_quality, provenance, vlm_ocr_text, structured_content}`, reconciled against Docling (Docling stays the numeric source of truth unless the VLM wins the faithfulness gate).
- `table_enrichment.enrich_table()` → `fiscal_year, reporting_period, currency, table_category, detected_units, table_summary` (prefers VLM structured content, else regex/keyword rules).

**Stage 5 — write `table_store`** (`store_chunks()` → `storage_service._store_tables()`), one row per logical table:
- `json_data` (JSONB), `csv_data`; flags `has_currency` / `has_percentages` / `has_numeric_data`
- `embedding` = parent-summary embedding
- **`structured_content` gated by size:** for **small tables (`row_count ≤ 25`)**, `structured_content` = VLM output (or markdown/raw_text fallback) and `structured_content_embedding` = its embedding. For **big tables (`row_count > 25`)**, both are **NULL** — the content instead lives sliced per-window in `table_chunk_store` (next step), never duplicated as one diluted whole-table vector.
- enrichment + lineage columns; bulk insert via `execute_values`, `RETURNING id`.

**Stage 5a-ext — write `table_chunk_store`** (`ingestion_tasks.py:575-660` → `table_chunk_store.insert_table_chunks()`), **big tables only** (small tables were filtered out in Stage 4). Maps `table_index → table_store.id` from the returned UUIDs. For each `TableRowChunk`:
- base columns: `document_id, table_id (FK), table_index, chunk_index, row_start, row_end, serialized_text, page_number, embedding, chunk_metadata`
- **per-window structured columns (migration 018):** `structured_content` = `build_window_structured_content()` — a pretty-printed JSON slice `{"title", "headers", "rows"}` of just this window's 25 canonical rows (`indent=2`, `ensure_ascii=False` so ₹/€/£ stay literal); `structured_content_embedding` = BGE embedding of that slice (all window slices embedded in one batch).
- `insert_table_chunks()` is **arity-tolerant**: a caller supplying only the 10 base values is padded to 12 with NULLs rather than crashing `execute_values`.

> **Note.** The same table-storing logic exists in `ingestion_orchestrator.ingest_document`'s inline path, but production runs the **staged** `chunk_embed_store_task`. Both are kept in sync.

**Stage 6: Graph** (best-effort GraphRAG), then staging cleanup, then `document_registry.status='completed'` (or `completed_with_errors` on a lineage/count mismatch).

**Backfill:** `scripts/backfill_table_chunks.py` rebuilds these windows for any big table missing children (idempotent — skips tables that already have children), so a document ingested before this feature was live can be populated without re-uploading.

---

## Part 2 — Query: UI question → table-store retrieval → answer

### Step 1: Query submitted (UI)
`frontend/src/app/api/v1/query/route.ts` — Next.js proxy forwards `QueryRequest` to backend `/api/v1/query`.

### Step 2: Request received, query embedded
`app/api/routes/query.py` — `QueryRequest = {query, document_types?, document_id?, top_k, use_reranker, table_filters?, enable_hybrid, enable_graphrag}`. `table_filters` can carry `currency, fiscal_year, table_category, numeric_only, min_quality`.
`retriever_service.retrieve()` → `embedding_service.embed_query()`: query prefixed with `"Represent this question for searching relevant passages: "`, encoded with the same BGE model → 1024-dim vector.

### Step 3: Store selection
Unless `document_types` are given, `classify_intent()` + `_select_stores()` pick which stores to hit. Active stores queried concurrently (ThreadPoolExecutor, one pooled DB connection each).

### Step 4: Table store ANN search — `retriever_service._query_table_store()`
If `TABLE_CHILD_SEARCH_ENABLED` (default):
- **Child ANN** on `COALESCE(table_chunk_store.structured_content_embedding, table_chunk_store.embedding) <=> query_embedding` — big-table windows match on their structured_content embedding; any window without one falls back to the serialized-text `embedding`.
- Returned chunk text = `COALESCE(structured_content, serialized_text)` — big-table windows surface the structured JSON slice.
- WHERE: `document_registry.status='completed'`, `embedding` not null, optional `document_type IN (...)`, optional `document_id = ...`, optional metadata filters (`_table_filter_sql()` against `table_store` columns).
- Joins `table_chunk_store → table_store → document_registry`; top-`k` per store (`RETRIEVAL_TOP_K_PER_STORE`, default 15).
- **Dedup**: per `table_id`, keeps the best `TABLE_MAX_WINDOWS_PER_QUERY_RESULT` (default 2) windows.
- **Parent path** (`_query_table_store_parent_only()`): ANN on `COALESCE(table_store.structured_content_embedding, table_store.embedding)`, same filters, excludes tables already covered by child hits. **Small tables (no children by design) are always retrieved here.**

### Step 5: Pool + rank across stores
`query.py:_rank_chunks()` → `balanced_pool()` caps each store's contribution (`RERANK_PER_STORE_CAP=8`). Then:
- `use_reranker=True`: `reranker_service.rerank()` — `BAAI/bge-reranker-large` CrossEncoder (max 512 tokens), scores top `MAX_RERANK_CANDIDATES=40`, blended `0.3·sigmoid(logit) + 0.7·minmax`.
- else: Reciprocal Rank Fusion across store rankings.

### Step 6: Synthesis
`synthesis_service.synthesize()` — no chunks → canned no-results; below `_MIN_RELEVANCE_THRESHOLD` → off-topic fallback; else `_build_context()` builds numbered blocks (table chunks include their `table_markdown`) up to `SYNTHESIS_CONTEXT_MAX_CHARS`, sent to Gemma 4. On Gemma error → `_fallback()` (concatenated chunk texts).

### Step 7: Response with citations
`query.py:_citation_from_chunk()` → `CitationItem {document_id, filename, chunk_text, store_type, relevance_score, page_number, table_markdown, bbox, signed_url}` (signed Supabase URL for table crops/PDFs). Final JSON: `{answer, confidence, confidence_breakdown, sources_used, notes}`.

---

## Key parameters

| Item | Value |
|---|---|
| Embedding model | `BAAI/bge-large-en-v1.5`, 1024-dim |
| Reranker | `BAAI/bge-reranker-large` (CrossEncoder, max 512 tokens) |
| Vector index | HNSW (`m=16, ef_construction=128`) on `table_store.embedding`, `table_store.structured_content_embedding`, `table_chunk_store.embedding`, `table_chunk_store.structured_content_embedding` (`idx_table_chunk_store_sc_embedding`, migration 018) |
| Row-window size | 25 rows/window (`TABLE_CHUNK_MAX_ROWS`) — row-count split, not token-budget. 200 rows → 8 windows |
| Small-table threshold | `row_count ≤ 25` → **no** `table_chunk_store` rows (whole table lives in `table_store`) |
| Retrieval top-k per store | 15 (`RETRIEVAL_TOP_K_PER_STORE`) |
| Child windows per table (query) | 2 (`TABLE_MAX_WINDOWS_PER_QUERY_RESULT`) |
| Rerank candidate pool | 40 (`MAX_RERANK_CANDIDATES`) |
| Per-store cap before rerank | 8 (`RERANK_PER_STORE_CAP`) |

## Relevant schema

```sql
-- multi_store_rag_working.table_store   (one row per logical table)
id, document_id, table_index, table_title, page_number, bbox,
raw_text, markdown_text, json_data, csv_data, row_count, col_count,
has_numeric_data, has_currency, has_percentages, detected_units,
context_before, context_after, table_summary, embedding vector(1024),
fiscal_year, reporting_period, currency, table_category, table_metadata,
created_at,
image_storage_path, source_image_id, extraction_method, extraction_quality,
source_confidence, provenance,
structured_content, structured_content_embedding vector(1024)

-- multi_store_rag_working.table_chunk_store   (big tables only, row_count > 25)
id, document_id, table_id (FK -> table_store.id), table_index, chunk_index,
row_start, row_end, serialized_text, page_number, embedding vector(1024),
chunk_metadata, created_at,
structured_content, structured_content_embedding vector(1024)   -- migration 018
```

**Storage rule (`row_count` vs. `TABLE_CHUNK_MAX_ROWS = 25`):**

| Table size | `table_store.structured_content` (+ embedding) | `table_chunk_store` rows |
|---|---|---|
| `≤ 25` rows | populated (whole table) | none |
| `> 25` rows | NULL | one row per 25-row window; each carries its own `structured_content` slice + embedding |

---

## Column reference: `table_store`

| Column | Description |
|---|---|
| `id` | Primary key (UUID). Referenced by `table_chunk_store.table_id`. |
| `document_id` | FK → `document_registry.id`. Cascades on delete. |
| `table_index` | 0-based position within the document (extraction order). Keys the crop path `tables/{document_id}/{table_index}.png` and maps VLM analysis back to the table. |
| `table_title` | Caption from Docling (`table.caption_text`), if any. |
| `page_number` | Page the table appears on (post continuation-merge = page of the first fragment). Citations / PDF deep-link. |
| `bbox` | JSONB `{x1,y1,x2,y2}` on-page box for UI highlight/crop. |
| `raw_text` | Flattened plain-text rendering. Fallback text for embedding/search. |
| `markdown_text` | Markdown-table rendering. Shown inline in synthesis answers so the LLM sees real structure. |
| `json_data` | `{"headers":[...],"rows":[[...]]}` — machine-readable form; what exact-compute reads for sums/averages/counts. |
| `csv_data` | CSV rendering (via `_to_csv()`). Export convenience. |
| `row_count` / `col_count` | Grid dimensions. `row_count` drives the small/big split (`> 25` → children). |
| `has_numeric_data` | Bool — any cell parses as a number (currency symbols ₹/$/€/£ stripped first). |
| `has_currency` | Bool — regex currency symbol/code ($, €, £, ₹, USD, EUR, INR). Drives `table_filters.currency`. |
| `has_percentages` | Bool — regex `%` values. |
| `detected_units` | Text array of unit hints (millions, %, per share). Prevents magnitude misreads in synthesis. |
| `context_before` / `context_after` | Text surrounding the table in the source. Context the raw grid lacks. |
| `table_summary` | Short NL summary (period, category, headline). Feeds the parent-summary embedding. |
| `embedding` | `vector(1024)` — BGE embedding of the **parent summary**. Parent-level ANN target / fallback. |
| `fiscal_year` / `reporting_period` / `currency` / `table_category` | Enrichment facets (`table_category ∈ balance_sheet\|income_statement\|cash_flow\|kpi\|comparison\|other`). Query-time filters. |
| `table_metadata` | JSONB catch-all: continuation pages merged, merged/spanned-cell map, other structural metadata. |
| `created_at` | Insert timestamp. |
| `image_storage_path` | Supabase path to the crop image, or NULL. Minted into signed URLs for citations. |
| `source_image_id` | FK → `image_store.id`; crop-image asset this row came from. Nullable (pure-grid extractions). |
| `extraction_method` | `pdf_grid` \| `image_vlm` \| `image_ocr` \| `vlm_gapfilled`. |
| `extraction_quality` | Coarse bucket `high\|medium\|low` (NULL when no confidence available). |
| `source_confidence` | Continuous 0.0–1.0 confidence when available. |
| `provenance` | JSONB audit trail (passes run, gap-fill, model versions). Additive. |
| `structured_content` | Best textual representation (VLM transcription, or markdown/raw_text fallback). **Populated only for small tables (`row_count ≤ 25`); NULL for big tables** — those carry per-window `structured_content` in `table_chunk_store`. |
| `structured_content_embedding` | `vector(1024)` — embedding of `structured_content`. Preferred over `embedding` in the parent ANN (`COALESCE(structured_content_embedding, embedding)`). Also NULL for big tables. |

## Column reference: `table_chunk_store`  (big tables only)

| Column | Description |
|---|---|
| `id` | Primary key (UUID) for this row-window chunk. |
| `document_id` | FK → `document_registry.id`. Denormalized for filtering without a join. |
| `table_id` | FK → `table_store.id`. Parent logical table; cascades on delete. |
| `table_index` | Mirrors the parent `table_store.table_index`. |
| `chunk_index` | 0-based window position within the table (`0..ceil(rows/25)-1`). |
| `row_start` / `row_end` | 0-based, inclusive row range this window covers (25 rows/window). |
| `serialized_text` | The window's rows as `"Col1: val1; Col2: val2\n..."` (header repeated per row). Base embedding input. |
| `page_number` | Page this window's content appears on (multi-page tables cite different pages per window). |
| `embedding` | `vector(1024)` — BGE embedding of `serialized_text`. The base ANN target and the fallback in `COALESCE(structured_content_embedding, embedding)`; always populated. |
| `chunk_metadata` | JSONB flags: `oversized`, `coarsened`, `page_start`/`page_end` for continuation windows. |
| `created_at` | Insert timestamp. |
| `structured_content` | *(migration 018)* Pretty-printed JSON slice `{"title","headers","rows"}` of this window's ≤25 canonical rows (`indent=2`, ₹/€/£ literal). Surfaced at query time via `COALESCE(structured_content, serialized_text)`. |
| `structured_content_embedding` | *(migration 018)* `vector(1024)` — BGE embedding of this window's `structured_content` slice. Primary big-table ANN target: `COALESCE(structured_content_embedding, embedding)`. HNSW `idx_table_chunk_store_sc_embedding`. |
---
