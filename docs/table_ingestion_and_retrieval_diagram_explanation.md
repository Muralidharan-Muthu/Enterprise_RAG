# Table Ingestion & Retrieval — Step-by-Step Explanation

This document explains every diagram in
[`table_ingestion_and_retrieval_diagram.md`](./table_ingestion_and_retrieval_diagram.md),
one step at a time, and follows **one running example** all the way through so you
can see the data actually change at each stage.

---

## The running example

We upload a single PDF: **`stockmarket.pdf`** (1 page) containing one table of NSE
stocks. To make the interesting path visible, assume the table has **60 data rows**
(more than the `TABLE_CHUNK_MAX_ROWS = 25` threshold, so it counts as a **big table**).

The table looks like this (first + relevant rows shown):

| S.No | Company Name              | NSE Symbol | Sector | Price (INR) | Change % |
|-----:|---------------------------|------------|--------|-------------|----------|
| 1    | Reliance Industries       | RELIANCE   | Energy | ₹2,910.40   | +0.82%   |
| …    | …                         | …          | …      | …           | …        |
| 126  | Steel Authority of India  | SAIL       | Metals | ₹8,615.17   | -1.96%   |
| …    | …                         | …          | …      | …           | …        |

Later a user asks: **"what is the nse symbol of the company steel authority of india"**.
The correct answer is **SAIL**, and it lives only in that table row.

Two key constants used everywhere below:

- `TABLE_CHUNK_MAX_ROWS = 25` — the parent/child split threshold.
- `TABLE_MAX_WINDOWS_PER_QUERY_RESULT = 2` — max windows kept per table at query time.

---

# 1. Ingestion — high level (staged two-task chain)

**What the diagram shows:** an upload becomes two chained Celery tasks —
`parse_document_task` (parse + stage) then `chunk_embed_store_task` (everything
else) — and the table is stored differently depending on its size.

### Step-by-step

| Node | Step | What happens with our example |
|------|------|-------------------------------|
| **A** | User uploads document | `stockmarket.pdf` is uploaded via the UI. |
| **B** | `ingestion.py` registers the doc | A row is inserted into `document_registry` (status `pending`) and `ingestion_jobs` (progress tracking). A Celery **chain** is dispatched. |
| **P** | `parse_document_task` starts | Runs in the `parse` queue. No GPU model needed, so it can run highly concurrently. |
| **P1** | Parse with Docling | Docling OCRs the page and extracts the table structure (headers + 60 rows). Multi-page continuation tables would be merged here (ours is 1 page, so no merge). |
| **P2** | Stage `ParsedDocument` | The parsed result is serialized to Supabase Storage (`staging/<doc_id>/parsed.json`), a `parse_staging` row is written, and `document_registry.status = parsed`. |
| **Q** | `chunk_embed_store_task` starts | Runs in the `embed` queue (rate-limited, concurrency=1, because it uses the 1.3 GB BGE model). |
| **Q1** | Load staged `ParsedDocument` | Downloads `parsed.json` back and rebuilds the object. |
| **Q2** | Route | Gemma 4 classifies the document type (here → `financial`). |
| **Q3** | Chunk | Text is chunked normally; the table is split into **row-window** chunks of 25 rows each. |
| **Q4** | Embed | BGE embeds text chunks, table **parent summaries**, and **big-table windows**. Small tables' windows are skipped (they fit whole in the parent). |
| **Q5** | Reconstruct + enrich | A VLM re-transcribes the table crop; enrichment adds `fiscal_year`, `currency`, `table_category`, etc. |
| **R** | Decision: `row_count > 25?` | Our table has 60 rows → **yes (big)**. |
| **R1** | Small-table branch (NOT taken) | Would store the whole table in `table_store` with `structured_content` + embedding, and **no** `table_chunk_store` rows. |
| **R2** | Big-table branch (taken) | `table_store` parent row has `structured_content = NULL`; the actual searchable content goes into `table_chunk_store` as **one row per 25-row window**, each with its own structured_content + embedding. |
| **Z** | Graph stage + cleanup | Graph relationships are built (best-effort), staging is cleaned up, `document_registry.status = completed`. |

### Example: how our 60-row table splits

`ceil(60 / 25) = 3` windows:

- Window 0 → rows 1–25
- Window 1 → rows 26–50
- Window 2 → rows 51–60 (the **SAIL row, S.No 126, is here** in this example)

So after ingestion:

- `table_store`: **1 parent row** (summary embedding, `structured_content = NULL`).
- `table_chunk_store`: **3 child rows** (windows 0/1/2), each with `serialized_text` + `embedding` (+ structured_content slice + its embedding).

---

# 2. Ingestion — low level

**What the diagram shows:** the exact functions inside each task, including parser
fallbacks and the table-storage branch.

### `PARSE` subgraph (`parse_document_task`)

| Node | Step | Example |
|------|------|---------|
| **A → A1** | `ingestion.py` validates, uploads to Storage, creates registry + jobs rows | `stockmarket.pdf` bytes stored in the bucket. |
| **A1 → A2** | `dispatch_ingestion` builds the chain | `parse_document_task → chunk_embed_store_task`. |
| **B** | Is it a PDF? | Yes. |
| **B1** | `parse_document_chunked` (page-chunked Docling) | Primary parser. |
| **B1 → B2 → B3** | Fallback ladder | If chunked parsing fails → `_parse_with_docling` (whole doc) → `_parse_fallback` (PyMuPDF). Robustness only; our file succeeds at B1. |
| **B4** | Non-PDF path (NOT taken) | Would be a single Docling pass. |
| **C** | `_extract_tables` | Finds the 1 stock table. |
| **C1** | `_detect_merged_cells` + `_parse_table_data` | Reads headers `[S.No, Company Name, NSE Symbol, Sector, Price (INR), Change %]` and 60 rows. |
| **C2** | `_merge_continued_tables` | Merges tables that continue across pages. Ours is single-page → no-op. |
| **C3** | Build `ExtractedTable` | Produces `raw_text`, `markdown_text`, a crop image, and a caption. |
| **C4** | `save_parsed` → staging | Writes `parsed.json`, sets status `parsed`. |

### `EMBED` subgraph (`chunk_embed_store_task`)

| Node | Step | Example |
|------|------|---------|
| **D** | `load_parsed` | Rebuilds the `ParsedDocument`. |
| **E** | `classify_document` (Gemma 4) | → `document_type = financial`. |
| **F** | `chunk_document` | Text chunks (page intro paragraph, etc.). |
| **G** | `chunk_tables` + `build_row_windows` | Splits the 60-row table into windows of 25 rows → 3 windows. |
| **G1** | Drop small-table windows | Any table with `row_count ≤ 25` has its windows dropped here (kept whole in the parent). Our table is big → windows kept. |
| **H** | `embed_passages` | Embeds text chunks + parent summary + the 3 big-table windows. |
| **I** | `reconstruct_tables_with_vlm` + `enrich_table` | VLM transcription + metadata enrichment. |
| **M** | Decision `row_count > 25?` | Yes (60). |
| **M1** | Small path (NOT taken) | `table_store` row with `structured_content` + `sc_embedding` **set**. |
| **M2** | Big path (taken) | `table_store` row with `structured_content` + `sc_embedding` **NULL**. |
| **N** | `build_window_structured_content` per window | For each of the 3 windows, build a JSON slice of just that window's rows; batch-embed the slices. |
| **N1** | `insert_table_chunks` | Insert 3 rows into `table_chunk_store`: `serialized_text` + `embedding` + `structured_content` slice + `sc_embedding`. Rows are arity-padded to 12 columns if the caller passes fewer. |
| **Z** | Status `completed`. | |

### Example data at N1 — one `table_chunk_store` row (Window 2, containing SAIL)

```
document_id            = <doc_id>
table_id               = <table_store parent id>   (FK, ON DELETE CASCADE)
table_index            = 0
chunk_index            = 2
row_start              = 51
row_end                = 60
serialized_text        = "S.No: 126; Company Name: Steel Authority of India;
                          NSE Symbol: SAIL; Sector: Metals; Price (INR): ₹8,615.17;
                          Change %: -1.96% | ... (other rows in this window)"
embedding              = <1024-dim BGE vector of serialized_text>
structured_content     = { "title": null,
                           "headers": ["S.No","Company Name","NSE Symbol",...],
                           "rows": [ ["126","Steel Authority of India","SAIL",
                                      "Metals","₹8,615.17","-1.96%"], ... ] }
structured_content_embedding = <1024-dim BGE vector of the JSON slice>
```

This is exactly the row shown in your Supabase screenshot.

---

# 3. Retrieval — high level

**What the diagram shows:** query → embed → pick stores → search tables → rank →
synthesize → return.

| Node | Step | Example |
|------|------|---------|
| **A** | User asks a question | "what is the nse symbol of the company steel authority of india". |
| **B** | Embed the query | BGE with the instruction prefix `"Represent this question for searching relevant passages: …"` → 1024-dim vector. |
| **C** | Select relevant stores | Choose among `vector / clause / research / table`. **The `table` store must be included** or the SAIL row is invisible. |
| **D** | Search table stores | Vector ANN over `table_store` + `table_chunk_store`. |
| **E** | Pool + rerank | Combine candidates from all active stores, cap per store, rerank. |
| **F** | Synthesize | Gemma 4 writes the answer from the top-ranked chunks. |
| **G** | Return | Answer + citations to the UI. |

> **Note (bug fixed this project):** if store selection wrongly narrows to
> `["vector"]` only, the table store is skipped and the answer becomes
> "not listed". `_select_stores` now always keeps `table` on when an intent hint
> narrows the set.

---

# 4. Retrieval — low level

**What the diagram shows:** the full request path from the Next.js proxy through
store queries, ranking, and synthesis.

| Node | Step | Example |
|------|------|---------|
| **A → A1** | UI submits query → Next.js proxy → `POST /api/v1/query` | |
| **B** | `query.py` parses `QueryRequest` | `top_k`, `document_types`, `table_filters`, `use_reranker`. |
| **C** | `embed_query` | Prefix + BGE → 1024-dim vector. |
| **D** | Are `document_types` given? | If the user didn't specify → **D1** `classify_intent → _select_stores`; if they did → **D2** use them directly. For our query, intent decides. |
| **E** | Concurrent per-store queries | Each active store runs on its own DB connection in parallel. |
| **F** | `_query_table_store` | The table-specific search. |
| **F1** | Child ANN | Order by `COALESCE(structured_content_embedding, embedding) <=> query`; text returned is `COALESCE(structured_content, serialized_text)`; filtered by `status=completed`, type/doc/table_filters. |
| **F2** | Joins | `table_chunk_store → table_store → document_registry`. |
| **F3** | Dedup per `table_id` | Keep the best `TABLE_MAX_WINDOWS_PER_QUERY_RESULT` (=2) windows per table so one wide table can't flood the pool. |
| **F5** | `_query_table_store_parent_only` | Parent ANN over `COALESCE(sc_embedding, embedding)` — covers **small tables** and **big tables with no child hit**. |
| **G** | Candidate table chunks | big-table windows + small-table parents. |
| **H** | `balanced_pool` | Cap per store at `RERANK_PER_STORE_CAP = 8`. |
| **I** | `use_reranker?` | If yes → **I1** BGE-reranker-large on top 40, score `0.3*sigmoid + 0.7*minmax`; if no → **I2** Reciprocal Rank Fusion. |
| **J** | Final top_k chunks | |
| **K** | Below threshold / zero chunks? | If yes → **K1** no-results / off-topic reply. |
| **L** | `_build_context` | Numbered blocks; table chunks include `table_markdown`. |
| **Mx** | `synthesize` via Gemma 4 | Writes the answer; on error → **M1** fallback concatenates chunk texts. |
| **Nx** | `_citation_from_chunk` | filename, page, `table_markdown`, bbox, signed URL. |
| **O** | Return | answer, confidence, `sources_used`. |

### Example trace for our query

1. **Embed** "…nse symbol…steel authority of india" → query vector `q`.
2. **F1 child ANN**: window 2's `structured_content_embedding` (the JSON slice
   containing `"Steel Authority of India","SAIL"`) is closest to `q`.
3. **F3 dedup**: keep top 2 windows for this table (window 2 survives).
4. **H/I rerank**: window 2 ranks first; `is_child_match=True` gives it a small
   +0.05 nudge so its bare numeric rows aren't beaten on wording alone.
5. **L build_context**: block includes the row `126 | Steel Authority of India |
   SAIL | Metals | ₹8,615.17 | -1.96%`.
6. **Mx synthesize**: Gemma reads it and answers **"SAIL"** with a citation to
   `stockmarket.pdf`.

---

# 5. `table_store` / `table_chunk_store` internal architecture — high level

**What the diagram shows:** the parent/child split is driven entirely by table size
(`TABLE_CHUNK_MAX_ROWS = 25`).

### Small table (≤ 25 rows) — **not** our example

| Node | Meaning |
|------|---------|
| **S1** | One `table_store` row holds the whole table. |
| **S1a** | `embedding` = parent summary vector. |
| **S1b** | `structured_content` = VLM/markdown, and `structured_content_embedding` is **SET**. |
| **S1c** | **No** `table_chunk_store` rows. |
| **QP** | Query path = parent-only ANN over `COALESCE(sc_embedding, embedding)`. |

### Big table (> 25 rows) — **our example (60 rows)**

| Node | Meaning |
|------|---------|
| **B1** | One `table_store` parent row. |
| **B1a** | `embedding` = parent summary vector. |
| **B1b** | `structured_content = NULL`, `structured_content_embedding = NULL`. |
| **C1** | `table_chunk_store` gets `N = ceil(rows/25)` windows → **3** for us. |
| **C1a** | Each window: 25 rows → `serialized_text` + `embedding`. |
| **C1b** | Each window: `structured_content` JSON slice + `structured_content_embedding`. |
| **QC** | Query path = child ANN over `COALESCE(sc_embedding, embedding)`. |

**Why split at all?** A 60-row table embedded as one vector blurs every row
together — a query about one company barely moves the similarity. Splitting into
25-row windows means the window that actually contains "Steel Authority of India"
has a focused embedding that matches the query strongly.

---

# 6. `table_store` / `table_chunk_store` internal architecture — low level

**What the diagram shows:** the actual columns of both tables and the FK + query edges.

### `table_store` — one row per logical table

| Group | Columns | Example value |
|-------|---------|---------------|
| **TSk** | `id` PK, `document_id` FK, `table_index`, `row_count`, `col_count`, `page_number`, `bbox` | `row_count=60`, `col_count=6`, `page_number=1` |
| **TSt** | `raw_text`, `markdown_text`, `json_data`, `csv_data`, `table_summary`, enrichment fields | `table_category=stock_list`, `currency=INR` |
| **TSe** | `embedding vector(1024)` = parent summary | one vector for the whole table |
| **TSs** | `structured_content` + `structured_content_embedding` | **NULL** here (big table); SET only when `row_count ≤ 25` |

### `table_chunk_store` — big tables only, N windows

| Group | Columns | Example (Window 2) |
|-------|---------|--------------------|
| **TCk** | `id` PK, `document_id` FK, `table_id` FK → `table_store.id`, `table_index` | `table_index=0` |
| **TCw** | `chunk_index`, `row_start`, `row_end` (inclusive 25-row window) | `chunk_index=2`, `row_start=51`, `row_end=60` |
| **TCt** | `serialized_text` (`Col: val; …` per row) | `"… NSE Symbol: SAIL …"` |
| **TCe** | `embedding vector(1024)` = serialized_text | window vector |
| **TCs** | `structured_content` JSON slice (indent=2) + `structured_content_embedding vector(1024)`, HNSW index `idx_table_chunk_store_sc_embedding` | the JSON with the SAIL row |

### Edges

- `TSk → TCk` : `table_id` FK, **only** for tables with `row_count > 25`,
  `ON DELETE CASCADE` (deleting the parent removes all its windows).
- `QRY → TCe` : **child ANN** using `COALESCE(sc_embedding, embedding)`.
- `QRY → TSe` : **parent-only ANN** using `COALESCE(sc_embedding, embedding)`.
- Returned text is `COALESCE(structured_content, serialized_text)` for child hits,
  and the small-table text for parent hits — both flow to **synthesis**.

---

## One-paragraph summary

Ingestion parses the PDF, and for each table decides by size: a **small table**
(≤25 rows) is stored whole in `table_store` (with a searchable `structured_content`
embedding and no children); a **big table** (>25 rows) keeps a summary-only parent in
`table_store` and stores its real content as 25-row **windows** in
`table_chunk_store`, each independently embedded. Retrieval embeds the query, makes
sure the **table store is searched**, runs a child ANN over the windows plus a
parent ANN for small/uncovered tables, keeps the best 2 windows per table, reranks,
and hands the winning window's rows to Gemma 4, which reads the exact row
(`Steel Authority of India → SAIL`) and answers with a citation.
