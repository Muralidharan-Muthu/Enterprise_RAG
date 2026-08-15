"""Unit tests for the in-process signed-URL cache in supabase_storage.

No network: the raw generator and the Supabase client are monkeypatched.
"""
from unittest.mock import MagicMock

import pytest

from app.services import supabase_storage as ss


@pytest.fixture(autouse=True)
def _clear_cache():
    ss._signed_url_cache.clear()
    yield
    ss._signed_url_cache.clear()


def _counter(monkeypatch):
    calls = {"n": 0}

    def fake_raw(bucket, path, expires_in):
        calls["n"] += 1
        return f"https://signed/{bucket}/{path}?v={calls['n']}"

    monkeypatch.setattr(ss, "_create_signed_url_raw", fake_raw)
    return calls


def test_second_call_is_cached(monkeypatch):
    calls = _counter(monkeypatch)
    u1 = ss.create_signed_url("bkt", "images/d/0.png")
    u2 = ss.create_signed_url("bkt", "images/d/0.png")
    assert u1 == u2                 # same URL returned
    assert calls["n"] == 1          # generated only once


def test_distinct_paths_generate_separately(monkeypatch):
    calls = _counter(monkeypatch)
    ss.create_signed_url("bkt", "a.png")
    ss.create_signed_url("bkt", "b.png")
    assert calls["n"] == 2


def test_distinct_expiry_is_a_distinct_key(monkeypatch):
    calls = _counter(monkeypatch)
    ss.create_signed_url("bkt", "a.png", expires_in=3600)
    ss.create_signed_url("bkt", "a.png", expires_in=60)
    assert calls["n"] == 2


def test_cache_refreshes_after_expiry(monkeypatch):
    calls = _counter(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(ss.time, "monotonic", lambda: clock["t"])

    ss.create_signed_url("bkt", "a.png", expires_in=3600)   # ttl = 3600 - 300 = 3300
    assert calls["n"] == 1
    clock["t"] += 3299                                       # still valid
    ss.create_signed_url("bkt", "a.png", expires_in=3600)
    assert calls["n"] == 1
    clock["t"] += 2                                          # now past expiry
    ss.create_signed_url("bkt", "a.png", expires_in=3600)
    assert calls["n"] == 2


def test_delete_files_invalidates_cache(monkeypatch):
    _counter(monkeypatch)
    monkeypatch.setattr(ss, "_client", lambda: MagicMock())   # no-op remove()
    ss.create_signed_url("bkt", "gone.png")
    assert ("bkt", "gone.png", 3600) in ss._signed_url_cache
    ss.delete_files("bkt", ["gone.png"])
    assert ("bkt", "gone.png", 3600) not in ss._signed_url_cache
