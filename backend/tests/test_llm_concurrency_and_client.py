"""Tests for package B: LLM client hygiene / concurrency safety.

1. groq_client.chat() (sync path) must be gated by a shared threading.Semaphore
   sized to settings.GROQ_MAX_CONCURRENT, so concurrent callers (e.g. clause
   enrichment's ThreadPoolExecutor) never exceed the cap.
2. router_service must reuse a single module-level httpx.Client instead of
   constructing a new one per call.

No real network calls are made in this file.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import httpx
import pytest

import app.services.groq_client as groq_client
import app.services.router_service as router_service


# ── Sync semaphore cap ──────────────────────────────────────────────────────────

class _ConcurrencyTracker:
    """Records the max number of simultaneously in-flight calls."""

    def __init__(self):
        self.current = 0
        self.max_seen = 0
        self.lock = threading.Lock()

    def enter(self):
        with self.lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)

    def exit(self):
        with self.lock:
            self.current -= 1


@pytest.fixture(autouse=True)
def _reset_groq_client_state(monkeypatch):
    """Ensure each test starts with fresh module-level singletons so settings
    monkeypatches actually take effect (they're read lazily on first use)."""
    monkeypatch.setattr(groq_client, "_sync_semaphore", None)
    monkeypatch.setattr(groq_client, "_client", None)
    yield
    monkeypatch.setattr(groq_client, "_sync_semaphore", None)
    monkeypatch.setattr(groq_client, "_client", None)


def test_sync_semaphore_caps_concurrency(monkeypatch):
    """Fire many concurrent groq_client.chat() calls with GROQ_MAX_CONCURRENT
    forced low; observed max concurrency must not exceed that cap."""
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_CONCURRENT", 2)
    monkeypatch.setattr(groq_client.settings, "GROQ_BASE_URL", "http://mock-Groq")
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_RETRIES", 0)

    tracker = _ConcurrencyTracker()

    def fake_post(url, json=None, headers=None):
        tracker.enter()
        try:
            time.sleep(0.05)
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            return resp
        finally:
            tracker.exit()

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.post.side_effect = fake_post
    monkeypatch.setattr(groq_client, "_client", mock_client)

    n_calls = 10
    with ThreadPoolExecutor(max_workers=n_calls) as pool:
        futures = [
            pool.submit(groq_client.chat, [{"role": "user", "content": "hi"}], 50)
            for _ in range(n_calls)
        ]
        results = [f.result() for f in futures]

    assert all(r == "ok" for r in results)
    assert tracker.max_seen <= 2
    assert tracker.max_seen >= 1


def test_sync_semaphore_default_respects_settings(monkeypatch):
    """With GROQ_MAX_CONCURRENT=1, calls must run strictly serially."""
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_CONCURRENT", 1)
    monkeypatch.setattr(groq_client.settings, "GROQ_BASE_URL", "http://mock-Groq")
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_RETRIES", 0)

    tracker = _ConcurrencyTracker()

    def fake_post(url, json=None, headers=None):
        tracker.enter()
        try:
            time.sleep(0.03)
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            return resp
        finally:
            tracker.exit()

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.post.side_effect = fake_post
    monkeypatch.setattr(groq_client, "_client", mock_client)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(groq_client.chat, [{"role": "user", "content": "hi"}], 50)
            for _ in range(5)
        ]
        [f.result() for f in futures]

    assert tracker.max_seen == 1


def test_semaphore_guards_against_non_positive_setting(monkeypatch):
    """GROQ_MAX_CONCURRENT <= 0 must be treated as 1, not break/deadlock."""
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_CONCURRENT", 0)
    sem = groq_client._get_sync_semaphore()
    # A semaphore initialized with 1 permit acquires once then blocks;
    # verify it was NOT constructed with 0 (which would deadlock immediately).
    acquired = sem.acquire(timeout=1)
    assert acquired is True
    sem.release()


def test_semaphore_is_shared_singleton(monkeypatch):
    """Repeated calls to _get_sync_semaphore() must return the same instance,
    proving the gate is process-wide, not per-call."""
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_CONCURRENT", 3)
    sem1 = groq_client._get_sync_semaphore()
    sem2 = groq_client._get_sync_semaphore()
    assert sem1 is sem2


def test_chat_releases_semaphore_on_read_timeout(monkeypatch):
    """Even when chat() raises (ReadTimeout not retried), the semaphore slot
    must be released — otherwise the cap permanently shrinks."""
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_CONCURRENT", 1)
    monkeypatch.setattr(groq_client.settings, "GROQ_BASE_URL", "http://mock-Groq")
    monkeypatch.setattr(groq_client.settings, "GROQ_MAX_RETRIES", 0)
    monkeypatch.setattr(groq_client.settings, "GROQ_TIMEOUT_SECONDS", 30)

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.post.side_effect = httpx.ReadTimeout("timed out")
    monkeypatch.setattr(groq_client, "_client", mock_client)

    with pytest.raises(httpx.ReadTimeout):
        groq_client.chat([{"role": "user", "content": "hi"}], 50)

    # Semaphore must have been released — a second call should not hang.
    sem = groq_client._get_sync_semaphore()
    acquired = sem.acquire(timeout=1)
    assert acquired is True
    sem.release()


# ── Router client reuse ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_router_client(monkeypatch):
    monkeypatch.setattr(router_service, "_client", None)
    yield
    monkeypatch.setattr(router_service, "_client", None)


def test_router_reuses_same_client_instance(monkeypatch):
    """Multiple _call_Groq invocations must reuse the same httpx.Client
    instance rather than constructing a new one per call."""
    monkeypatch.setattr(router_service.settings, "GROQ_BASE_URL", "http://mock-Groq")
    monkeypatch.setattr(router_service.settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(router_service.settings, "GROQ_MODEL_NAME", "Groq4-test")
    monkeypatch.setattr(router_service.settings, "GROQ_MAX_TOKENS", 100)
    monkeypatch.setattr(router_service.settings, "GROQ_TIMEOUT_SECONDS", 30)

    construction_count = {"n": 0}

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.post.return_value = resp

    def counting_client(*args, **kwargs):
        construction_count["n"] += 1
        return mock_client

    with patch("app.services.router_service.httpx.Client", side_effect=counting_client):
        client1 = router_service._get_client()
        router_service._call_Groq("prompt 1")
        client2 = router_service._get_client()
        router_service._call_Groq("prompt 2")
        router_service._call_Groq("prompt 3")

    assert construction_count["n"] == 1
    assert client1 is client2
    assert mock_client.post.call_count == 3


def test_get_client_is_singleton_across_direct_calls(monkeypatch):
    monkeypatch.setattr(router_service, "_client", None)
    c1 = router_service._get_client()
    c2 = router_service._get_client()
    assert c1 is c2


def test_get_client_recreates_if_closed(monkeypatch):
    monkeypatch.setattr(router_service, "_client", None)
    c1 = router_service._get_client()
    c1.close()
    c2 = router_service._get_client()
    assert c2 is not c1
    assert not c2.is_closed
