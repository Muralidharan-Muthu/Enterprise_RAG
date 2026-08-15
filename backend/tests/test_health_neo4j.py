from unittest.mock import patch

import app.api.routes.health as health
from app.config import Settings, settings


# ── config defaults ──────────────────────────────────────────────────

def test_neo4j_config_defaults_safe():
    # Enabled by default (GraphRAG is part of the standard architecture). This is
    # still SAFE without Neo4j provisioned: graph_service._get_driver() returns
    # None and is_available() returns False when the URI/credentials are unset or
    # unreachable, so every graph operation — query routing AND ingestion
    # extraction — short-circuits gracefully (see run_graph_stage's is_available
    # gate and route_graphrag). Checked against the Settings class's declared
    # default, not the live `settings` singleton (which reflects the local/CI .env).
    default_field = Settings.model_fields["NEO4J_ENABLED"]
    assert default_field.default is True
    # NEO4J_USERNAME is the renamed field (was NEO4J_USER); accepts Aura-style neo4j+s:// too.
    assert hasattr(settings, "NEO4J_USERNAME")
    assert settings.NEO4J_USERNAME  # non-empty


# ── health mapping ───────────────────────────────────────────────────

def test_check_neo4j_disabled():
    with patch.object(health.settings, "NEO4J_ENABLED", False):
        assert health._check_neo4j() == "disabled"


def test_check_neo4j_ok_when_available():
    with patch.object(health.settings, "NEO4J_ENABLED", True), \
         patch("app.services.graph_service.is_available", return_value=True):
        assert health._check_neo4j() == "ok"


def test_check_neo4j_unreachable_when_not_available():
    with patch.object(health.settings, "NEO4J_ENABLED", True), \
         patch("app.services.graph_service.is_available", return_value=False):
        assert health._check_neo4j() == "unreachable"


def test_check_neo4j_unreachable_when_is_available_raises():
    with patch.object(health.settings, "NEO4J_ENABLED", True), \
         patch("app.services.graph_service.is_available", side_effect=RuntimeError("boom")):
        assert health._check_neo4j() == "unreachable"
