# Phase 3 — Easy Traces (spec + plan)

**Date:** 2026-06-19 · **Branch:** `feat/phase3-traces` · Phase 3 of 4.

## Goal

Every citation can jump to its exact source: a signed URL to the **original PDF anchored to the page** (`#page=N`), the **bbox** region, the page number and filename. Frontend renders a clickable "Open source ↗" link per cited document.

## Current state

Citations already carry `document_id, filename, page_number, store_type, image_url (signed crop), caption`. Provenance exists in DB: every store row → `document_registry.storage_path` (original PDF in bucket); `bbox` JSONB on `vector_store`, `table_store`, `image_store` (not on clause/research). What's missing: a signed URL to the *source PDF* and the bbox surfaced to the client.

## Design

**Backend:**
1. Retrieval SQL — add `dr.storage_path, dr.storage_bucket` to every store SELECT, and `bbox` for the stores that have it (vector, table, image). Carry on `RetrievedChunk`: `pdf_storage_path`, `pdf_bucket`, `bbox` (dict|None; psycopg2 returns jsonb as dict).
2. `CitationItem` (query.py) += `pdf_url` (signed original-PDF URL with `#page=N` when page known) + `bbox`.
3. `_citation_from_chunk` mints `pdf_url` from `pdf_bucket`/`pdf_storage_path` (best-effort; None on failure, like image_url). Keeps page anchor.

**Frontend:**
4. `types.ts` `CitationItem` — sync with backend: add `pdf_url, bbox, image_url, caption, ocr_text`; add `"table" | "image"` to `store_type`.
5. `query/page.tsx` `groupSources` — carry one `pdf_url` per filename; render the filename badge as an `<a target="_blank">` link when `pdf_url` present (page anchor already in the URL).

## Non-goals

In-browser bbox highlight overlay (we provide the bbox; rendering a PDF.js highlight is a later polish). Phase 4 graph.

## Tasks (TDD, inline)

- **P3T1** backend retrieval — `RetrievedChunk` fields + SQL SELECT additions + inline mapping (vector/table/image bbox; all carry pdf path/bucket). Live-verify a real retrieve populates `pdf_storage_path`.
- **P3T2** backend citation — `CitationItem.pdf_url/bbox` + `_citation_from_chunk` mints `signed(pdf_bucket, path)+#page=N`. Unit-test (mock signed URL): pdf_url present + anchored; failure → None; bbox passed through.
- **P3T3** frontend — types sync + clickable source link in `groupSources`.
- **P3T4** live verify — query → citation has working `pdf_url` opening the PDF at the page.

## Acceptance

- A citation returns `pdf_url` (signed) anchored to its page + `bbox` (where available).
- Frontend source badge is a clickable link opening the PDF at the cited page.
- Non-image/text stores still work; signed-URL failure is non-fatal.
