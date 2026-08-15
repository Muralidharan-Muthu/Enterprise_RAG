# Phase 1 — Multimodal Extraction + Image Store + Bucket Assets

**Date:** 2026-06-19
**Status:** Approved (design), pending implementation plan
**Branch:** `feat/phase1-image-store`

## Context

Multi-Store RAG Chatbot is a multi-store RAG system (FastAPI + Celery + Supabase/pgvector + Next.js).
Today, images inside PDFs are **not** captured as first-class objects:

- Docling only *detects* pictures (`PictureItem`) and records their page numbers.
- `image_analysis_service` renders **whole pages** that contain pictures to PNG, sends
  them to Gemma-4 vision, and appends the returned description as plain text blocks that
  get chunked into `vector_store`.
- The image pixels are never saved; nothing is uploaded to the bucket except the original PDF;
  there is no `image_store`; tables are stored as markdown/JSON only (no visual crop).

Consequence: image content is lossy, noisy (whole-page descriptions pollute `vector_store`),
not retrievable as a distinct modality, and not traceable to an exact figure.

This phase makes figures and tables **first-class, retrievable, traceable assets**.

This is **Phase 1 of 4**. Later phases (separate specs): (2) retrieval accuracy strategy,
(3) easy traces / citation deep-links, (4) multi-PDF connection / graph.

## Goals

1. Extract each figure/picture from a PDF as a **cropped image** with its page + bbox.
2. Generate a **caption** (visual description) and **OCR text** per image via Gemma-4 vision.
3. Upload each cropped image to **Supabase Storage** (private bucket), record its path,
   and mint **signed URLs** on read.
4. Store images in a new **`image_store`** table with a BGE embedding of `caption + ocr_text`
   so images live in the **same 1024-dim vector space** as text (unified retrieval, no second model).
5. Crop **table images** too (bbox) and link them from `table_store`, so a table citation can
   show the visual table, not only markdown.
6. Full **provenance**: every image/table row chains to `document_registry` (→ original PDF
   `storage_path`, filename) plus `page_number` + `bbox` to pinpoint the exact region.
7. **Wire `image_store` into retrieval** minimally so images surface in results now
   (full intent-routing/fusion is Phase 2).

## Non-Goals (deferred)

- CLIP / native visual embeddings (we embed the text description instead — same vector space).
- Intent classification / cross-store fusion / RRF (Phase 2).
- Citation deep-link UI that opens the PDF at a bbox (Phase 3).
- Cross-document graph / multi-hop (Phase 4).
- Re-architecting the existing chunking strategies beyond removing the whole-page-vision noise.

## Design

### A. Extraction (`document_parser.py`)

- Enable in `PdfPipelineOptions`: `generate_picture_images=True`, `generate_table_images=True`,
  `images_scale=2.0` (≈144 DPI — good for both vision quality and display).
- During Docling iteration, for each `PictureItem`: capture
  `ExtractedImage{ image_index, page_number, bbox, png_bytes, caption_hint }`.
  - `png_bytes` from `PictureItem.get_image(doc)` → PNG. If a figure has no rasterized
    image (rare), fall back to cropping the page render by bbox.
- For each `TableItem`: also capture a cropped `png_bytes` (via `get_image(doc)`) and attach
  to the existing `ExtractedTable` as `image_png_bytes` (new optional field).
- `ParsedDocument` gains `images: list[ExtractedImage]`. `ExtractedTable` gains
  `image_png_bytes: bytes | None`.

### B. Vision captioning (`image_analysis_service.py`, refactored)

- Replace whole-page rendering with **per-image** analysis.
- New `describe_image(png_bytes) -> {caption: str, ocr_text: str}`:
  base64-encode the cropped PNG → Gemma-4 vision (same CDAC endpoint already used) →
  prompt asks for (1) a concise factual description incl. any chart numbers/KPIs,
  (2) verbatim text visible in the image (OCR). Returns both.
- The old whole-page → `vector_store` text path is **removed** (images now live in `image_store`),
  eliminating the noisy duplicate chunks.

### C. Bucket assets (`supabase_storage.py`)

- Reuse `upload_file(bucket, path, content, content_type)` with `content_type="image/png"`.
- Paths: images → `images/{document_id}/{image_index}.png`; table crops →
  `tables/{document_id}/{table_index}.png`.
- Add `create_signed_url(bucket, path, expires_in=3600) -> str` (Supabase
  `storage.from_(bucket).create_signed_url`). Private bucket; URLs minted on read, not stored.
- Upload failure for one asset logs + skips that asset; never fails the whole ingestion.

### D. New table: `image_store` (migration `003_image_store.sql`, additive)

```
id            UUID PK default uuid_generate_v4()
document_id   UUID NOT NULL REFERENCES document_registry(id) ON DELETE CASCADE
image_index   INT NOT NULL
page_number   INT
bbox          JSONB                 -- {x1,y1,x2,y2}
storage_path  TEXT NOT NULL         -- images/{document_id}/{idx}.png
storage_bucket TEXT NOT NULL
mime_type     TEXT default 'image/png'
width         INT
height        INT
caption       TEXT                  -- Gemma vision description
ocr_text      TEXT                  -- text visible in the image
embedding     vector(1024)          -- BGE(caption + '\n' + ocr_text)
image_metadata JSONB default '{}'
created_at    TIMESTAMPTZ default NOW()
```
Indexes: HNSW on `embedding` (`vector_cosine_ops`, m=16, ef_construction=128);
B-tree on `document_id`. Lives in `multi_store_rag_working` schema like all others.
`table_store` gets a new column `image_storage_path TEXT` (the cropped table PNG path).

### E. Storage routing (`storage_service.py` + repo)

- New `_store_images(document_id, images, embeddings)` → bulk insert into `image_store`
  (psycopg2 `execute_values`, same pattern as other stores).
- New repository `app/db/repositories/image_store.py` with `insert_images(...)`.
- `_store_tables` extended to set `image_storage_path` when a table crop was uploaded.

### F. Orchestrator flow (`ingestion_orchestrator.py`)

New ordering after parse + route:
1. Parse (now yields `images` + table crops).
2. Route (unchanged).
3. **Images stage (new):** for each `ExtractedImage`: `describe_image` → upload PNG to bucket
   → collect `{caption, ocr_text, storage_path, page, bbox, w, h}`. BGE-embed
   `caption+ocr_text` in a batch. `_store_images(...)`.
   Upload table crops to bucket here too; pass paths to table storage.
4. Chunk + embed + store text/tables/clauses (unchanged, minus the removed whole-page vision text).
- Add `"images"` to `ingestion_jobs.stage_timings`. Update `document_registry.has_images`
  to mean "≥1 image extracted to image_store".

### G. Retrieval wiring (`retriever_service.py`, minimal)

- Add `_query_image_store(query_embedding, top_k, ...)` → cosine `<=>`, JOIN `document_registry`,
  `WHERE dr.status='completed'`, return rows.
- Extend `RetrievedChunk` with: `image_storage_path: str|None`, `caption: str|None`,
  `ocr_text: str|None` (and reuse `text` = `caption + '\n' + ocr_text` for rerank/synthesis).
  `store_type='image'`.
- Include image_store in `retrieve()` when no `document_types` filter, and when
  `financial`/`policy`/`research` are requested (images commonly accompany these).
- `query.py` response: `CitationItem` gains `image_url` (signed URL minted at response time
  from `image_storage_path`), `caption`, `ocr_text`. Synthesis treats caption+ocr as context.

## Data Flow

```
PDF → Docling(generate_picture_images, generate_table_images)
    → ParsedDocument{ text_blocks, tables[+image_png], images[png+bbox+page] }
        ├─ images:  describe_image(Gemma vision) → {caption, ocr}
        │            → upload PNG → bucket(images/{doc}/{i}.png)
        │            → BGE(caption+ocr) → image_store row (FK doc, page, bbox, path, embedding)
        ├─ tables:   upload crop → bucket(tables/{doc}/{t}.png) → table_store.image_storage_path
        └─ text:     chunk → embed → vector_store / clause_store / document_store (unchanged)

Query → embed → retrieve(incl. image_store) → rerank → synthesize
     → CitationItem{ ..., image_url=signed(path), caption, ocr_text }
```

## Error Handling

- Vision call fails → store image with `caption=NULL, ocr_text=NULL`, embed page/filename
  context string so it stays traceable; do not fail ingestion.
- Bucket upload fails for an asset → log, skip that asset, continue.
- Docling figure with no raster → page-render crop by bbox fallback.
- Migration is additive; existing rows/queries unaffected. If `image_store` is empty,
  retrieval behaves exactly as today.

## Testing

- **Unit (`tests/test_parser.py`):** sample PDF with ≥1 figure → `ParsedDocument.images`
  non-empty, each has bbox + page + non-empty png_bytes.
- **Unit (`tests/test_supabase_storage.py`):** upload PNG + `create_signed_url` round-trip
  (real bucket, gated `-m slow`, or mocked client).
- **Unit (`tests/test_storage_image.py`):** `_store_images` inserts rows; query returns them.
- **Unit (`tests/test_retriever.py`):** with an image row present, `retrieve()` yields a
  `store_type='image'` chunk carrying `image_storage_path` + caption.
- **Integration (manual, documented):** ingest a PDF with charts → `image_store` populated
  + PNGs in bucket → query "revenue chart" → image citation with working signed URL.

## Parallelizable Work Units

Independent enough to fan out to parallel agents:
1. Migration `003_image_store.sql` + `table_store.image_storage_path`.
2. `document_parser.py` image/table-crop extraction + `ExtractedImage` model.
3. `image_analysis_service.py` refactor to per-image `describe_image`.
4. `supabase_storage.py` `create_signed_url` + image upload paths.
5. `image_store` repo + `storage_service._store_images` + table-crop linkage.
6. `retriever_service` + `query.py` image wiring.
7. Orchestrator stitch (depends on 2,3,4,5 — integrate last).

## Acceptance Criteria

- Ingesting a PDF with figures populates `image_store` with embeddings + bucket PNGs.
- Each `image_store` row traces to its source PDF (document_id → storage_path) + page + bbox.
- A query about image content returns an image citation with a working signed URL + caption.
- Tables carry a `image_storage_path` to a visual crop.
- No regression: text/table/clause/research retrieval still works; empty image_store = today's behavior.
- Whole-page-vision noise removed from `vector_store`.
