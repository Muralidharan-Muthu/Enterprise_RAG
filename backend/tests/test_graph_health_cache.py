from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.services.graph_service as gs


@pytest.fixture(autouse=True)
def _reset_cache_state():
    gs._driver = None
    gs._driver_failed = False
    gs._last_check_at = 0.0
    gs._last_check_result = False
    yield
    gs._driver = None
    gs._driver_failed = False
    gs._last_check_at = 0.0
    gs._last_check_result = False


def _mock_driver():
    session = MagicMock()

    @contextmanager
    def _session(*args, **kwargs):
        yield session

    driver = MagicMock()
    driver.session.side_effect = _session
    return driver


def test_repeated_calls_within_ttl_hit_cache_once():
    driver = _mock_driver()
    t = [1000.0]
    with patch.object(gs, "_get_driver", return_value=driver), \
         patch.object(gs.time, "monotonic", side_effect=lambda: t[0]):
        assert gs.is_available() is True
        driver.verify_connectivity.assert_called_once()

        # advance well within the TTL window — should be a cache hit
        t[0] += gs.settings.NEO4J_HEALTH_CACHE_TTL_SECONDS / 2
        assert gs.is_available() is True
        driver.verify_connectivity.assert_called_once()  # still just once


def test_cached_result_expires_after_ttl():
    driver = _mock_driver()
    t = [1000.0]
    with patch.object(gs, "_get_driver", return_value=driver), \
         patch.object(gs.time, "monotonic", side_effect=lambda: t[0]):
        assert gs.is_available() is True
        driver.verify_connectivity.assert_called_once()

        t[0] += gs.settings.NEO4J_HEALTH_CACHE_TTL_SECONDS + 1
        assert gs.is_available() is True
        assert driver.verify_connectivity.call_count == 2


def test_cached_failure_expires_after_ttl_and_can_recover():
    driver = _mock_driver()
    driver.verify_connectivity.side_effect = [RuntimeError("down"), None]
    t = [1000.0]
    with patch.object(gs, "_get_driver", return_value=driver), \
         patch.object(gs.time, "monotonic", side_effect=lambda: t[0]):
        assert gs.is_available() is False
        driver.verify_connectivity.assert_called_once()

        # still within TTL — cached failure returned, no new network call
        t[0] += gs.settings.NEO4J_HEALTH_CACHE_TTL_SECONDS / 2
        assert gs.is_available() is False
        driver.verify_connectivity.assert_called_once()

        # TTL expires — re-checks and recovers
        t[0] += gs.settings.NEO4J_HEALTH_CACHE_TTL_SECONDS + 1
        assert gs.is_available() is True
        assert driver.verify_connectivity.call_count == 2


def test_disabled_short_circuits_without_touching_cache():
    with patch.object(gs.settings, "NEO4J_ENABLED", False):
        assert gs.is_available() is False
    assert gs._last_check_at == 0.0
