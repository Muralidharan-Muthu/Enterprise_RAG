"""parse_staging_service — serialize / deserialize ParsedDocument to Supabase Storage.

Implements Feature 1.6: durable staging of the parsed output between the two
chained Celery tasks (parse_document_task → chunk_embed_store_task).

Serialization strategy
----------------------
ParsedDocument contains two bytes fields that must NOT be base64-encoded into
the main JSON blob (multi-MB per file, no benefit):

    ExtractedImage.png_bytes       → staging/<doc_id>/img_<i>.png
    ExtractedTable.image_png_bytes → staging/<doc_id>/table_<i>.png

The main JSON blob (staging/<doc_id>/parsed.json) has those fields set to null.
load_parsed() re-downloads each PNG blob and re-attaches it so all downstream
image/table-crop stages work unchanged.

All paths are deterministic → re-saving the same document_id is idempotent
(supabase_storage.upload_file uses upsert=true).
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Bucket resolution ─────────────────────────────────────────────────────────

def _staging_bucket() -> str:
    """Return the bucket to use for parse staging blobs.

    When PARSE_STAGING_BUCKET is empty, fall back to SUPABASE_STORAGE_BUCKET so
    a single-bucket deployment (the common case) works without any extra config.
    """
    from app.config import settings
    return settings.PARSE_STAGING_BUCKET or settings.SUPABASE_STORAGE_BUCKET


# ── Path helpers ──────────────────────────────────────────────────────────────

def _json_path(document_id: str) -> str:
    return f"staging/{document_id}/parsed.json"


def _image_blob_path(document_id: str, image_index: int) -> str:
    return f"staging/{document_id}/img_{image_index}.png"


def _table_blob_path(document_id: str, table_index: int) -> str:
    return f"staging/{document_id}/table_{table_index}.png"


# ── Serialization helpers ─────────────────────────────────────────────────────

def _dataclass_to_dict_no_bytes(obj: Any) -> Any:
    """Recursively convert a dataclass (possibly nested) to a JSON-safe dict.

    bytes fields are replaced with None — they are stored as separate blobs and
    rehydrated by load_parsed().  All other field types (str, int, float, bool,
    list, dict, None) pass through unchanged.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            if isinstance(value, bytes):
                result[f.name] = None          # strip bytes — store as blob
            elif isinstance(value, list):
                result[f.name] = [_dataclass_to_dict_no_bytes(v) for v in value]
            elif dataclasses.is_dataclass(value):
                result[f.name] = _dataclass_to_dict_no_bytes(value)
            else:
                result[f.name] = value
        return result
    if isinstance(obj, list):
        return [_dataclass_to_dict_no_bytes(v) for v in obj]
    return obj


# ── Deserialization helpers ───────────────────────────────────────────────────

def _dict_to_bounding_box(d: dict | None):
    if d is None:
        return None
    from app.models.document import BoundingBox
    return BoundingBox(x1=d["x1"], y1=d["y1"], x2=d["x2"], y2=d["y2"])


def _dict_to_text_block(d: dict):
    from app.models.document import TextBlock
    return TextBlock(
        text=d["text"],
        page_number=d["page_number"],
        block_type=d["block_type"],
        section_title=d.get("section_title"),
        section_level=d.get("section_level"),
        bbox=_dict_to_bounding_box(d.get("bbox")),
        token_count=d.get("token_count", 0),
    )


def _dict_to_extracted_table(d: dict):
    from app.models.document import ExtractedTable
    return ExtractedTable(
        table_index=d["table_index"],
        page_number=d["page_number"],
        headers=d.get("headers") or [],
        rows=d.get("rows") or [],
        caption=d.get("caption"),
        bbox=_dict_to_bounding_box(d.get("bbox")),
        raw_text=d.get("raw_text") or "",
        markdown_text=d.get("markdown_text") or "",
        image_png_bytes=None,               # rehydrated in load_parsed
        table_metadata=d.get("table_metadata") or {},
    )


def _dict_to_extracted_image(d: dict):
    from app.models.document import ExtractedImage
    return ExtractedImage(
        image_index=d["image_index"],
        page_number=d["page_number"],
        bbox=_dict_to_bounding_box(d.get("bbox")),
        png_bytes=b"",                      # rehydrated in load_parsed
        width=d.get("width") or 0,
        height=d.get("height") or 0,
    )


def _dict_to_parsed_document(d: dict):
    from app.models.document import ParsedDocument
    return ParsedDocument(
        doc_id=d["doc_id"],
        filename=d["filename"],
        raw_text=d.get("raw_text") or "",
        text_blocks=[_dict_to_text_block(b) for b in (d.get("text_blocks") or [])],
        tables=[_dict_to_extracted_table(t) for t in (d.get("tables") or [])],
        page_count=d.get("page_count") or 0,
        word_count=d.get("word_count") or 0,
        has_tables=bool(d.get("has_tables")),
        has_images=bool(d.get("has_images")),
        language_detected=d.get("language_detected") or "en",
        metadata=d.get("metadata") or {},
        image_page_numbers=d.get("image_page_numbers") or [],
        images=[_dict_to_extracted_image(i) for i in (d.get("images") or [])],
    )


# ── Public API ────────────────────────────────────────────────────────────────

def save_parsed(document_id: str, parsed: "ParsedDocument") -> str:
    """Serialize *parsed* and upload all blobs to Supabase Storage.

    Returns the blob_path of the main JSON blob.

    Steps
    1. Build JSON-safe dict (bytes → None).
    2. Upload parsed.json.
    3. Upload each image PNG blob (img_<i>.png).
    4. Upload each table PNG blob (table_<i>.png).
    5. Upsert parse_staging row with counts.

    All paths are deterministic → idempotent on retry.
    """
    from app.services.supabase_storage import upload_file
    from app.db.repositories import parse_staging as staging_repo

    bucket = _staging_bucket()

    # 1. Serialise (bytes stripped)
    doc_dict = _dataclass_to_dict_no_bytes(parsed)
    json_bytes = json.dumps(doc_dict, ensure_ascii=False).encode("utf-8")

    # 2. Upload main JSON blob
    json_path = _json_path(document_id)
    upload_file(bucket, json_path, json_bytes, "application/json")
    logger.info("[%s] Staged parsed.json (%d bytes) → %s/%s",
                document_id, len(json_bytes), bucket, json_path)

    # 3. Upload image PNG blobs
    for img in parsed.images or []:
        if img.png_bytes:
            p = _image_blob_path(document_id, img.image_index)
            try:
                upload_file(bucket, p, img.png_bytes, "image/png")
            except Exception as exc:
                logger.warning("[%s] image blob upload failed (img %d): %s",
                               document_id, img.image_index, exc)

    # 4. Upload table PNG blobs
    for tbl in parsed.tables or []:
        if getattr(tbl, "image_png_bytes", None):
            p = _table_blob_path(document_id, tbl.table_index)
            try:
                upload_file(bucket, p, tbl.image_png_bytes, "image/png")
            except Exception as exc:
                logger.warning("[%s] table blob upload failed (tbl %d): %s",
                               document_id, tbl.table_index, exc)

    # 5. Upsert parse_staging row
    staging_repo.upsert_staging(
        document_id=document_id,
        storage_bucket=bucket,
        blob_path=json_path,
        status="staged",
        page_count=parsed.page_count,
        block_count=len(parsed.text_blocks),
        table_count=len(parsed.tables),
        image_count=len(parsed.images),
        bytes_size=len(json_bytes),
    )
    return json_path


def load_parsed(document_id: str) -> "ParsedDocument":
    """Download and fully reconstruct a ParsedDocument from Supabase Storage.

    Steps
    1. Fetch the parse_staging row to get bucket + blob_path.
    2. Download and parse parsed.json → rebuild dataclasses.
    3. Re-download each PNG blob and re-attach to the image/table objects.

    Raises RuntimeError if no staging row is found.
    """
    from app.services.supabase_storage import download_file
    from app.db.repositories import parse_staging as staging_repo

    row = staging_repo.get_staging(document_id)
    if row is None:
        raise RuntimeError(
            f"No parse_staging row for document_id={document_id}. "
            "Was save_parsed() called successfully?"
        )

    bucket = row["storage_bucket"]
    blob_path = row["blob_path"]

    # 1. Download + parse JSON
    json_bytes = download_file(bucket, blob_path)
    doc_dict = json.loads(json_bytes.decode("utf-8"))

    # 2. Reconstruct dataclasses
    parsed = _dict_to_parsed_document(doc_dict)

    # 3. Rehydrate image PNG blobs
    for img in parsed.images:
        p = _image_blob_path(document_id, img.image_index)
        try:
            img.png_bytes = download_file(bucket, p)
        except Exception as exc:
            logger.warning("[%s] image blob download failed (img %d): %s",
                           document_id, img.image_index, exc)
            img.png_bytes = b""

    # 4. Rehydrate table PNG blobs
    for tbl in parsed.tables:
        p = _table_blob_path(document_id, tbl.table_index)
        try:
            tbl.image_png_bytes = download_file(bucket, p)
        except Exception:
            # Tables without crop images (most) → None is correct
            tbl.image_png_bytes = None

    logger.info("[%s] Loaded ParsedDocument from staging (%d pages, %d blocks, %d tables, %d images)",
                document_id,
                parsed.page_count,
                len(parsed.text_blocks),
                len(parsed.tables),
                len(parsed.images))
    return parsed


def staging_exists(document_id: str) -> bool:
    """Return True if a parse_staging row exists for *document_id*."""
    from app.db.repositories import parse_staging as staging_repo
    return staging_repo.get_staging(document_id) is not None


def delete_staging(document_id: str) -> None:
    """Delete all staging blobs and the parse_staging row for *document_id*.

    Non-fatal: errors are logged but not re-raised (retention cleanup should
    never crash the main ingestion flow).
    """
    from app.services.supabase_storage import delete_files
    from app.db.repositories import parse_staging as staging_repo

    row = staging_repo.get_staging(document_id)
    if row is None:
        return

    bucket = row["storage_bucket"]
    paths_to_delete: list[str] = [row["blob_path"]]

    # Enumerate blobs: we don't store the exact list so re-derive from counts.
    page_count = row.get("image_count") or 0
    for i in range(page_count):
        paths_to_delete.append(_image_blob_path(document_id, i))

    table_count = row.get("table_count") or 0
    for i in range(table_count):
        paths_to_delete.append(_table_blob_path(document_id, i))

    try:
        delete_files(bucket, paths_to_delete)
    except Exception as exc:
        logger.warning("[%s] staging blob delete failed (non-fatal): %s", document_id, exc)

    try:
        staging_repo.delete_staging(document_id)
    except Exception as exc:
        logger.warning("[%s] staging row delete failed (non-fatal): %s", document_id, exc)

    logger.info("[%s] Staging blobs and row deleted", document_id)
