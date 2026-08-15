# Image Cross-Store Routing — Generic, Schema-Complete, Traceable

Date: 2026-06-30
Branch: `feat/semantic-chunking-store-tracking`
Builds on: `2026-06-30-semantic-chunking-store-tracking-design.md`

## Goal

Every image the VLM classifies into another store must become a **fully populated,
schema-compliant** record in that store, with **store-specific structured_content**, an
embedding derived from the destination content, complete metadata, full traceability back to the
originating image, transactional integrity, and `stored_in` flipped **only after** a verified
successful insert. Routing must be **generic** (registry-driven), not hardcoded per store, and must
work for `table_store`, `clause_store`, `vector_store`, `document_store`, and any future store.

## Audit result (current state)

- Routing hardcoded to 3 stores; `document_store` never routed (SQL filter + VLM prompt + `_canonical_store`).
- `structured_content` is generic free text; table structure / clause fields are discarded at insert.
- Destination rows leave most columns NULL.
- Cross-store embedding reuses the image embedding rather than re-deriving from destination text.
- Traceability limited to `{"source":"image","image_index"}`; no `source_image_id`; `table_store.image_storage_path` left NULL.
- `stored_in`/transaction are correct (prior commit) except `ON CONFLICT DO NOTHING` can flip `stored_in` on a 0-row insert.

## Design — a store registry

### New module: `app/services/store_router.py`

A single registry decouples routing from individual stores. Adding a future store = add one entry.

```python
# Canonical store name -> StoreHandler
STORE_REGISTRY: dict[str, StoreHandler]

@dataclass
class StoreHandler:
    name: str                      # 'table_store' | 'clause_store' | 'vector_store' | 'document_store'
    content_type: str              # image_store.content_type value ('table'|'text'|...)
    schema_hint: str               # injected into the VLM prompt: the JSON shape this store expects
    def parse(self, structured_raw: str, ctx: ImageCtx) -> dict: ...
        # Parse the VLM structured_content (JSON object preferred; plain text tolerated)
        # into a dict of destination columns. NEVER raises — falls back to text.
    def canonical_text(self, parsed: dict, ctx: ImageCtx) -> str: ...
        # The text to embed for THIS store (e.g. table markdown, clause_text, chunk_text).
    def insert(self, conn, parsed: dict, embedding: list, ctx: ImageCtx) -> int: ...
        # INSERT into the store, populating EVERY applicable column + traceability.
        # Returns affected rowcount. Caller owns the transaction.
    def validate(self, conn, ctx: ImageCtx) -> None: ...
        # Re-select the inserted row; assert required columns non-null + embedding present.
        # Raise on failure so the txn rolls back and stored_in stays honest.
```

`ImageCtx` carries traceability + source data: `document_id`, `image_id` (image_store.id UUID),
`image_index`, `page_number`, `bbox_json`, `storage_path`, `ocr_text`, `vlm_ocr_text`,
`detected_store`, `confidence`, `reason`.

`get_handler(detected_store) -> StoreHandler | None` — returns None for `image_store`/unknown so the
generic loop simply leaves those rows alone.

### Per-store structured_content schema (VLM emits a JSON object)

- **table_store** — `{"title", "headers":[...], "rows":[[...]], "units", "fiscal_year", "reporting_period", "currency", "table_category", "notes"}`
- **clause_store** — `{"clause_title", "clause_text", "clause_type", "parties":[...], "obligor", "obligee", "key_dates":{...}, "monetary_values":{...}, "obligations":[...], "risk_level", "risk_rationale"}`
- **vector_store** — `{"text", "section_title", "keywords":[...], "semantic_type"}` (clean retrieval text, NOT a caption)
- **document_store** — `{"chunk_text", "chunk_type", "section_title", "citation":{...}, "entities":[...]}`

The schema_hint strings live in the registry and are the single source of truth — the VLM prompt is
assembled from them so prompt and parser never drift.

### Column-population maps (populate EVERYTHING applicable)

- **table_store.insert** — title→table_title; headers/rows→json_data + csv_data + row_count + col_count;
  canonical markdown→raw_text + markdown_text; has_numeric/currency/percentages computed from cells;
  units→detected_units; fiscal_year/reporting_period/currency/table_category from VLM; page_number;
  bbox; **image_storage_path = ctx.storage_path**; embedding; table_summary = short canonical text;
  table_metadata = traceability; from_image_store=TRUE.
- **clause_store.insert** — clause_title, clause_text, clause_word_count, clause_type, risk_level,
  risk_rationale, obligor, obligee, parties_mentioned, key_dates(JSONB), monetary_values(JSONB),
  conditions(from obligations), page_number, page_numbers, section_path, embedding,
  clause_metadata = traceability; from_image_store=TRUE.
- **vector_store.insert** — chunk_text, word/char counts, page_number, page_numbers, bbox,
  section_title, section_level, semantic_type, keywords, embedding, chunk_metadata = traceability;
  from_image_store=TRUE.
- **document_store.insert** — chunk_text, chunk_type, citation_key/source_* fields when present,
  page_number, section_title, section_type, contains_* flags, entities_mentioned, embedding,
  chunk_metadata = traceability. (document_store has no from_image_store until migration 007 is
  applied — the column IS in 007, so set it TRUE.)

### Traceability (no new migration)

Each destination row's `*_metadata` JSONB carries:
`{"source":"image","source_image_id": <image_store.id>, "image_index", "page_number",
"detected_store", "confidence", "reason_for_store_selection"}`. Plus `table_store.image_storage_path`
is set to the image's storage path (dedicated column already exists). This satisfies req 7 without a
new migration; a future migration could promote `source_image_id` to a real column if needed.

### Re-embedding (req 5)

Inside the per-image transaction, embed `handler.canonical_text(parsed)` with
`embedding_service.embed_passages([text])[0]`. This derives the vector from the destination content,
not the generic image blob. BGE is already warm in the worker.

### Generic dispatch + stored_in order (req 1, 6, 8)

Rewrite `store_image_derived_chunks(document_id)`:

1. SELECT all image_store rows for the doc WHERE `detected_store <> 'image_store'` (no hardcoded
   list — also pull `id, storage_path, vlm_ocr_text`). ORDER BY image_index.
2. For each row:
   - `handler = get_handler(detected_store)`; if None → skip (leave stored_in).
   - Build `ImageCtx`. `parsed = handler.parse(structured_content, ctx)`.
   - `canonical = handler.canonical_text(parsed, ctx)`; skip (stored_in unchanged) if empty.
   - `embedding = embed_passages([canonical])[0]`.
   - `with get_db() as conn:`  (one transaction)
       - `n = handler.insert(conn, parsed, embedding, ctx)`
       - **if n < 1: raise** (no silent ON CONFLICT skip flipping stored_in)
       - `handler.validate(conn, ctx)`  — verify the row is complete
       - `_update_image_stored_in_conn(conn, image_id, detected_store)`
   - on success → stored_in = detected_store; on any exception → caught, logged, stored_in stays
     `image_store`, loop continues.
3. Log succeeded/skipped/failed counts.

Replace `ON CONFLICT DO NOTHING` with an explicit pre-delete (idempotent reprocess) OR keep upsert
but check rowcount; either way `stored_in` must only flip when a row is verified present.

### VLM changes (`image_analysis_service.py`)

- Add **Document Store** to the store list; extend `_canonical_store` to map "document"/"research"/
  "paper"/"citation" → `document_store`; extend `_content_type_from_store`.
- Assemble the prompt's structured_content instructions from `store_router` schema_hints so the VLM
  emits a JSON object matching the store it selects. Keep the single-call design (no second pass).
- Backward compatible: parsers tolerate plain-text structured_content (fall back to text fields).

## Files

| Wave | File | Work |
|---|---|---|
| 1 | `app/services/store_router.py` (new) | registry, ImageCtx, 4 handlers, schema hints, parse/canonical_text/insert/validate |
| 2a | `app/services/storage_service.py` | generic `store_image_derived_chunks`; keep `_conn` helpers or delegate to registry; rowcount guard |
| 2b | `app/services/image_analysis_service.py` | document_store + JSON-object structured_content from registry hints |
| — | `app/services/ingestion_orchestrator.py` | no change needed (calls store_image_derived_chunks already) |

## Verification

Reprocess a doc with figures, then per routed image confirm: destination row exists in the correct
store; every required column populated + optionals where data exists; embedding present & derived
from destination text; `*_metadata` carries `source_image_id` + page; `stored_in` == destination;
image_store row still present; no NULL-heavy partial rows. Unit tests: parsers (JSON + plain-text
fallback), generic dispatch picks the right handler, rowcount-0 leaves stored_in unchanged,
validate() raises on a missing required column.

## Out of scope

- No second VLM pass (single schema-directed call).
- No new migration (traceability via existing JSONB + table_store.image_storage_path; from_image_store
  is migration 007, already authored).
