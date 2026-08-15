# Phase 1 — Multimodal Extraction + Image Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract figures/tables from PDFs as cropped images, caption them with Gemma vision, upload to the Supabase bucket, and store them in a new `image_store` table with BGE embeddings so images are retrievable and traceable to their source PDF.

**Architecture:** Docling extracts per-figure PNGs (with bbox + page). Each image is captioned + OCR'd by Gemma-4 vision, uploaded to a private Supabase bucket (signed URLs minted on read), and inserted into `image_store` with a BGE embedding of `caption + ocr_text` — same 1024-dim space as text, so retrieval is unified. Provenance chains every image to `document_registry` via `document_id` + `page_number` + `bbox`.

**Tech Stack:** Python 3.11, Docling 2.14.0, Pillow, psycopg2 + pgvector, Supabase Storage (supabase 2.10.0 / storage3 0.9.0), BGE `bge-large-en-v1.5`, Gemma-4 (CDAC OpenAI-compatible vision endpoint), Celery (solo pool on Windows), FastAPI, pytest.

## Global Constraints

- Schema: all tables in `multi_store_rag_working` (connection sets `search_path`). Copy verbatim into every SQL string.
- Embedding dimension: **1024** (`vector(1024)`), model `BAAI/bge-large-en-v1.5`.
- HNSW index params on every embedding column: `vector_cosine_ops`, `m = 16, ef_construction = 128`.
- Migration must be **additive** — no ALTER/DROP on existing columns; existing queries must keep working with an empty `image_store`.
- Secrets (`SUPABASE_*`, `GEMMA4_API_KEY`) live only in gitignored `.env`; never commit them.
- Bucket is **private**: store `storage_path`; mint signed URLs at read time, never persist URLs.
- Vision/upload failures are **non-fatal**: log + skip the asset, never fail the whole ingestion.
- Windows: Celery runs `--pool=solo`. Tests run from `backend/` with `.venv` active.
- DB/integration tests that need network are marked `@pytest.mark.slow` (excluded by `pytest -m "not slow"`).

---

### Task 1: Data models — `ExtractedImage`, `ParsedDocument.images`, table crop field

**Files:**
- Modify: `backend/app/models/document.py`
- Test: `backend/tests/test_models_image.py`

**Interfaces:**
- Produces:
  - `ExtractedImage(image_index:int, page_number:int, bbox:Optional[BoundingBox], png_bytes:bytes, width:int, height:int)` — dataclass.
  - `ParsedDocument.images: list[ExtractedImage]` (default empty).
  - `ExtractedTable.image_png_bytes: Optional[bytes]` (default None).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_models_image.py
from app.models.document import ExtractedImage, ParsedDocument, ExtractedTable, BoundingBox


def test_extracted_image_fields():
    img = ExtractedImage(
        image_index=0, page_number=2,
        bbox=BoundingBox(x1=1, y1=2, x2=3, y2=4),
        png_bytes=b"\x89PNG", width=100, height=50,
    )
    assert img.image_index == 0
    assert img.page_number == 2
    assert img.png_bytes == b"\x89PNG"
    assert img.width == 100 and img.height == 50


def test_parsed_document_images_default_empty():
    pd = ParsedDocument(
        doc_id="d", filename="f.pdf", raw_text="", text_blocks=[], tables=[],
        page_count=1, word_count=0, has_tables=False, has_images=False,
    )
    assert pd.images == []


def test_extracted_table_image_bytes_default_none():
    t = ExtractedTable(table_index=0, page_number=1, headers=[], rows=[])
    assert t.image_png_bytes is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models_image.py -v`
Expected: FAIL — `ImportError: cannot import name 'ExtractedImage'`

- [ ] **Step 3: Edit `backend/app/models/document.py`**

Add the new dataclass after `BoundingBox` (after line 11):

```python
@dataclass
class ExtractedImage:
    image_index: int
    page_number: int
    bbox: Optional[BoundingBox]
    png_bytes: bytes
    width: int = 0
    height: int = 0
```

Add `image_png_bytes` to `ExtractedTable` (after its `markdown_text` field):

```python
    markdown_text: str = ""
    image_png_bytes: Optional[bytes] = None
```

Add `images` to `ParsedDocument` (after the `image_page_numbers` field, line 49):

```python
    image_page_numbers: list[int] = field(default_factory=list)  # pages with embedded images
    images: list["ExtractedImage"] = field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models_image.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/document.py backend/tests/test_models_image.py
git commit -m "feat: add ExtractedImage model + ParsedDocument.images + table crop field"
```

---

### Task 2: DB migration — `image_store` table + `table_store.image_storage_path`

**Files:**
- Create: `backend/app/db/migrations/003_image_store.sql`
- Create: `backend/scripts/apply_migration.py`
- Test: `backend/tests/test_migration_image_store.py`

**Interfaces:**
- Produces: table `multi_store_rag_working.image_store` with columns
  `id, document_id, image_index, page_number, bbox, storage_path, storage_bucket, mime_type, width, height, caption, ocr_text, embedding vector(1024), image_metadata, created_at`;
  and `multi_store_rag_working.table_store.image_storage_path TEXT`.

- [ ] **Step 1: Create the migration SQL**

```sql
-- backend/app/db/migrations/003_image_store.sql
-- Phase 1: image_store for extracted figures + table crop link. Additive only.
SET search_path TO multi_store_rag_working, public;

CREATE TABLE IF NOT EXISTS multi_store_rag_working.image_store (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id    UUID NOT NULL REFERENCES multi_store_rag_working.document_registry(id) ON DELETE CASCADE,
    image_index    INT  NOT NULL,
    page_number    INT,
    bbox           JSONB,
    storage_path   TEXT NOT NULL,
    storage_bucket TEXT NOT NULL,
    mime_type      TEXT DEFAULT 'image/png',
    width          INT,
    height         INT,
    caption        TEXT,
    ocr_text       TEXT,
    embedding      vector(1024),
    image_metadata JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_image_store_embedding
    ON multi_store_rag_working.image_store
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 128);

CREATE INDEX IF NOT EXISTS idx_image_store_document_id
    ON multi_store_rag_working.image_store (document_id);

ALTER TABLE multi_store_rag_working.table_store
    ADD COLUMN IF NOT EXISTS image_storage_path TEXT;
```

- [ ] **Step 2: Create the migration runner**

```python
# backend/scripts/apply_migration.py
"""Apply a .sql migration file through the configured psycopg2 connection."""
import sys
from pathlib import Path

from app.db.connection import get_db


def apply(sql_path: str) -> None:
    sql = Path(sql_path).read_text(encoding="utf-8")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"Applied migration: {sql_path}")


if __name__ == "__main__":
    apply(sys.argv[1])
```

- [ ] **Step 3: Write the failing test**

```python
# backend/tests/test_migration_image_store.py
import pytest
from app.db.connection import get_db

REQUIRED = {
    "id", "document_id", "image_index", "page_number", "bbox",
    "storage_path", "storage_bucket", "mime_type", "width", "height",
    "caption", "ocr_text", "embedding", "image_metadata", "created_at",
}


@pytest.mark.slow
def test_image_store_columns_exist():
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'multi_store_rag_working' AND table_name = 'image_store'"""
        )
        cols = {r[0] for r in cur.fetchall()}
    assert REQUIRED <= cols, f"missing: {REQUIRED - cols}"


@pytest.mark.slow
def test_table_store_has_image_storage_path():
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'multi_store_rag_working'
                 AND table_name = 'table_store' AND column_name = 'image_storage_path'"""
        )
        assert cur.fetchone() is not None
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_migration_image_store.py -v -m slow`
Expected: FAIL — `image_store` columns missing (table does not exist yet).

- [ ] **Step 5: Apply the migration**

Run: `python scripts/apply_migration.py app/db/migrations/003_image_store.sql`
Expected: `Applied migration: app/db/migrations/003_image_store.sql`

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_migration_image_store.py -v -m slow`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add backend/app/db/migrations/003_image_store.sql backend/scripts/apply_migration.py backend/tests/test_migration_image_store.py
git commit -m "feat: add image_store table migration + table_store.image_storage_path"
```

---

### Task 3: Docling image extraction in `document_parser.py`

**Files:**
- Modify: `backend/app/services/document_parser.py`
- Test: `backend/tests/test_parser_images.py`

**Interfaces:**
- Consumes: `ExtractedImage` (Task 1).
- Produces:
  - `_pil_to_png_bytes(pil_image) -> tuple[bytes, int, int]` — returns `(png_bytes, width, height)`.
  - `_parse_with_docling` now populates `ParsedDocument.images` and `ExtractedTable.image_png_bytes`.

- [ ] **Step 1: Write the failing test (helper is pure, no Docling needed)**

```python
# backend/tests/test_parser_images.py
import io
from PIL import Image
from app.services.document_parser import _pil_to_png_bytes


def test_pil_to_png_bytes_roundtrip():
    src = Image.new("RGB", (40, 20), color=(10, 20, 30))
    png_bytes, w, h = _pil_to_png_bytes(src)
    assert w == 40 and h == 20
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    # bytes are decodable back to the same size
    back = Image.open(io.BytesIO(png_bytes))
    assert back.size == (40, 20)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parser_images.py -v`
Expected: FAIL — `ImportError: cannot import name '_pil_to_png_bytes'`

- [ ] **Step 3: Add the helper + enable image extraction**

In `backend/app/services/document_parser.py`, add the import at top (after line 7):

```python
import io
```

Add the helper in the Helpers section (after `_simple_token_count`):

```python
def _pil_to_png_bytes(pil_image) -> tuple[bytes, int, int]:
    """Encode a PIL image to PNG bytes; return (bytes, width, height)."""
    buf = io.BytesIO()
    rgb = pil_image.convert("RGB") if pil_image.mode not in ("RGB", "RGBA") else pil_image
    rgb.save(buf, format="PNG")
    return buf.getvalue(), pil_image.width, pil_image.height
```

Update `_parse_with_docling` pipeline options (replace lines 51-55):

```python
    pipeline_options = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        artifacts_path=artifacts_dir,
        generate_picture_images=True,
        generate_table_images=True,
        images_scale=2.0,
    )
```

Add an `images` accumulator next to `image_pages` (after line 67):

```python
    image_pages: set[int] = set()
    images: list = []
```

Replace the `PictureItem` branch (lines 110-114) to extract the cropped image:

```python
        elif item_type == "PictureItem":
            pg = _get_page(item)
            if pg not in image_pages:
                image_pages.add(pg)
            try:
                from app.models.document import ExtractedImage
                pil = item.get_image(doc)
                if pil is not None:
                    png, w, h = _pil_to_png_bytes(pil)
                    images.append(ExtractedImage(
                        image_index=len(images),
                        page_number=pg,
                        bbox=_get_bbox(item),
                        png_bytes=png,
                        width=w,
                        height=h,
                    ))
            except Exception as img_exc:
                logger.warning("Picture extraction failed on page %s: %s", pg, img_exc)
```

In the table loop (after building each `ExtractedTable`, replacing lines 118-129), attach a crop:

```python
    tables: list[ExtractedTable] = []
    for idx, table in enumerate(doc.tables):
        headers, rows = _parse_table_data(table)
        table_png = None
        try:
            tpil = table.get_image(doc)
            if tpil is not None:
                table_png, _, _ = _pil_to_png_bytes(tpil)
        except Exception:
            table_png = None
        tables.append(ExtractedTable(
            table_index=idx,
            page_number=_get_table_page(table),
            headers=headers,
            rows=rows,
            caption=getattr(table, "caption", None),
            bbox=_get_table_bbox(table),
            raw_text=_table_to_text(headers, rows),
            markdown_text=_table_to_markdown(headers, rows),
            image_png_bytes=table_png,
        ))
```

Pass `images` into the returned `ParsedDocument` (in the `return ParsedDocument(...)` call, add before the closing paren):

```python
        image_page_numbers=sorted(image_pages),
        images=images,
        metadata=metadata,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_parser_images.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Add a documented slow integration test**

```python
# append to backend/tests/test_parser_images.py
import os
import pytest


@pytest.mark.slow
@pytest.mark.skipif(not os.path.exists("tests/fixtures/with_figure.pdf"),
                    reason="needs a sample PDF with an embedded figure")
def test_docling_extracts_images():
    from app.services.document_parser import parse_document
    pd = parse_document("tests/fixtures/with_figure.pdf", "test-doc")
    assert pd.has_images
    assert len(pd.images) >= 1
    first = pd.images[0]
    assert first.png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert first.page_number >= 1
```

- [ ] **Step 6: Run fast suite to confirm no regression**

Run: `pytest tests/test_parser_images.py -v -m "not slow"`
Expected: PASS (1 passed, 1 skipped/deselected)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/document_parser.py backend/tests/test_parser_images.py
git commit -m "feat: extract figure + table images via Docling generate_picture_images"
```

---

### Task 4: Supabase signed URLs + image upload

**Files:**
- Modify: `backend/app/services/supabase_storage.py`
- Test: `backend/tests/test_supabase_signed_url.py`

**Interfaces:**
- Consumes: existing `upload_file(bucket, path, content, content_type)`.
- Produces: `create_signed_url(bucket:str, path:str, expires_in:int=3600) -> str` — returns the signed URL string, tolerating storage3 response key variants (`signedURL` / `signedUrl` / `signed_url`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_supabase_signed_url.py
from unittest.mock import MagicMock, patch
import app.services.supabase_storage as sb


def _patched_client(signed_response):
    client = MagicMock()
    bucket = MagicMock()
    bucket.create_signed_url.return_value = signed_response
    client.storage.from_.return_value = bucket
    return client


def test_create_signed_url_handles_camel_key():
    client = _patched_client({"signedURL": "https://x/y?token=abc"})
    with patch.object(sb, "_client", return_value=client):
        url = sb.create_signed_url("rag-documents", "images/d/0.png", expires_in=600)
    assert url == "https://x/y?token=abc"
    client.storage.from_.assert_called_with("rag-documents")


def test_create_signed_url_handles_snake_key():
    client = _patched_client({"signed_url": "https://x/snake"})
    with patch.object(sb, "_client", return_value=client):
        url = sb.create_signed_url("rag-documents", "images/d/1.png")
    assert url == "https://x/snake"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_supabase_signed_url.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'create_signed_url'`

- [ ] **Step 3: Implement `create_signed_url`**

Append to `backend/app/services/supabase_storage.py`:

```python
def create_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Mint a time-limited signed URL for a private-bucket object."""
    resp = _client().storage.from_(bucket).create_signed_url(path, expires_in)
    if isinstance(resp, dict):
        for key in ("signedURL", "signedUrl", "signed_url", "url"):
            if resp.get(key):
                return resp[key]
    raise RuntimeError(f"Unexpected signed-url response for {bucket}/{path}: {resp!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_supabase_signed_url.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/supabase_storage.py backend/tests/test_supabase_signed_url.py
git commit -m "feat: add create_signed_url helper for private-bucket image assets"
```

---

### Task 5: Per-image Gemma vision — `describe_image`

**Files:**
- Modify: `backend/app/services/image_analysis_service.py`
- Test: `backend/tests/test_describe_image.py`

**Interfaces:**
- Produces: `describe_image(png_bytes: bytes) -> dict` returning `{"caption": str, "ocr_text": str}`.
  Returns `{"caption": "", "ocr_text": ""}` if `GEMMA4_BASE_URL` unset or on parse failure.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_describe_image.py
import json
from unittest.mock import MagicMock, patch
import app.services.image_analysis_service as ias


def _gemma_response(content: str):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


def test_describe_image_parses_json_caption_and_ocr():
    payload = json.dumps({"caption": "Bar chart of revenue", "ocr_text": "Q1 100 Q2 200"})
    client = MagicMock()
    client.__enter__.return_value.post.return_value = _gemma_response(payload)
    with patch.object(ias, "httpx") as httpx_mod:
        httpx_mod.Client.return_value = client
        with patch("app.config.settings") as s:
            s.GEMMA4_BASE_URL = "http://gemma/v1"
            s.GEMMA4_API_KEY = ""
            s.GEMMA4_MODEL_NAME = "gemma-4-27b-it"
            s.GEMMA4_TIMEOUT_SECONDS = 30
            out = ias.describe_image(b"\x89PNG-fake")
    assert out["caption"] == "Bar chart of revenue"
    assert out["ocr_text"] == "Q1 100 Q2 200"


def test_describe_image_no_endpoint_returns_empty():
    with patch("app.config.settings") as s:
        s.GEMMA4_BASE_URL = ""
        out = ias.describe_image(b"\x89PNG-fake")
    assert out == {"caption": "", "ocr_text": ""}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_describe_image.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'describe_image'`

- [ ] **Step 3: Implement `describe_image`**

Add to `backend/app/services/image_analysis_service.py` (keep existing functions; add `import json` at top):

```python
def describe_image(png_bytes: bytes) -> dict:
    """Caption + OCR a single cropped image via Gemma-4 vision. Non-fatal."""
    from app.config import settings

    empty = {"caption": "", "ocr_text": ""}
    if not settings.GEMMA4_BASE_URL:
        logger.warning("GEMMA4_BASE_URL not set — skipping image description")
        return empty

    b64 = base64.b64encode(png_bytes).decode()
    headers = {"Content-Type": "application/json"}
    if settings.GEMMA4_API_KEY:
        headers["Authorization"] = f"Bearer {settings.GEMMA4_API_KEY}"

    prompt = (
        "You are analyzing one figure/chart/image cropped from a business or "
        "technical document. Respond with ONLY a JSON object: "
        '{"caption": "...", "ocr_text": "..."}. '
        "caption = a precise factual description including chart type, axes, "
        "legend, series, KPIs and any exact numbers/trends. "
        "ocr_text = all text visible in the image, verbatim (empty string if none)."
    )
    payload = {
        "model": settings.GEMMA4_MODEL_NAME,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 1024,
        "temperature": 0.1,
    }

    base = settings.GEMMA4_BASE_URL.rstrip("/")
    try:
        with httpx.Client(timeout=settings.GEMMA4_TIMEOUT_SECONDS) as client:
            resp = client.post(f"{base}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```")[1].lstrip("json").strip()
        data = json.loads(content)
        return {"caption": str(data.get("caption", "")), "ocr_text": str(data.get("ocr_text", ""))}
    except Exception as exc:
        logger.warning("describe_image failed: %s", exc)
        return empty
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_describe_image.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/image_analysis_service.py backend/tests/test_describe_image.py
git commit -m "feat: per-image Gemma vision describe_image (caption + ocr)"
```

---

### Task 6: `image_store` storage + repository

**Files:**
- Create: `backend/app/db/repositories/image_store.py`
- Modify: `backend/app/services/storage_service.py`
- Test: `backend/tests/test_store_images.py`

**Interfaces:**
- Consumes: `image_store` table (Task 2), 1024-d embeddings (np.ndarray).
- Produces:
  - `storage_service._image_rows(document_id:str, records:list[dict], embeddings:np.ndarray) -> list[tuple]` — pure row builder.
  - `storage_service.store_images(document_id:str, records:list[dict], embeddings:np.ndarray) -> None` — bulk insert.
  - `record` dict keys: `image_index, page_number, bbox(dict|None), storage_path, storage_bucket, mime_type, width, height, caption, ocr_text, image_metadata(dict)`.
  - `_clear_existing_chunks` also clears `image_store`.

- [ ] **Step 1: Write the failing test (pure row builder)**

```python
# backend/tests/test_store_images.py
import numpy as np
from app.services.storage_service import _image_rows


def test_image_rows_shapes_and_serialization():
    records = [{
        "image_index": 0, "page_number": 3,
        "bbox": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
        "storage_path": "images/doc1/0.png", "storage_bucket": "rag-documents",
        "mime_type": "image/png", "width": 100, "height": 50,
        "caption": "A chart", "ocr_text": "Q1 10", "image_metadata": {"k": "v"},
    }]
    embs = np.zeros((1, 1024), dtype="float32")
    rows = _image_rows("doc1", records, embs)
    assert len(rows) == 1
    row = rows[0]
    # (document_id, image_index, page_number, bbox_json, storage_path, storage_bucket,
    #  mime_type, width, height, caption, ocr_text, embedding_list, metadata_json)
    assert row[0] == "doc1"
    assert row[1] == 0
    assert row[2] == 3
    assert '"x1": 1' in row[3]
    assert row[4] == "images/doc1/0.png"
    assert row[5] == "rag-documents"
    assert row[6] == "image/png"
    assert row[7] == 100 and row[8] == 50
    assert row[9] == "A chart" and row[10] == "Q1 10"
    assert len(row[11]) == 1024
    assert '"k": "v"' in row[12]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store_images.py -v`
Expected: FAIL — `ImportError: cannot import name '_image_rows'`

- [ ] **Step 3: Implement the repository**

```python
# backend/app/db/repositories/image_store.py
"""image_store repository — bulk insert of extracted images with embeddings."""
import psycopg2.extras

from app.db.connection import get_db

_INSERT_SQL = """
    INSERT INTO multi_store_rag_working.image_store
        (document_id, image_index, page_number, bbox, storage_path, storage_bucket,
         mime_type, width, height, caption, ocr_text, embedding, image_metadata)
    VALUES %s
"""
_TEMPLATE = "(%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb)"


def insert_images(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with get_db() as conn:
        psycopg2.extras.execute_values(
            conn.cursor(), _INSERT_SQL, rows, template=_TEMPLATE, page_size=200,
        )
    return len(rows)
```

- [ ] **Step 4: Implement `_image_rows` + `store_images` in `storage_service.py`**

Add to `backend/app/services/storage_service.py`:

```python
def _image_rows(document_id: str, records: list[dict], embeddings) -> list[tuple]:
    rows = []
    for rec, emb in zip(records, embeddings):
        bbox_json = json.dumps(rec["bbox"]) if rec.get("bbox") else None
        rows.append((
            document_id,
            rec["image_index"],
            rec.get("page_number"),
            bbox_json,
            rec["storage_path"],
            rec["storage_bucket"],
            rec.get("mime_type", "image/png"),
            rec.get("width"),
            rec.get("height"),
            rec.get("caption"),
            rec.get("ocr_text"),
            emb.tolist(),
            json.dumps(rec.get("image_metadata", {})),
        ))
    return rows


def store_images(document_id: str, records: list[dict], embeddings) -> None:
    if not records:
        return
    from app.db.repositories.image_store import insert_images
    n = insert_images(_image_rows(document_id, records, embeddings))
    logger.info("Stored %d images for document %s", n, document_id)
```

Update `_clear_existing_chunks` (line 29) to include the new table:

```python
            for table in ("vector_store", "table_store", "clause_store", "document_store", "image_store"):
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_store_images.py -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Add a slow DB round-trip test**

```python
# append to backend/tests/test_store_images.py
import os, uuid, pytest
from app.db.connection import get_db


@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("RUN_DB_TESTS"), reason="needs live DB + a real document_id")
def test_insert_and_query_image(monkeypatch):
    doc_id = os.environ["RUN_DB_TESTS"]  # set to an existing document_registry.id
    from app.services.storage_service import store_images
    rec = [{
        "image_index": 999, "page_number": 1, "bbox": None,
        "storage_path": f"images/{doc_id}/test.png", "storage_bucket": "rag-documents",
        "mime_type": "image/png", "width": 10, "height": 10,
        "caption": "pytest image", "ocr_text": "", "image_metadata": {},
    }]
    embs = np.random.rand(1, 1024).astype("float32")
    store_images(doc_id, rec, embs)
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT caption FROM multi_store_rag_working.image_store WHERE document_id=%s AND image_index=999",
            (doc_id,),
        )
        assert cur.fetchone()[0] == "pytest image"
        cur.execute("DELETE FROM multi_store_rag_working.image_store WHERE document_id=%s AND image_index=999", (doc_id,))
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/db/repositories/image_store.py backend/app/services/storage_service.py backend/tests/test_store_images.py
git commit -m "feat: image_store repository + storage_service.store_images"
```

---

### Task 7: Retrieval — query `image_store` + extend `RetrievedChunk`

**Files:**
- Modify: `backend/app/services/retriever_service.py`
- Test: `backend/tests/test_retriever_images.py`

**Interfaces:**
- Consumes: `image_store` table (Task 2), `embed_query`.
- Produces:
  - `RetrievedChunk` gains `image_storage_path: Optional[str] = None`, `caption: Optional[str] = None`, `ocr_text: Optional[str] = None`.
  - `_rows_to_image_chunks(rows: list) -> list[RetrievedChunk]` — pure mapper; `store_type='image'`, `text = caption + '\n' + ocr_text`.
  - `_query_image_store(conn, embedding, document_types, document_id, top_k)`.
  - `retrieve()` adds `search_image` (True when no filter, or any of `policy`/`financial`/`research` requested).

- [ ] **Step 1: Write the failing test (pure mapper)**

```python
# backend/tests/test_retriever_images.py
from app.services.retriever_service import _rows_to_image_chunks


def test_rows_to_image_chunks_maps_fields():
    # row order: id, document_id, caption, ocr_text, page_number, storage_path, distance, filename, doctype
    rows = [("img-1", "doc-1", "Revenue chart", "Q1 100", 4,
             "images/doc-1/0.png", 0.12, "report.pdf", "financial")]
    chunks = _rows_to_image_chunks(rows)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.store_type == "image"
    assert c.chunk_id == "img-1"
    assert c.document_id == "doc-1"
    assert c.caption == "Revenue chart"
    assert c.ocr_text == "Q1 100"
    assert "Revenue chart" in c.text and "Q1 100" in c.text
    assert c.page_number == 4
    assert c.image_storage_path == "images/doc-1/0.png"
    assert abs(c.distance - 0.12) < 1e-9
    assert c.document_filename == "report.pdf"
    assert c.document_type == "financial"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_retriever_images.py -v`
Expected: FAIL — `ImportError: cannot import name '_rows_to_image_chunks'`

- [ ] **Step 3: Extend `RetrievedChunk` + add mapper + query + wiring**

In `backend/app/services/retriever_service.py`, add fields to `RetrievedChunk` (after `table_markdown`, line 31):

```python
    table_markdown: Optional[str] = None
    image_storage_path: Optional[str] = None
    caption: Optional[str] = None
    ocr_text: Optional[str] = None
```

Add the pure mapper and store query (after `_query_table_store`):

```python
def _rows_to_image_chunks(rows: list) -> list:
    out = []
    for r in rows:
        caption = r[2] or ""
        ocr = r[3] or ""
        out.append(RetrievedChunk(
            chunk_id=r[0], document_id=r[1],
            text=(caption + ("\n" + ocr if ocr else "")).strip() or "(image)",
            caption=caption or None, ocr_text=ocr or None,
            page_number=r[4], image_storage_path=r[5],
            distance=float(r[6]),
            document_filename=r[7], document_type=r[8],
            store_type="image",
        ))
    return out


def _query_image_store(conn, embedding, document_types, document_id, top_k: int) -> list:
    type_sql, type_params = _type_filter(document_types)
    doc_sql, doc_params = _doc_filter(document_id, "img")
    emb = _emb_str(embedding)
    sql = f"""
        SELECT
            img.id::text, img.document_id::text, img.caption, img.ocr_text,
            img.page_number, img.storage_path,
            (img.embedding <=> %s::vector) AS distance,
            dr.original_filename, dr.document_type
        FROM multi_store_rag_working.image_store img
        JOIN multi_store_rag_working.document_registry dr ON dr.id = img.document_id
        WHERE dr.status = 'completed' AND img.embedding IS NOT NULL
        {type_sql} {doc_sql}
        ORDER BY img.embedding <=> %s::vector
        LIMIT %s
    """
    params = [emb] + type_params + doc_params + [emb, top_k]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return _rows_to_image_chunks(rows)
```

Wire into `retrieve()`: add the flag (after line 51) and the call (after line 68):

```python
    search_table = True
    search_image = True

    if document_types:
        search_vector = any(dt in document_types for dt in ["policy", "entity", "financial"])
        search_clause = "legal" in document_types
        search_research = "research" in document_types
        search_table = "financial" in document_types
        search_image = any(dt in document_types for dt in ["policy", "financial", "research"])
```

```python
        if search_table:
            results.extend(_query_table_store(conn, query_embedding, document_types, document_id, top_k_per_store))
        if search_image:
            results.extend(_query_image_store(conn, query_embedding, document_types, document_id, top_k_per_store))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_retriever_images.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retriever_service.py backend/tests/test_retriever_images.py
git commit -m "feat: retrieve from image_store; add image fields to RetrievedChunk"
```

---

### Task 8: Query API — image citations with signed URLs

**Files:**
- Modify: `backend/app/api/routes/query.py`
- Test: `backend/tests/test_query_citation_image.py`

**Interfaces:**
- Consumes: `RetrievedChunk.image_storage_path/caption/ocr_text` (Task 7), `create_signed_url` (Task 4).
- Produces:
  - `CitationItem` gains `image_url: Optional[str] = None`, `caption: Optional[str] = None`, `ocr_text: Optional[str] = None`.
  - `_citation_from_chunk(chunk, bucket) -> CitationItem` — mints `image_url` from `image_storage_path` (best-effort; None on failure).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_query_citation_image.py
from unittest.mock import patch
from app.services.retriever_service import RetrievedChunk
import app.api.routes.query as q


def _img_chunk():
    return RetrievedChunk(
        chunk_id="i1", document_id="d1", text="Revenue chart\nQ1 100",
        store_type="image", distance=0.1, document_filename="r.pdf",
        document_type="financial", relevance_score=0.9, page_number=4,
        image_storage_path="images/d1/0.png", caption="Revenue chart", ocr_text="Q1 100",
    )


def test_citation_from_image_chunk_has_signed_url():
    with patch.object(q, "create_signed_url", return_value="https://signed/url") as m:
        item = q._citation_from_chunk(_img_chunk(), bucket="rag-documents")
    assert item.store_type == "image"
    assert item.image_url == "https://signed/url"
    assert item.caption == "Revenue chart"
    assert item.ocr_text == "Q1 100"
    m.assert_called_once_with("rag-documents", "images/d1/0.png")


def test_citation_signed_url_failure_is_non_fatal():
    with patch.object(q, "create_signed_url", side_effect=RuntimeError("boom")):
        item = q._citation_from_chunk(_img_chunk(), bucket="rag-documents")
    assert item.image_url is None
    assert item.caption == "Revenue chart"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_query_citation_image.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_citation_from_chunk'`

- [ ] **Step 3: Implement**

In `backend/app/api/routes/query.py`, add imports near the top:

```python
from app.config import settings
from app.services.supabase_storage import create_signed_url
```

Add fields to `CitationItem` (after `table_markdown`, line 36):

```python
    table_markdown: Optional[str] = None
    image_url: Optional[str] = None
    caption: Optional[str] = None
    ocr_text: Optional[str] = None
```

Add the builder above the route handler:

```python
def _citation_from_chunk(c, bucket: str) -> CitationItem:
    image_url = None
    if getattr(c, "image_storage_path", None):
        try:
            image_url = create_signed_url(bucket, c.image_storage_path)
        except Exception as exc:
            logger.warning("Signed URL mint failed for %s: %s", c.image_storage_path, exc)
    return CitationItem(
        document_id=c.document_id,
        filename=c.document_filename,
        chunk_text=c.text,
        store_type=c.store_type,
        relevance_score=round(c.relevance_score, 4),
        page_number=c.page_number,
        section_title=c.section_title,
        clause_type=c.clause_type,
        risk_level=c.risk_level,
        chunk_type=c.chunk_type,
        source_doi=c.source_doi,
        table_markdown=c.table_markdown,
        image_url=image_url,
        caption=getattr(c, "caption", None),
        ocr_text=getattr(c, "ocr_text", None),
    )
```

Replace the inline `citations = [CitationItem(...) for c in final_chunks]` list (lines 118-134) with:

```python
    bucket = settings.SUPABASE_STORAGE_BUCKET
    citations = [_citation_from_chunk(c, bucket) for c in final_chunks]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_query_citation_image.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/query.py backend/tests/test_query_citation_image.py
git commit -m "feat: image citations with on-demand signed URLs in query response"
```

---

### Task 9: Orchestrator — images stage (extract → caption → upload → embed → store)

**Files:**
- Modify: `backend/app/services/ingestion_orchestrator.py`
- Test: `backend/tests/test_orchestrator_image_records.py`

**Interfaces:**
- Consumes: `parse_document` (Task 3), `describe_image` (Task 5), `upload_file`/`create_signed_url` (Task 4), `embed_passages`, `store_images` (Task 6).
- Produces:
  - `_build_image_records(parsed_doc, document_id, bucket) -> tuple[list[dict], list[str]]` — pure-ish helper (uploads + captions per image) returning `(records, embed_texts)`. Each upload/caption failure skips that image.
  - New "images" stage in `ingest_document` replacing the old whole-page `analyze_images` → `vector_store` path.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_orchestrator_image_records.py
from unittest.mock import patch
from app.models.document import ParsedDocument, ExtractedImage, BoundingBox
import app.services.ingestion_orchestrator as orch


def _doc_with_one_image():
    return ParsedDocument(
        doc_id="d1", filename="f.pdf", raw_text="", text_blocks=[], tables=[],
        page_count=1, word_count=0, has_tables=False, has_images=True,
        images=[ExtractedImage(image_index=0, page_number=2,
                               bbox=BoundingBox(1, 2, 3, 4),
                               png_bytes=b"\x89PNG", width=10, height=8)],
    )


def test_build_image_records_uploads_captions_and_collects():
    with patch.object(orch, "upload_file", return_value="images/d1/0.png") as up, \
         patch("app.services.image_analysis_service.describe_image",
               return_value={"caption": "chart", "ocr_text": "x"}):
        records, texts = orch._build_image_records(_doc_with_one_image(), "d1", "rag-documents")
    assert len(records) == 1
    rec = records[0]
    assert rec["storage_path"] == "images/d1/0.png"
    assert rec["storage_bucket"] == "rag-documents"
    assert rec["page_number"] == 2
    assert rec["caption"] == "chart" and rec["ocr_text"] == "x"
    assert rec["bbox"] == {"x1": 1, "y1": 2, "x2": 3, "y2": 4}
    assert texts == ["chart\nx"]
    up.assert_called_once()


def test_build_image_records_skips_failed_upload():
    with patch.object(orch, "upload_file", side_effect=RuntimeError("bucket down")), \
         patch("app.services.image_analysis_service.describe_image",
               return_value={"caption": "c", "ocr_text": ""}):
        records, texts = orch._build_image_records(_doc_with_one_image(), "d1", "rag-documents")
    assert records == [] and texts == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator_image_records.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_build_image_records'`

- [ ] **Step 3: Add `_build_image_records` + imports**

In `backend/app/services/ingestion_orchestrator.py`, add at module level (top imports):

```python
from app.services.supabase_storage import upload_file
```

Add the helper near `_get_current_stage`:

```python
def _build_image_records(parsed_doc, document_id: str, bucket: str) -> tuple[list, list]:
    """Caption + upload each extracted image. Returns (records, embed_texts).
    Failures for an individual image are skipped (non-fatal)."""
    from app.services.image_analysis_service import describe_image

    records: list = []
    embed_texts: list = []
    for img in parsed_doc.images:
        try:
            desc = describe_image(img.png_bytes)
        except Exception as exc:
            logger.warning("[%s] describe_image failed (img %d): %s", document_id, img.image_index, exc)
            desc = {"caption": "", "ocr_text": ""}
        path = f"images/{document_id}/{img.image_index}.png"
        try:
            upload_file(bucket, path, img.png_bytes, "image/png")
        except Exception as exc:
            logger.warning("[%s] image upload failed (img %d): %s", document_id, img.image_index, exc)
            continue
        bbox = None
        if img.bbox:
            bbox = {"x1": img.bbox.x1, "y1": img.bbox.y1, "x2": img.bbox.x2, "y2": img.bbox.y2}
        caption = desc.get("caption", "") or ""
        ocr = desc.get("ocr_text", "") or ""
        records.append({
            "image_index": img.image_index, "page_number": img.page_number, "bbox": bbox,
            "storage_path": path, "storage_bucket": bucket, "mime_type": "image/png",
            "width": img.width, "height": img.height, "caption": caption, "ocr_text": ocr,
            "image_metadata": {},
        })
        embed_texts.append((caption + ("\n" + ocr if ocr else "")).strip() or f"image page {img.page_number}")
    return records, embed_texts
```

- [ ] **Step 4: Replace the old image-analysis stage**

Replace the whole "Stage 1b: IMAGE ANALYSIS" block (lines 84-99) with the new images stage:

```python
        # ── Stage 1b: IMAGES (extract → caption → upload → embed → store) ──
        if parsed_doc.images:
            job_repo.update_job(job_id, "images", progress=0)
            t0 = time.monotonic()
            try:
                from app.config import settings as _settings
                from app.services.embedding_service import embed_passages as _embed
                from app.services.storage_service import store_images
                import numpy as _np

                bucket = _settings.SUPABASE_STORAGE_BUCKET
                records, embed_texts = _build_image_records(parsed_doc, document_id, bucket)
                if records:
                    img_embs = _embed(embed_texts)
                    store_images(document_id, records, img_embs)
                logger.info("[%s] Stored %d images", document_id, len(records))
            except Exception as img_exc:
                logger.warning("[%s] Image stage failed (non-fatal): %s", document_id, img_exc)
            job_repo.update_job(
                job_id, "images", progress=100,
                stage_timing=("images", time.monotonic() - t0),
            )
```

- [ ] **Step 5: Run test + fast suite**

Run: `pytest tests/test_orchestrator_image_records.py -v`
Expected: PASS (2 passed)
Run: `pytest tests/ -v -m "not slow"`
Expected: PASS (no regressions)

- [ ] **Step 6: Manual integration verification (documented)**

```
1. Start redis + API + celery (venv). Upload a PDF containing charts via the UI.
2. Watch celery log: "Stored N images" appears.
3. Supabase Storage bucket has images/{document_id}/*.png.
4. SELECT count(*) FROM multi_store_rag_working.image_store WHERE document_id='<id>';  → N
5. Query "describe the revenue chart" → response citations include a store_type='image'
   item with a working image_url (signed) + caption.
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/ingestion_orchestrator.py backend/tests/test_orchestrator_image_records.py
git commit -m "feat: orchestrator images stage — extract, caption, upload, embed, store"
```

---

## Self-Review

**Spec coverage:**
- Image extraction (real crops) → Task 3. ✓
- Caption + OCR via Gemma vision → Task 5. ✓
- Bucket upload + signed URLs (private) → Task 4 + Task 9. ✓
- `image_store` table + BGE embedding same space → Task 2 + Task 6 + Task 9. ✓
- Table crops linked → Task 1 (`image_png_bytes`) + Task 3 (extract). *Note:* persisting `table_store.image_storage_path` end-to-end is scaffolded (column added Task 2, crop captured Task 3) but the upload+write wiring for tables is deferred — see Gap below.
- Provenance (doc_id + page + bbox) → Task 2 schema + Task 6 rows. ✓
- Retrieval wiring → Task 7. ✓
- Citation deep-data (image_url/caption) → Task 8. ✓
- Remove whole-page vision noise → Task 9 (replaces Stage 1b). ✓

**Gap found + resolution:** The spec lists "upload table crops → `table_store.image_storage_path`". Tasks add the column and capture `image_png_bytes`, but the orchestrator only uploads *figure* images. To keep tasks bite-sized and the table path is lower-value than figures, table-crop upload is explicitly **deferred to a follow-up task** (Task 10 below) rather than left as a silent gap.

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓
**Type consistency:** `_image_rows` tuple order matches `_INSERT_SQL` column order and `_TEMPLATE` casts; `_rows_to_image_chunks` row indices match `_query_image_store` SELECT order; `record` dict keys consistent across Tasks 6 and 9. ✓

---

### Task 10 (follow-up): Persist table crops to bucket + `table_store.image_storage_path`

**Files:**
- Modify: `backend/app/services/storage_service.py` (`_store_tables`)
- Modify: `backend/app/services/ingestion_orchestrator.py` (upload table crops, pass paths)
- Test: `backend/tests/test_store_tables_image_path.py`

**Interfaces:**
- Consumes: `ExtractedTable.image_png_bytes` (Task 1), `upload_file` (Task 4).
- Produces: `_store_tables` writes `image_storage_path`; orchestrator uploads `tables/{document_id}/{table_index}.png` before storing.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_store_tables_image_path.py
from app.services.storage_service import _table_image_path


def test_table_image_path_format():
    assert _table_image_path("doc1", 3) == "tables/doc1/3.png"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_store_tables_image_path.py -v`
Expected: FAIL — `ImportError: cannot import name '_table_image_path'`

- [ ] **Step 3: Implement**

Add to `storage_service.py`:

```python
def _table_image_path(document_id: str, table_index: int) -> str:
    return f"tables/{document_id}/{table_index}.png"
```

Extend `_store_tables` to accept and write paths. Change its signature and INSERT:

```python
def _store_tables(document_id: str, parsed_doc: ParsedDocument, image_paths: dict | None = None) -> None:
    image_paths = image_paths or {}
    rows = []
    for table in parsed_doc.tables:
        ...  # existing row fields unchanged, then append image path as a new trailing column
        rows.append((
            document_id, table.table_index, table.caption, table.page_number, bbox_json,
            table.raw_text, table.markdown_text, json_data, csv_data,
            len(table.rows), len(table.headers), has_numeric, has_currency, has_percentages,
            image_paths.get(table.table_index),
        ))
    # add image_storage_path to the column list + one %s to the template
```

Update the INSERT column list to end with `..., has_percentages, image_storage_path)` and template to end with `..., %s, %s)`.

In the orchestrator's storing stage, before `store_chunks`, upload crops and pass the map (financial docs):

```python
        table_image_paths = {}
        for t in parsed_doc.tables:
            if getattr(t, "image_png_bytes", None):
                p = f"tables/{document_id}/{t.table_index}.png"
                try:
                    upload_file(_settings.SUPABASE_STORAGE_BUCKET, p, t.image_png_bytes, "image/png")
                    table_image_paths[t.table_index] = p
                except Exception as exc:
                    logger.warning("[%s] table crop upload failed (%d): %s", document_id, t.table_index, exc)
```

Then call `_store_tables(document_id, parsed_doc, table_image_paths)` (route this through `store_chunks` by threading the dict, or call `_store_tables` directly in the financial branch).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_store_tables_image_path.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/storage_service.py backend/app/services/ingestion_orchestrator.py backend/tests/test_store_tables_image_path.py
git commit -m "feat: upload table crops to bucket + store table_store.image_storage_path"
```

---

## Execution Order & Parallelism

- **Independent (can run in parallel):** Tasks 1, 2, 4, 5. (Models, migration, signed-URL, vision — no shared edits.)
- **After Task 1:** Task 3 (parser).
- **After Tasks 2 + 6 deps:** Task 6 (needs migration + models).
- **After Task 6:** Task 7 (retriever), then Task 8 (query).
- **Integrate last:** Task 9 (orchestrator — depends on 3,4,5,6). Then Task 10.

Recommended fan-out wave 1: {1, 2, 4, 5}. Wave 2: {3, 6}. Wave 3: {7, 8}. Wave 4: {9}. Wave 5: {10}.
