# Table Pipeline Architecture

Status: current as of commit `f36454d` (2026-07-03). Schema: `multi_store_rag_working`. All behavior described here has been verified against a live Docker stack (API + Celery worker + Supabase/pgvector), not just unit-tested — see [Validation Results](#validation-results) for exact evidence.

## 1. Overview

Multi-Store RAG Chatbot's table pipeline turns tables inside a PDF into queryable, retrievable, exactly-computable data. A table can be:

- extracted whole from a single page (the common case),
- extracted as fragments split across multiple pages by Docling and then **merged back into one logical table** (continuation),
- extracted with **rowspan/colspan/multi-row headers**, captured as a structural span map alongside the flattened searchable text,
- queried two ways at once: **semantically** (embeddings, for "what does this say") and **exactly** (a structured engine, for "what is the sum/average/count").

```
PDF
 │
 ▼
Docling (page-chunked or whole-doc)  ──► raw table fragments (1 per physical page-appearance)
 │
 ▼
_detect_merged_cells() + _parse_table_data()   [per fragment: span map, multi-row header combine]
 │
 ▼
_merge_continued_tables()                       [cross-fragment: continuation detection + merge]
 │
 ▼
ExtractedTable (final, 1 per logical table)  ──► image crop rendered + registered in image_store
 │
 ▼
VLM reconciliation (table_reconstruction.py)    [Docling vs VLM, numeric-faithfulness gate]
 │
 ▼
Enrichment (fiscal_year, currency, category, summary — Slice 3, unchanged this round)
 │
 ▼
storage_service._store_tables()  ──►  table_store (1 row per logical table)
 │
 ▼
table_chunker.chunk_tables()  ──►  table_chunk_store (row-window children, page_start/page_end per window)
 │
 ▼
Lineage/completeness gates (before "completed")  ──►  document_registry.status
 │
 ▼
Query time: retriever_service (semantic, multi-window) + table_query_engine (exact aggregate) ──► synthesis
```

## 2. Ingestion pipeline, stage by stage

### 2.1 Parsing (Docling)

Two entry points, both PDF: `parse_document_chunked()` ([document_parser.py:696](../backend/app/services/document_parser.py#L696)) and `_parse_with_docling()` ([document_parser.py:641](../backend/app/services/document_parser.py#L641)). `parse_document_chunked` is tried first; `_parse_with_docling` is the whole-document fallback if chunked parsing throws.

`_adaptive_chunk_size()` ([document_parser.py:668](../backend/app/services/document_parser.py#L668)) decides pages-per-Docling-call: **1 page per chunk for any document ≤25 pages**, growing to a cap of 8 for very large documents. This means the overwhelming majority of real documents are Docling-converted one page at a time.

**Confirmed empirically this session**: Docling itself splits a table into separate table objects at page boundaries — this is true even with zero chunking (a single whole-document `convert()` call on a 3-page, 70-row synthetic table still returned 3 separate table objects, one per page, with continuous row numbering proving they were one logical table). The chunk-size-1 behavior above makes this the default case for small documents too, not just an edge case for huge PDFs.

### 2.2 Per-fragment processing: span detection + multi-row headers

For every raw Docling table object, `_extract_tables()` ([document_parser.py:473](../backend/app/services/document_parser.py#L473)) runs, in order:

1. `_detect_merged_cells(table)` ([document_parser.py:929](../backend/app/services/document_parser.py#L929)) — reads Docling's raw `table.data.table_cells` list directly (**not** `table.data.grid`, which duplicates a spanned cell's text into every position it covers and therefore always reports uniform row widths — this was the key research finding that unlocked genuine span detection). Returns:
   ```python
   {
     "has_merged_cells": bool,
     "max_row_span": int,
     "max_col_span": int,
     "spanned_cell_count": int,
     "header_row_count": int,        # contiguous leading header rows, via column_header flag
     "cells": [                       # only cells with row_span>1 or col_span>1
       {"row_start": 0, "row_end": 1, "col_start": 2, "col_end": 4,
        "text": "Q1 2024", "is_header": true},
       ...
     ],
   }
   ```
   Docling attribute names verified against `docling-core==2.74.0`: `row_span`, `col_span`, `start_row_offset_idx`/`end_row_offset_idx` (0-based, **exclusive** — confirmed via `TableData.grid`'s own `range()` construction), same for columns, `column_header: bool` (the real header-row signal — `row_header`/`row_section` also exist but aren't used here). **Format gap, documented not fixed**: `column_header` is only set by TableFormer (native PDF) and the HTML/XML backends — DOCX/PPTX/XLSX/MD backends never set it, so `header_row_count` is always 0 for those formats.

2. `_parse_table_data(table, header_row_count=...)` ([document_parser.py:872](../backend/app/services/document_parser.py#L872)) — builds `headers`/`rows` from the dense grid. When `header_row_count <= 1` (the common case), behavior is byte-identical to before this round. When `> 1`, it combines the leading header rows per column into one string (e.g. `"Q1 2024 - Budget"`) and excludes those rows from `rows`.

   **Correctness bug fixed this round**: previously, only grid row 0 was ever treated as the header. A genuine 2-row header (e.g. `"Q1 2024"` spanning `"Budget"`/`"Actual"` sub-columns) had its second header row silently misclassified as a *data* row — corrupting `row_count` and leaking header fragments into searchable text. Now detected via Docling's own `column_header` flags, never via row-content heuristics.

3. `table_metadata["merged_cells"] = span_info` is attached to the `ExtractedTable`, but **only** when there's something to report (`has_merged_cells` or a multi-row header) — an ordinary single-header, no-span table gets `table_metadata = {}`, unchanged from before this round.

### 2.3 Cross-fragment: continuation merging

`_merge_continued_tables(tables)` ([document_parser.py:524](../backend/app/services/document_parser.py#L524)) runs once per document, after all fragments (across all page-chunks) have been extracted, right before `ParsedDocument` is constructed — called from both `parse_document_chunked` and `_parse_with_docling`.

**Merge rule** (exact): tables sorted by `(page_number, table_index)`; adjacent A→B merge iff:
- `B.page_number == A.page_number + 1` (strictly consecutive pages), **and**
- `A.headers == B.headers` (exact list equality, non-empty), **and**
- every row in both A and B has length equal to `len(headers)`.

Merging chains transitively (A→B→C collapses into one group) and is greedy left-to-right.

**Documented, accepted false-positive risk**: two genuinely unrelated tables with identical headers on strictly consecutive pages will be merged. There is no additional signal (e.g. semantic dissimilarity) checked — this is a deliberate simplicity tradeoff, not an oversight.

**What a merge produces**: one `ExtractedTable` with:
- `headers` = the shared header list,
- `rows` = concatenation of all fragments' rows in page order,
- `row_page_numbers: list[int]` — new field on `ExtractedTable`, one page number per merged row (`None` for an ordinary unmerged table — full backward compatibility),
- `page_number` = the **first** fragment's page (kept as the representative single-page value for any code still reading the singular field),
- `bbox` / `image_png_bytes` = the **first** fragment's (representative — see §3 for why this is sufficient),
- `raw_text`/`markdown_text` rebuilt from the combined headers+rows via the existing serializers (not hand-rolled),
- `table_index` renumbered contiguously across the final list,
- `table_metadata["continuation"] = {"is_continuation": true, "fragment_count": N, "fragment_pages": [...], "fragment_table_indices": [...]}`.

**Span-map + continuation interaction** (verified during Phase-1 integration): when a merged table's fragments each independently produced their own `table_metadata["merged_cells"]`, the merged table keeps **only the first fragment's** span data. Each fragment's row/col offsets are local to its own physical-page grid — concatenating them across fragments with independent 0-based indices would produce colliding, meaningless coordinates. This was confirmed as the already-correct behavior, not something requiring a fix.

**Ordering guarantee**: `_detect_merged_cells`/`_parse_table_data` (multi-row header resolution) always run per-fragment *before* `_merge_continued_tables` runs on the full list — so the continuation-merge header comparison always compares fully-resolved header strings, never raw un-combined ones.

### 2.4 Crop images and lineage (source_image_id)

Because merging happens upstream in `document_parser.py`, `ingestion_orchestrator.py` and `storage_service.py` never see the pre-merge fragments at all — they see exactly one `ExtractedTable` per logical table, with one `image_png_bytes` (the first fragment's). Crop-image registration (`store_table_crop_images()`) therefore naturally produces **exactly one** `image_store` row per logical table, keyed 1:1 via the existing FK:

```
table_store.source_image_id  →  image_store.id   (asset_role = 'table_crop')
```

No code change was needed here — this was verified, not assumed, by reading the actual crop-registration loop.

### 2.5 VLM reconciliation + numeric-faithfulness gate

`table_reconstruction.py` re-runs the multimodal VLM on each table's crop image and decides whether to trust Docling's native grid or the VLM's re-transcription. Three-tier faithfulness gate (`faithfulness_ok()`):

| Docling grid | Merged cells? | Tolerance for VLM-introduced unseen numbers |
|---|---|---|
| Well-formed (uniform column count) | No | 0% — any unseen number rejects the VLM |
| Well-formed | Yes | 8% (`_MERGED_CELL_TOLERANCE`) — added prior round, specifically because a merged-cell table's dense grid looks "well-formed" even though its true structure is more complex |
| Not well-formed (ragged) | either | 20% (`_FAITHFULNESS_TOLERANCE`) — no reliable native structure to check against |

`has_merged_cells` for this decision comes from `table_metadata["merged_cells"]["has_merged_cells"]`, fail-open to `False` if absent.

### 2.6 Storage

`storage_service._store_tables()` inserts one row per (post-merge) `ExtractedTable` into `table_store`, writing `table_metadata` verbatim — which now carries `merged_cells` (span map) and/or `continuation` and `row_page_numbers` as additive JSONB, no schema migration required for any of Phase 1/2's feature data.

`table_chunker.chunk_tables()` builds row-window children exactly as before, but now also slices the parent's `row_page_numbers` per window and records `chunk_metadata["page_start"]` / `["page_end"]` — the mechanism that lets a citation later say "this excerpt spans pages 1–2."

### 2.7 Completion gates (before `status = 'completed'`)

Two independent, additive gates run in `ingest_document()` right before `doc_repo.update_status(document_id, "completed", ...)` ([ingestion_orchestrator.py:652](../backend/app/services/ingestion_orchestrator.py#L652)):

1. **`_find_table_lineage_gap()`** ([ingestion_orchestrator.py:761](../backend/app/services/ingestion_orchestrator.py#L761)) — added when the crop-registration silent-failure bug was fixed: if any table that had an image-crop candidate ended up with no `source_image_id`, the document does **not** report `completed`.
2. **`_find_table_count_mismatch()`** ([ingestion_orchestrator.py:778](../backend/app/services/ingestion_orchestrator.py#L778)) — added this round after a live validation run caught a stray worker silently producing structurally broken (unmerged) output while still reporting success. Compares `len(parsed_doc.tables)` (the parser's final, post-merge count) against the actual number of `table_store` rows inserted. Any mismatch → not `completed`.

Either gate firing routes the document to `status = "completed_with_errors"` with `error_stage` and a specific `error_message`. The count-mismatch message deliberately includes `worker_id`/`code_version` (see §4) so a future divergence is diagnosable from the database alone.

## 3. Worker identity & diagnosability

`backend/app/core/worker_identity.py` computes two module-level constants once at import time:

- `CODE_VERSION` — `git rev-parse --short HEAD` against the repo root, 2s timeout, falls back to `"unknown"` on any failure (missing git, non-repo, timeout). **Confirmed live**: the Docker worker image has no `git` binary installed, so `CODE_VERSION` is `"unknown"` in this deployment today — a correct, non-fatal degradation, not a bug.
- `WORKER_ID` — `f"{hostname}-{pid}-{process_start_unix_ts}"`, distinguishing any two worker processes (native venv vs container, or across container restarts).

`_stamp_worker_identity()` ([ingestion_orchestrator.py:37](../backend/app/services/ingestion_orchestrator.py#L37)) writes both into `ingestion_jobs.worker_id` / `.code_version` (migration `015_ingestion_worker_stamp.sql`) at the very first status update of a job, wrapped in try/except so a pre-migration environment never fails ingestion because of this.

**Why this exists**: a real incident during this project's validation. A stray native Celery worker running stale code (from before the continuation-merge fix landed) was consuming the same Redis queue as the Docker worker container. Celery task delivery to either consumer is nondeterministic per-task — so identical re-ingestions of the same document silently alternated between correct (merged) and broken (unmerged, 3 separate rows) results, both reporting `status = completed`, with zero distinguishing signal. The count-mismatch gate (§2.7) plus this stamping together close that gap: a mismatch is now caught before `completed` is ever reported, and when it is caught, the error message names exactly which worker/version produced it.

## 4. Database schema (current)

All additive — no breaking changes to any existing column across this entire effort.

```
document_registry
 ├─ status: 'completed' | 'completed_with_errors' | ... (existing enum, no new values needed)
 ├─ error_stage, error_message (existing, now also populated by the table-count gate)
 └─ ... (unchanged)

ingestion_jobs                                    [migration 015]
 ├─ worker_id       TEXT   (new, nullable)
 └─ code_version    TEXT   (new, nullable)

image_store
 ├─ asset_role       ('table_crop' | 'figure')     [migration 014]
 └─ ... (unchanged this round — 1 crop image per logical/merged table, not per fragment)

table_store
 ├─ source_image_id  UUID → image_store.id         [migration 014, 1:1 unique]
 ├─ extraction_method, extraction_quality,
 │  source_confidence, provenance JSONB            [migration 014]
 └─ table_metadata JSONB now additionally carries:
     ├─ merged_cells: {has_merged_cells, max_row_span, max_col_span,
     │                 spanned_cell_count, header_row_count, cells: [...]}
     ├─ continuation: {is_continuation, fragment_count,
     │                 fragment_pages, fragment_table_indices}
     └─ row_page_numbers: [int, ...]                 (one entry per stored row)

table_chunk_store
 ├─ table_id → table_store.id
 ├─ row_start, row_end                             (row-window bounds, on the MERGED table's rows)
 └─ chunk_metadata JSONB now additionally carries:
     └─ {page_start: int, page_end: int}             (per-window page range)
```

No migration was needed for any table-shape/span/continuation data — `table_metadata` and `chunk_metadata` are both pre-existing additive JSONB columns, used exactly as designed. Migration 015 was the one genuinely new, first-class operational field (worker identity belongs to a job run, not to table content, so it got its own columns rather than being buried in JSONB).

## 5. Lineage flow (end to end)

```
PDF page 1 ─┐
PDF page 2 ─┼─► Docling (3 raw table fragments, one per page)
PDF page 3 ─┘
                │
                ▼
       _merge_continued_tables()
                │
                ▼
     ONE ExtractedTable (table_index=0, row_page_numbers=[1×28, 2×31, 3×11])
                │
        ┌───────┴────────┐
        ▼                ▼
  crop image           table_store row
  (1st fragment)        (id=X, table_index=0, row_count=70)
        │                │
        └──────┬─────────┘
               ▼
     image_store.id ←── table_store.source_image_id (FK, 1:1, asset_role='table_crop')
               
     table_store row (id=X)
               │
               ▼
     table_chunk_store windows (table_id=X, each with page_start/page_end)
               │
               ▼
     embeddings (BGE, per window, non-null)
               │
               ▼
     retriever query → RetrievedChunk (page_number, page_number_end)
               │
               ▼
     CitationItem in the API response (page_number, page_number_end)
```

Verified live end-to-end (see §7): every hop above was independently queried/observed with real data, not asserted.

## 6. Retrieval flow

Two parallel, independently-gated paths per query — never mutually exclusive:

### 6.1 Semantic (existing, extended this round)

`retriever_service._query_table_store()` ([retriever_service.py:511](../backend/app/services/retriever_service.py#L511)): child-window ANN search over `table_chunk_store`, deduped **per table_id to the top-K closest windows** (`TABLE_MAX_WINDOWS_PER_QUERY_RESULT`, default 2 — a prior-round fix; confirmed this round to require no change for continuation-merged tables, since the cap operates generically on `table_id` regardless of how many windows that table produced upstream).

New this round: `_resolve_chunk_page_range()` ([retriever_service.py:473](../backend/app/services/retriever_service.py#L473)) reads `chunk_metadata.page_start/page_end` defensively (dict, JSON-string, or missing — never raises) and populates `RetrievedChunk.page_number_end`, defaulting `None` for any chunk without a page range (i.e., every ordinary single-page table, unaffected).

### 6.2 Structured (new this round)

`table_query_engine.try_structured_query()` ([table_query_engine.py:397](../backend/app/services/table_query_engine.py#L397)) — a deterministic, non-LLM, rule-based path:

1. Intent detection (regex/keyword): SUM/AVG/COUNT/MIN/MAX, or exact row-filter + column lookup.
2. Column resolution (`_extract_target_column()`, [table_query_engine.py:123](../backend/app/services/table_query_engine.py#L123)): fuzzy-matches the query's target column name against actual `table_store.json_data` headers for candidate tables.
3. Reads `table_store.json_data` directly (the same JSON already stored per logical table — a SUM over a continuation-merged table therefore sums across **all its original pages' rows for free**, with no special-casing).
4. Numeric parsing reuses `table_reconstruction`'s existing currency/percentage/comma-stripping conventions.
5. Returns `None` when no aggregate/lookup intent is recognized — the existing semantic path is then completely unaffected, byte-for-byte.

Gated by `STRUCTURED_QUERY_ENABLED` (default `True`). Wired into `query.py` (`_try_structured_query()`, [query.py:161](../backend/app/api/routes/query.py#L161)) additively: when non-`None`, the computed fact is prefixed onto the synthesis prompt and returned as `QueryResponse.structured_result` ([query.py:158](../backend/app/api/routes/query.py#L158)); when `None`, `synthesis_query` is identical to the raw query.

**Known, documented gap (not blocking, follow-up filed)**: `_extract_target_column`'s verbatim-substring fast path can false-positive on plural/inflected phrasing — e.g. a query containing the word "months" substring-matches a `"Month"` column before the intended column (say, "budget") is considered. Exact/verbatim column-name phrasing is unaffected. Fix: require a word-boundary match before falling back to fuzzy matching.

**Known, documented gap**: `/query/stream` with `AGENTIC_RAG_ENABLED=True` (non-default) never reaches the structured-query call — the agentic branch returns its own `StreamingResponse` earlier. Does not affect default configuration.

## 7. Validation results

All of the below is real evidence gathered against the live Docker stack (API + Celery worker + Supabase), through the actual HTTP API and actual SQL — not internal function calls presented as proof.

| Feature | Evidence |
|---|---|
| Multi-page continuation | 70-row/3-page synthetic PDF → exactly 1 `table_store` row (not 3), `row_page_numbers` = `[1]×28 + [2]×31 + [3]×11` (exact), 2 `table_chunk_store` windows genuinely straddling the original page boundary |
| Per-cell span model | Synthetic 11×7 PDF with 4 genuine reportlab `SPAN` regions → Docling's TableFormer detected all 4 on the first attempt; `table_metadata.merged_cells.cells` matched exactly; faithfulness gate correctly selected the 8% middle tolerance tier (not strict, not loose) |
| Multi-row header fix | 2-row synthetic header → `headers` combined to `"Q1 2024 - Budget"` / `"Q1 2024 - Actual"`; data rows correctly excluded the second header row |
| Structured query engine | `SUM(Actual USD)` over the 70-row merged table → `1,062,740.0` via the live API, independently cross-checked against raw SQL over all 70 rows — exact match. Also correct on the original single-page regression document (`24,500` exact) |
| Retrieval citations | `page_number_end` correctly `null` on every citation from ordinary single-page tables (zero regression); correctly non-null and spanning the true page range on continuation-merged tables |
| Worker stamping | Live `ingestion_jobs` row showed `worker_id=2953886f2018-1-1783020256, code_version=unknown` — `unknown` independently confirmed correct (`docker compose exec worker which git` → not found) |
| Table-count sanity gate | **Actually triggered**, not just unit-tested: a temporary, fully-reversible one-line patch simulated a dropped table on write; the document correctly landed as `completed_with_errors` with an error message naming the exact mismatch (`parser produced 2 table(s) but 1 table_store row(s) were inserted`) and the responsible `worker_id`/`code_version`. The patch was reverted and confirmed byte-identical via MD5 hash; the next ingestion of the same document completed cleanly |
| Test suite | 667 passed, 2 skipped (both pre-existing, environment-gated: `test_parser_images.py`, `test_store_images.py` — need an external resource), 0 failed. Independently rerun by the reviewing process before every commit in this effort, not taken from any agent's self-report |
| Regression check | Original single-page finance-newsletter document re-queried after all changes: identical citation shape (`page_number_end: null`), correct SUM when asked, unaffected semantic answers |

## 8. Known limitations (final, as of this document)

These are accepted, documented tradeoffs — not oversights:

- **Typed per-cell span model is span-only, not a full reconstruction model**: `merged_cells.cells` gives geometry and text per spanned region, but nothing downstream currently reads it to reconstruct a visual table (e.g. for a UI). It exists for future consumption and for the faithfulness gate.
- **Continuation-merge false positives**: two unrelated tables with identical headers on strictly consecutive pages will merge. No semantic-dissimilarity check exists to guard against this.
- **PDF-only continuation**: `_parse_non_pdf` (DOCX/PPTX/XLSX/HTML/MD) never calls `_merge_continued_tables`.
- **`column_header` flag gap**: DOCX/PPTX/XLSX/MD Docling backends never set it, so multi-row-header combination only activates for PDF (TableFormer) and HTML/XML-sourced tables.
- **Structured query engine is single-table**: a SUM/AVG query matches and computes over one best-matching table, not a cross-table aggregate.
- **Column-matching substring false-positive** (§6.2) — tracked as a follow-up, not fixed.
- **Agentic-streaming + structured-query gap** (§6.2) — non-default configuration only.
- **No exact-value/aggregation JSONB structured path beyond what's described here** — the dead `TABLE_STRUCTURED_PATH_ENABLED` flag from a prior round was removed; this document's structured query engine is the actual, shipped implementation of that previously-unbuilt idea.
- **Synthesis-quality gaps observed, not architectural**: during live validation, Gemma's prose synthesis twice misused correctly-retrieved data (an undercounted category frequency, and a case of second-guessing its own correct `structured_result`). Retrieval/citation plumbing was independently confirmed correct in both cases via raw citation/chunk_text inspection — this is a downstream synthesis-prompt issue, not a defect in anything described in this document.
