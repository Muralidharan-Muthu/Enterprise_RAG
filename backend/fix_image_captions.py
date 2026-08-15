"""
fix_image_captions.py — Backfill structured_content + detected_store for image_store
rows that have missing or malformed data:
  - structured_content is empty/null AND width > 0 (real image with no extraction)
  - OR ocr_text contains raw JSON (leftover from old pipeline)

The old `caption` column no longer exists.  This script calls the current pipeline:
  ocr_image(png_bytes) -> raw OCR
  analyze_image(png_bytes, raw_ocr) -> structured_content / detected_store / content_type

Run from backend/ with venv activated:
    python fix_image_captions.py           # dry-run (prints what it would do)
    python fix_image_captions.py --apply   # actually update the DB
"""
import sys
import logging

sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

DRY_RUN = "--apply" not in sys.argv


def run():
    from app.config import settings
    from app.db.connection import get_db
    from app.services.supabase_storage import download_file
    from app.services.ocr_service import ocr_image
    from app.services.image_analysis_service import analyze_image

    SELECT_SQL = """
        SELECT id, storage_path, storage_bucket, ocr_text, structured_content,
               content_type, width, height
        FROM multi_store_rag_working.image_store
        WHERE
            (structured_content IS NULL OR structured_content = '')
            AND width > 0
        ORDER BY created_at DESC
    """

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_SQL)
            rows = cur.fetchall()

    print(f"\nFound {len(rows)} image_store rows to fix")
    if DRY_RUN:
        print("DRY RUN — pass --apply to write changes\n")

    fixed = skipped = failed = 0

    for img_id, storage_path, storage_bucket, old_ocr, old_sc, content_type, width, height in rows:
        bucket = storage_bucket or settings.SUPABASE_STORAGE_BUCKET
        print(f"\n{'─'*60}")
        print(f"  id:                  {img_id}")
        print(f"  storage_path:        {storage_path}")
        print(f"  size:                {width}×{height}  content_type={content_type}")
        print(f"  old structured_content: {str(old_sc)[:120]!r}")
        print(f"  old ocr_text:        {str(old_ocr)[:120]!r}")

        try:
            png_bytes = download_file(bucket, storage_path)
            print(f"  downloaded:          {len(png_bytes):,} bytes")
        except Exception as exc:
            print(f"  [SKIP] download failed: {exc}")
            skipped += 1
            continue

        # Step 1: raw OCR
        try:
            raw_ocr = ocr_image(png_bytes)
        except Exception as exc:
            logger.warning("ocr_image failed for %s: %s", img_id, exc)
            raw_ocr = old_ocr or ""

        # Step 2: VLM analysis
        result = analyze_image(png_bytes, raw_ocr)
        new_sc = result.get("structured_content", "")
        new_detected_store = result.get("detected_store", "image_store")
        new_ct = result.get("content_type", content_type or "figure")
        new_ocr = raw_ocr  # always write the fresh raw OCR

        print(f"  new structured_content: {new_sc[:120]!r}")
        print(f"  new detected_store:  {new_detected_store}")
        print(f"  new content_type:    {new_ct}")
        print(f"  new ocr_text:        {new_ocr[:120]!r}")

        if not new_sc and not new_ocr:
            print("  [SKIP] analysis returned empty — endpoint may not support images")
            skipped += 1
            continue

        if DRY_RUN:
            print("  [DRY RUN] would update")
            fixed += 1
            continue

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE multi_store_rag_working.image_store
                           SET ocr_text = %s,
                               structured_content = %s,
                               detected_store = %s,
                               content_type = %s
                           WHERE id = %s""",
                        (new_ocr, new_sc, new_detected_store, new_ct, img_id),
                    )
            print(f"  [OK] updated in DB")
            fixed += 1
        except Exception as exc:
            print(f"  [FAIL] DB update failed: {exc}")
            failed += 1

    print(f"\n{'='*60}")
    suffix = " (dry run)" if DRY_RUN else ""
    print(f"Done{suffix}: {fixed} fixed, {skipped} skipped, {failed} failed")
    if DRY_RUN and fixed:
        print("Re-run with --apply to write changes.")


if __name__ == "__main__":
    run()
