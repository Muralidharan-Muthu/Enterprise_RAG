import logging
import threading
import time

from supabase import create_client

logger = logging.getLogger(__name__)

_UPLOAD_MAX_ATTEMPTS = 4

# ── signed-URL cache ──────────────────────────────────────────────────────────
# The images panel polls /documents/{id}/images every ~1.2s. Without caching, each
# poll regenerated a signed URL per image — a fresh Supabase client + network round
# trip EACH — which dominated the endpoint latency. Cache by (bucket, path,
# expires_in): a signed URL points to a path and stays valid across re-uploads of
# that path, so it is safe to reuse until shortly before it expires.
_signed_url_cache: dict[tuple[str, str, int], tuple[str, float]] = {}
_signed_url_lock = threading.Lock()
_SIGNED_URL_SAFETY_MARGIN = 300   # refresh 5 min before the URL actually expires
_SIGNED_URL_CACHE_MAX = 2000      # bound memory: prune expired, then reset if still full


def _client():
    from app.config import settings
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


def upload_file(bucket: str, path: str, content: bytes, content_type: str = "application/pdf") -> str:
    """Upload bytes to Supabase Storage. Returns the storage path.

    Uses upsert so re-ingestion / reprocessing of the same document_id (which
    yields deterministic paths like images/<doc_id>/<idx>.png) overwrites the
    existing object instead of failing with a 409 "already exists".

    Retries on transient failures. The storage3 SDK has a latent bug: a network
    hiccup during the request (httpx ConnectError/ReadTimeout — both subclasses
    of HTTPError) lands in its `except HTTPError` branch, which references an
    unassigned `response` and raises `UnboundLocalError: ... 'response' ...`,
    masking the real transient error. Under rapid sequential uploads (per-image
    and per-table-crop) this intermittently dropped objects, leaving NULL
    storage paths. A fresh retry re-issues the request and almost always
    succeeds, so we retry with linear backoff before giving up."""
    last_exc: Exception | None = None
    for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
        try:
            _client().storage.from_(bucket).upload(
                path=path,
                file=content,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            logger.info("Uploaded to storage: %s/%s", bucket, path)
            return path
        except Exception as exc:  # incl. the SDK's UnboundLocalError on a transient HTTPError
            last_exc = exc
            if attempt < _UPLOAD_MAX_ATTEMPTS:
                logger.warning(
                    "upload %s/%s failed (attempt %d/%d): %s — retrying",
                    bucket, path, attempt, _UPLOAD_MAX_ATTEMPTS, exc,
                )
                time.sleep(0.5 * attempt)
    raise RuntimeError(f"upload failed after {_UPLOAD_MAX_ATTEMPTS} attempts: {bucket}/{path}") from last_exc


def download_file(bucket: str, path: str) -> bytes:
    """Download a file from Supabase Storage. Returns raw bytes."""
    data = _client().storage.from_(bucket).download(path)
    logger.info("Downloaded from storage: %s/%s (%d bytes)", bucket, path, len(data))
    return data


def delete_files(bucket: str, paths: list[str]) -> None:
    """Delete a list of files from a Supabase Storage bucket.

    Missing paths are silently ignored by the storage API, so this is safe to
    call even when some files were never uploaded or were already deleted.
    """
    if not paths:
        return
    _client().storage.from_(bucket).remove(paths)
    # Drop any cached signed URLs for the removed paths so we never hand out a URL
    # to an object that no longer exists.
    with _signed_url_lock:
        for key in [k for k in _signed_url_cache if k[0] == bucket and k[1] in set(paths)]:
            _signed_url_cache.pop(key, None)
    logger.info("Deleted %d file(s) from storage bucket %s", len(paths), bucket)


def _create_signed_url_raw(bucket: str, path: str, expires_in: int) -> str:
    """Mint a fresh signed URL from Supabase (no cache)."""
    resp = _client().storage.from_(bucket).create_signed_url(path, expires_in)
    if isinstance(resp, dict):
        for key in ("signedURL", "signedUrl", "signed_url", "url"):
            if resp.get(key):
                return resp[key]
    raise RuntimeError(f"Unexpected signed-url response for {bucket}/{path}: {resp!r}")


def _prune_signed_url_cache(now: float) -> None:
    """Drop expired entries (caller holds the lock)."""
    for key in [k for k, (_, exp) in _signed_url_cache.items() if exp <= now]:
        _signed_url_cache.pop(key, None)


def create_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Mint a time-limited signed URL for a private-bucket object, cached in-process
    until shortly before it expires (see the cache note at module top)."""
    key = (bucket, path, expires_in)
    now = time.monotonic()

    with _signed_url_lock:
        hit = _signed_url_cache.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]

    # Generate outside the lock (network I/O). A rare duplicate generation under a
    # race is harmless — both callers get a valid URL and the last write wins.
    url = _create_signed_url_raw(bucket, path, expires_in)
    ttl = max(1, expires_in - _SIGNED_URL_SAFETY_MARGIN)

    with _signed_url_lock:
        if len(_signed_url_cache) >= _SIGNED_URL_CACHE_MAX:
            _prune_signed_url_cache(now)
            if len(_signed_url_cache) >= _SIGNED_URL_CACHE_MAX:
                _signed_url_cache.clear()   # bounded fallback
        _signed_url_cache[key] = (url, now + ttl)

    return url
