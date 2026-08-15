# Universal VLM Table Pipeline — Design

**Date:** 2026-07-09
**Status:** Approved (pending spec review)

## Problem

Table extraction currently has two divergent paths:

- **Docling-only**: table with no rendered crop → `raw_text`/`markdown_text`/`json_data` only.
- **Docling → crop → VLM → reconcile**: table with a rendered crop → VLM produces
  rich `structured_content`, reconstructs headers/rows, reconciles against Docling.

The VLM `structured_content` (the clean, post-processed, retrieval-ready extraction —
the same quality column that `image_store` persists) is **consumed and discarded** during
reconstruction. It is never saved on `table_store`. Result: `table_store` UI/retrieval
surface only the raw Docling text, and tables without a crop never get VLM treatment at all.

## Goal

Every detected data table follows ONE pipeline:

```
PDF → Docling detects table → generate image crop → VLM → structured_content
   → reconcile with Docling (Docling = numeric source of truth) → save table
```

No Docling-only path. Persist `structured_content` on `table_store`, embed it, and surface
it in the UI.

## Decisions (locked)

- **Embedding**: ADD a second vector column `structured_content_embedding vector(1024)`
  with its own HNSW index. Keep the existing `table_summary`-based `embedding` column.
- **UI**: ADD alongside — show `structured_content` as primary; keep raw OCR/markdown
  available (collapsible/secondary). Do not hide raw text.

## Non-goals

- No change to the reconcile/numeric-faithfulness gate logic — Docling remains the source
  of truth for numeric cell values.
- Empty-grid chart/figure "tables" (Docling TableFormer flags them, 0×0, no cell data —
  `document_parser.py:494`) stay skipped. They have no table data to reconcile and are
  already captured by the image pipeline (VLM → `image_store`). This is the single, explicit
  exception to "every detected table".

## Design

### 1. Guarantee a crop for every data table — `document_parser.py`

Current: `document_parser.py:500-506` sets `image_png_bytes` from `table.get_image(doc)`,
which returns `None` on render failure → that table silently falls onto the Docling-only path.

Change: when `table.get_image(doc)` returns `None`, render a fallback crop from the table
`bbox` using PyMuPDF (`fitz`, already a dependency — see `document_parser.py:715,810`).

New helper:
```python
def _render_crop_via_fitz(pdf_path: Path, page_number: int, bbox: dict) -> Optional[bytes]:
    """Render a PNG crop of `bbox` on `page_number` (1-based) from the PDF via fitz.
    Returns None if the path/page/bbox is unusable (fail-open — caller keeps Docling-only
    for that one table rather than crash)."""
```

- `bbox` comes from `_get_table_bbox(table)` (already computed at `:521`).
- Scale crop with `settings.DOCLING_IMAGES_SCALE` for parity with Docling-rendered crops.
- Fail-open: if fitz can't render (encrypted/damaged/missing bbox), leave `image_png_bytes`
  None and log — that single table degrades to Docling-only rather than failing the doc.

Net effect: `ExtractedTable.image_png_bytes` is non-null for essentially every data table;
the VLM step below then runs on all of them.

### 2. VLM on all tables — `table_reconstruction.py` + orchestrator

- `reconstruct_tables_with_vlm` (`table_reconstruction.py:357`) already targets every table
  with a crop (`:375` filter). With step 1, that is now all data tables — no code change to
  the targeting logic, but the orchestrator guard must widen.
- Orchestrator `ingestion_orchestrator.py:377` currently gates the VLM call on
  `any(... image_png_bytes ...)`. Keep this guard (still correct — it's now true whenever
  there are tables), but ensure the crop-generation in step 1 runs before it.
- Reconcile gate unchanged: Docling stays numeric source of truth; VLM fills
  structure/labels; numeric-faithfulness gate as-is.

### 3. Persist `structured_content` — migration + `storage_service`

New migration `backend/app/db/migrations/017_table_structured_content.sql`:
```sql
SET search_path TO multi_store_rag_working, public, extensions;

ALTER TABLE multi_store_rag_working.table_store
    ADD COLUMN IF NOT EXISTS structured_content TEXT,
    ADD COLUMN IF NOT EXISTS structured_content_embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_table_store_sc_embedding
    ON multi_store_rag_working.table_store
    USING hnsw (structured_content_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
```

`storage_service._store_tables` (`storage_service.py:215+`, INSERT at `:331`):
- Accept a new per-table-index input `structured_content` (threaded from the orchestrator's
  existing `table_vlm_analyses` dict — `ingestion_orchestrator.py:376`).
- Value = VLM `structured_content` when present, else fall back to `markdown_text` (so
  crop-less / VLM-failed tables still populate the column with the best available text).
- Add `structured_content` and `structured_content_embedding` to the INSERT column list and
  VALUES template.

### 4. Embed `structured_content` — `ingestion_orchestrator`

- Reuse the existing BGE embedding path. Build one embedding per table from
  `structured_content` (fallback text when empty) alongside the existing table-summary
  embedding build (`_build_table_embeddings`, `ingestion_orchestrator.py:694`).
- Store into the new `structured_content_embedding` column.

### 5. Retrieval uses the new vector — `retriever_service.py`

- Parent-summary table ANN (`_query_table_store_parent_only`, searches `ts.embedding`):
  prefer `structured_content_embedding` when non-null, else fall back to `embedding`.
  Implementation: `COALESCE`-style — order by
  `COALESCE(ts.structured_content_embedding, ts.embedding) <=> query`. (Both are 1024-dim
  BGE vectors in the same space, so a single query embedding compares against either.)
- Child row-window search (`table_chunk_store`) unchanged.

### 6. UI — `documents.py` + frontend

- `documents.py:395` `/{document_id}/chunks?store=table`: add `structured_content` to the
  selected columns for the `table` store.
- `documents.py:258+` images endpoint already returns `structured_content` — no change.
- Frontend `frontend/src/components/documents/ChunkViewer.tsx`: the table view currently
  renders `parseMarkdownTable(t.markdown_text)` with `t.raw_text` fallback (`:454`, `:529`).
  Add a `structured_content` primary block above it; move the parsed markdown table / raw
  text into a collapsible/secondary section. Image view (same component / images endpoint)
  shows `structured_content` primary, raw OCR collapsible.

## Data flow (after change)

```
document_parser.parse_document
  → Docling table detected
  → image_png_bytes = get_image(doc)  OR  _render_crop_via_fitz(bbox)   [step 1]
ingestion_orchestrator
  → reconstruct_tables_with_vlm → table_vlm_analyses[idx].structured_content   [step 2]
  → embed structured_content → structured_content_embedding                    [step 4]
  → store_chunks → _store_tables(structured_content, structured_content_embedding) [step 3]
query
  → _query_table_store_parent_only: ORDER BY COALESCE(sc_emb, emb) <=> q        [step 5]
UI
  → /chunks?store=table returns structured_content; frontend shows it primary   [step 6]
```

## Backfill / migration notes

- Existing `table_store` rows have `structured_content` = NULL and
  `structured_content_embedding` = NULL. Retrieval `COALESCE` falls back to the old
  `embedding` for them → no regression. New/reprocessed docs populate the columns.
- Optional (not in scope now): a reprocess pass to backfill old docs.
- Migration must be applied manually via Supabase SQL Editor (project convention — no
  Alembic) BEFORE reprocessing, else the INSERT with the new columns fails.

## Testing

- `document_parser`: unit test that `_render_crop_via_fitz` returns PNG bytes for a known
  bbox on a fixture PDF, and returns None (fail-open) on a bad bbox/encrypted file.
- `document_parser`: a table whose `get_image` is monkeypatched to return None still ends
  up with non-null `image_png_bytes` via the fitz fallback.
- `storage_service._store_tables`: `structured_content` persisted; falls back to
  `markdown_text` when VLM analysis absent.
- `retriever_service`: parent-only table query orders by `COALESCE(sc_emb, emb)` — rows
  with only the legacy embedding still returned.
- Existing table reconstruction / reconcile tests must stay green (no gate change).

## Files touched

- `backend/app/services/document_parser.py` — fallback crop helper + wiring.
- `backend/app/services/ingestion_orchestrator.py` — widen guard, embed structured_content, thread to storage.
- `backend/app/services/storage_service.py` — persist structured_content (+ embedding).
- `backend/app/db/migrations/017_table_structured_content.sql` — new columns + HNSW index.
- `backend/app/services/retriever_service.py` — COALESCE parent ANN.
- `backend/app/api/routes/documents.py` — expose structured_content in table chunks.
- `frontend/src/components/documents/ChunkViewer.tsx` — table/image chunk views: structured_content primary, raw collapsible.
