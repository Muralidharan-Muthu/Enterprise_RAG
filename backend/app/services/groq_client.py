"""
Shared Groq (OpenAI-compatible) chat client.

Provides two entry points:
  chat()        — synchronous, for Celery ingestion workers
  chat_async()  — async, for the FastAPI query path

Features:
- Global cross-thread & cross-coroutine request pacer ensuring requests respect Groq's RPM limits (~30 RPM).
- Adaptive 429 Rate Limit backoff parsing 'retry-after', 'x-ratelimit-reset', or JSON error messages.
- Connection pooling with keepalive and thread-safe / async-safe concurrency limits.
"""
import asyncio
import json as _json
import logging
import random
import re
import threading
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Global Request Pacer State
_last_request_monotonic = 0.0
_pacer_lock = threading.Lock()
_async_pacer_lock = asyncio.Lock()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _extract_retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    """Determine retry delay, respecting Retry-After header or parsing error message,
    with adaptive exponential backoff fallback."""
    if resp is not None:
        # 1. Check standard headers
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return max(1.5, float(retry_after) + 0.5)
            except ValueError:
                pass

        reset_req = resp.headers.get("x-ratelimit-reset-requests")
        if reset_req:
            try:
                return max(1.5, float(reset_req.rstrip("s")) + 0.5)
            except ValueError:
                pass

        # 2. Check JSON response body for 'try again in X.XXs'
        try:
            body = resp.text
            m = re.search(r"try again in ([\d\.]+)s", body, re.IGNORECASE)
            if m:
                return float(m.group(1)) + 0.8
        except Exception:
            pass

    # 3. Exponential backoff with jitter: 2.5s, 5.0s, 10.0s, 20.0s, 35.0s
    base_delay = min(2.5 * (2 ** (attempt - 1)), 35.0)
    jitter = random.uniform(0.3, 1.0)
    return base_delay + jitter


def _pace_request_sync() -> None:
    """Enforce minimum interval between Groq requests globally across threads."""
    global _last_request_monotonic
    min_interval = getattr(settings, "GROQ_MIN_INTERVAL_SECONDS", 2.0)
    if min_interval <= 0:
        return
    with _pacer_lock:
        now = time.monotonic()
        elapsed = now - _last_request_monotonic
        if elapsed < min_interval:
            wait_time = min_interval - elapsed
            time.sleep(wait_time)
        _last_request_monotonic = time.monotonic()


async def _pace_request_async() -> None:
    """Enforce minimum interval between Groq requests globally across async coroutines."""
    global _last_request_monotonic
    min_interval = getattr(settings, "GROQ_MIN_INTERVAL_SECONDS", 2.0)
    if min_interval <= 0:
        return
    async with _async_pacer_lock:
        now = time.monotonic()
        elapsed = now - _last_request_monotonic
        if elapsed < min_interval:
            wait_time = min_interval - elapsed
            await asyncio.sleep(wait_time)
        _last_request_monotonic = time.monotonic()


def _base_url() -> str:
    return (settings.GROQ_BASE_URL or "https://api.groq.com/openai/v1").rstrip("/")


def _api_key() -> str:
    return settings.GROQ_API_KEY


def _model_name() -> str:
    return settings.GROQ_MODEL_NAME or "llama-3.3-70b-versatile"


def _timeout_seconds() -> int:
    return settings.GROQ_TIMEOUT_SECONDS or 120


def _connect_timeout() -> int:
    return settings.GROQ_CONNECT_TIMEOUT_SECONDS or 10


def _max_retries() -> int:
    return getattr(settings, "GROQ_MAX_RETRIES", 6)


def _max_concurrent() -> int:
    return getattr(settings, "GROQ_MAX_CONCURRENT", 3) or 3


def _build_headers() -> dict:
    h = {"Content-Type": "application/json", "User-Agent": "MultiStoreRAG/1.0"}
    key = _api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _build_payload(messages: list, max_tokens: int, temperature: float, model: str | None = None) -> dict:
    return {
        "model": model or _model_name(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_connect_timeout(),
        read=_timeout_seconds(),
        write=_connect_timeout(),
        pool=_connect_timeout(),
    )


def _limits() -> httpx.Limits:
    return httpx.Limits(
        max_keepalive_connections=10,
        max_connections=20,
        keepalive_expiry=60,
    )


# ── Sync client (Celery ingestion workers) ───────────────────────────────────

_client: httpx.Client | None = None
_client_lock = threading.Lock()

_sync_semaphore: threading.Semaphore | None = None
_sync_semaphore_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """Process-wide pooled sync client, thread-safe double-checked init."""
    global _client
    if _client is None or _client.is_closed:
        with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.Client(timeout=_timeout(), limits=_limits())
    return _client


def _get_sync_semaphore() -> threading.Semaphore:
    """Process-wide semaphore shared by every sync/thread caller."""
    global _sync_semaphore
    if _sync_semaphore is None:
        with _sync_semaphore_lock:
            if _sync_semaphore is None:
                limit = _max_concurrent()
                if limit <= 0:
                    limit = 1
                _sync_semaphore = threading.Semaphore(limit)
    return _sync_semaphore


def chat(
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.1,
    retries: int | None = None,
    timeout: float | None = None,
    model: str | None = None,
) -> str:
    """Sync LLM (Groq) call — rates paces and retries transient connect errors and 429/5xx."""
    base = _base_url()
    if not base:
        raise RuntimeError("LLM BASE_URL not configured")

    attempts = (_max_retries() if retries is None else retries) + 1
    url = f"{base}/chat/completions"
    headers = _build_headers()
    payload = _build_payload(messages, max_tokens, temperature, model=model)
    client = _get_client()
    last_exc: Exception | None = None

    semaphore = _get_sync_semaphore()
    for attempt in range(1, attempts + 1):
        _pace_request_sync()
        semaphore.acquire()
        try:
            if timeout is not None:
                resp = client.post(url, json=payload, headers=headers, timeout=timeout)
            else:
                resp = client.post(url, json=payload, headers=headers)

            if resp.status_code in _RETRYABLE_STATUS:
                delay = _extract_retry_delay(resp, attempt)
                if attempt < attempts:
                    logger.warning(
                        "Groq HTTP %s rate/server limit (attempt %d/%d) — waiting %.2fs before retry",
                        resp.status_code, attempt, attempts, delay
                    )
                    time.sleep(delay)
                    continue
                else:
                    logger.error(
                        "Groq HTTP %s failed after %d attempts: %s",
                        resp.status_code, attempts, resp.text[:300]
                    )

            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            last_exc = exc
            delay = _extract_retry_delay(None, attempt)
            logger.warning("Groq connect error (attempt %d/%d): %s — waiting %.2fs", attempt, attempts, exc, delay)
            if attempt < attempts:
                time.sleep(delay)
                continue
            raise
        except httpx.ReadTimeout:
            logger.warning("Groq read timeout after %ds", _timeout_seconds())
            raise
        finally:
            semaphore.release()

    if last_exc:
        raise last_exc
    raise RuntimeError("LLM call failed after retries")


# ── Async client + semaphore (FastAPI query path) ─────────────────────────────

_async_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_async_client() -> httpx.AsyncClient:
    """Lazy singleton — safe because asyncio is single-threaded."""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(timeout=_timeout(), limits=_limits())
    return _async_client


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy singleton — safe because asyncio is single-threaded."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_max_concurrent())
    return _semaphore


async def chat_async(
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.1,
    retries: int | None = None,
    model: str | None = None,
) -> str:
    """Async LLM call — acquires a semaphore slot and rate paces requests."""
    base = _base_url()
    if not base:
        raise RuntimeError("LLM BASE_URL not configured")

    attempts = (_max_retries() if retries is None else retries) + 1
    url = f"{base}/chat/completions"
    headers = _build_headers()
    payload = _build_payload(messages, max_tokens, temperature, model=model)
    last_exc: Exception | None = None

    async with _get_semaphore():
        client = _get_async_client()
        for attempt in range(1, attempts + 1):
            await _pace_request_async()
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in _RETRYABLE_STATUS:
                    delay = _extract_retry_delay(resp, attempt)
                    if attempt < attempts:
                        logger.warning(
                            "Groq async HTTP %s rate/server limit (attempt %d/%d) — waiting %.2fs before retry",
                            resp.status_code, attempt, attempts, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error(
                            "Groq async HTTP %s failed after %d attempts: %s",
                            resp.status_code, attempts, resp.text[:300]
                        )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                last_exc = exc
                delay = _extract_retry_delay(None, attempt)
                logger.warning("Groq async connect error (attempt %d/%d): %s — waiting %.2fs", attempt, attempts, exc, delay)
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    continue
                raise
            except httpx.ReadTimeout:
                logger.warning("Groq async read timeout after %ds", _timeout_seconds())
                raise

    if last_exc:
        raise last_exc
    raise RuntimeError("LLM async call failed after retries")


async def chat_async_stream(
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.1,
    model: str | None = None,
):
    """Async streaming LLM call — yields text delta strings via SSE."""
    base = _base_url()
    if not base:
        raise RuntimeError("LLM BASE_URL not configured")

    url = f"{base}/chat/completions"
    headers = _build_headers()
    payload = _build_payload(messages, max_tokens, temperature, model=model)
    payload["stream"] = True

    async with _get_semaphore():
        await _pace_request_async()
        client = _get_async_client()
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    return
                if not data:
                    continue
                try:
                    chunk = _json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (ValueError, KeyError, IndexError):
                    continue


