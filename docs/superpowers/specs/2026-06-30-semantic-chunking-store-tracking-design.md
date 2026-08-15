# Semantic Chunking + Store-Tracking Design

Date: 2026-06-30
Branch: `feat/semantic-chunking-store-tracking`

## Problem

Three defects/gaps surfaced from a 5-page finance PDF ingestion:

1. **Chunking is fixed-size, not semantic.** `chunker.py:_chunk_semantic()` is sentence/section
   aware but still cuts at a fixed 512-token target with 64-token overlap. We want genuine
   semantic-boundary chunking (no overlap, variable size) and every `vector_store` column filled
   (`section_title`, `keywords`, `semantic_type` currently land NULL/empty for many chunks).
2. **`image_store.stored_in` lies.** `storage_service.py:317` hardcodes `stored_in="image_store"`
   for every row; it is only corrected later by `_update_image_stored_in()` — and only when the
   cross-store INSERT succeeds. There is no try/except (lines 445-456) and no shared transaction,
   so a chart with `detected_store="table_store"` can show `stored_in="image_store"` while the
   table copy was skipped (no content / no embedding) or threw. The column reflects intent, not
   reality.
3. **No origin tracking.** No store has a boolean telling whether a row originated from an image
   extraction. Needed for connectivity/audit ("is this row from image_store? yes/no").

## Approach (production-grade hybrid — approved)

### Part 1 — Semantic chunking

Rewrite the splitter inside `chunker.py`. **Keep** the existing logical-unit builder
(`_build_logical_units`, lines ~235-309) — section boundaries, list coalescing, image-block
isolation are correct and reused unchanged.

Replace the fixed-size splitter (lines ~343-457) with:

1. **Embedding breakpoint detection (BGE, already warm in the worker via `embedding_service`):**
   - Within each logical unit, split into sentences (reuse existing sentence regex, line ~32).
   - Embed each sentence with BGE. Compute cosine distance between consecutive sentences.
   - Cut where distance exceeds the configured percentile (`CHUNK_SEMANTIC_BREAKPOINT_PERCENTILE`,
     default 95). This is the LlamaIndex `SemanticSplitterNodeParser` algorithm.
   - **Never** merge sentences across a section header (hard boundary preserved).
   - **No overlap.** Variable chunk size.
   - Guard rails: a chunk may not exceed `CHUNK_MAX_TOKENS` (default 1024) — force a cut at the
     next-best (highest-distance) interior breakpoint; chunks below `MIN_CHUNK_SIZE_TOKENS`
     (existing, 50) merge into the neighbour in the same section.
2. **Batched Gemma enrichment:** after chunks are formed, group them (e.g. up to ~8 per call) and
   issue one `gemma_client.chat()` (sync — worker context) returning a JSON array of
   `{section_title, keywords: [...], semantic_type}` aligned by index. Use a strict prompt + JSON
   parse with a safe fallback (if parse fails, keep heuristic section_title from the breadcrumb and
   `semantic_type="paragraph"`, `keywords=[]`). This fills the previously-empty columns.
3. **Context prefix:** keep the section-breadcrumb prefix behaviour (`_build_context_prefix`) — it
   is not overlap and aids retrieval.

**Config additions (`app/config.py`)** — all with defaults (a missing `.env` key must never kill
worker boot; see memory `worker-restart-and-config-defaults`):
- `CHUNK_USE_SEMANTIC: bool = True` — master toggle; `False` restores the old fixed-size path.
- `CHUNK_SEMANTIC_BREAKPOINT_PERCENTILE: int = 95`
- `CHUNK_MAX_TOKENS: int = 1024`
- `CHUNK_ENRICH_BATCH_SIZE: int = 8`
- `CHUNK_OVERLAP_TOKENS` retained but unused when semantic path active (set effective overlap 0).

The old fixed-size code path stays reachable behind `CHUNK_USE_SEMANTIC=False` so we can revert
instantly without a redeploy.

### Part 2 — `stored_in` accuracy

In `store_image_derived_chunks()` (`storage_service.py:402-458`): for each image row, run the
cross-store INSERT (`_store_image_as_table` / `_store_image_as_text_chunk` /
`_store_image_as_clause`) **and** `_update_image_stored_in()` inside **one DB transaction with a
per-image try/except**. Both commit together or roll back together.

- content + embedding present and write+update succeed → `stored_in = detected_store`.
- skipped (no content / no embedding) or any exception → `stored_in` stays `"image_store"`
  (honest: the content really only exists in image_store), error logged, loop continues to the
  next image (one bad image must not abort the batch).

`store_table_crop_images()` (461-521) already sets both `detected_store` and `stored_in` to
`"table_store"` consistently — leave as is, but it must also set `from_image_store=TRUE` (Part 3).

### Part 3 — `from_image_store` boolean

New migration `app/db/migrations/007_from_image_store.sql` (manual-apply via Supabase SQL Editor —
no Alembic):

```sql
SET search_path TO multi_store_rag_working;
ALTER TABLE vector_store  ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE table_store   ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE clause_store  ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE image_store   ADD COLUMN IF NOT EXISTS from_image_store BOOLEAN NOT NULL DEFAULT TRUE;
```

One uniform column across all stores. Set value at write time:
- `TRUE` — `_store_image_as_table()`, `_store_image_as_text_chunk()`, `_store_image_as_clause()`,
  and the table_store INSERT in `store_table_crop_images()`.
- `FALSE` — normal `_store_vector_chunks()` and normal `table_store` / `clause_store` inserts that
  do not originate from an image.
- `image_store` rows: column exists with default TRUE (every image_store row is by definition an
  image), no code change needed for the default.

Update the repository INSERT builders that construct column lists/placeholders
(`app/db/repositories/` + the inline INSERTs in `storage_service.py`) to include the new column.
The migration uses `ADD COLUMN ... DEFAULT FALSE`, so existing rows and any INSERT that omits the
column remain valid — code changes only need to set TRUE on the image-derived paths.

## Files touched

| Part | Files |
|---|---|
| 1 | `app/services/chunker.py`, `app/config.py` (+ possibly a small `semantic_chunker` helper) |
| 2 | `app/services/storage_service.py` (`store_image_derived_chunks`, `_update_image_stored_in`) |
| 3 | `app/db/migrations/007_from_image_store.sql`, `app/services/storage_service.py` (image-derived inserts + crop path), `app/db/repositories/*` INSERT builders |

`storage_service.py` is touched by Parts 2 and 3 → those two run as **one** implementation unit to
avoid edit collisions. Part 1 (chunker/config) and the migration file are independent.

## Verification

Reprocess the finance PDF, then inspect `multi_store_rag_working`:
- **vector_store:** chunks have non-null `section_title`, non-empty `keywords`, non-null
  `semantic_type`; variable `chunk_word_count`; no duplicated leading text between consecutive
  chunks (overlap gone).
- **image_store:** for every row `stored_in` matches where the content actually lives.
- **from_image_store:** TRUE on image-derived rows in vector/table/clause stores; FALSE on
  normally-chunked rows; TRUE on all image_store rows.
- Worker boots clean (no pydantic ValidationError) with the new config keys absent from `.env`.

## Out of scope

- No change to the retrieval/query path beyond reading the new column if convenient.
- No Alembic / migration-runner introduction (manual apply stays).
- `vlm_ocr_text` empty-for-icons behaviour is correct (no text to OCR) — not addressed here.
