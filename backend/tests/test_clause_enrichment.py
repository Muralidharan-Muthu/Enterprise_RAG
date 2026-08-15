"""Unit tests for clause_enrichment_service (Phase 2)."""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models.document import LegalClause
from app.services.clause_enrichment_service import (
    ClauseEnrichmentResult,
    _apply,
    _parse_response,
    enrich_clauses_batch,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clause(idx: int, text: str = "The Vendor shall deliver the goods within 30 days.") -> LegalClause:
    return LegalClause(
        clause_index=idx,
        clause_text=text,
        clause_number=str(idx),
        clause_title=f"Clause {idx}",
        page_number=1,
        page_numbers=[1],
        section_path=[],
    )


def _mock_gemma(content: str):
    """Return a context-manager-compatible httpx mock that returns `content`."""
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = resp
    return client


def _mock_gemma_chat(content: str):
    """Patch target for app.services.clause_enrichment_service.gemma_client.chat
    that simply returns `content` (mirrors gemma_client.chat's return contract:
    a stripped string)."""
    return MagicMock(return_value=content)


def _enrichment_json(**overrides):
    base = {
        "clause_type": "obligation",
        "risk_level": "medium",
        "risk_rationale": "Payment obligation",
        "obligor": "Vendor",
        "obligee": "Client",
        "parties_mentioned": ["Vendor", "Client"],
        "key_dates": {"due": "2025-06-30"},
        "monetary_values": [{"amount": 10000, "currency": "USD", "description": "monthly fee"}],
    }
    base.update(overrides)
    return base


# ── ClauseEnrichmentResult validation ─────────────────────────────────────────

class TestClauseEnrichmentResult:
    def test_defaults(self):
        r = ClauseEnrichmentResult()
        assert r.clause_type == "general"
        assert r.risk_level is None
        assert r.risk_rationale is None
        assert r.obligor is None
        assert r.obligee is None
        assert r.parties_mentioned == []
        assert r.key_dates == {}
        assert r.monetary_values == []

    def test_valid_clause_type_accepted(self):
        for ct in ("obligation", "liability", "termination", "governing_law"):
            assert ClauseEnrichmentResult(clause_type=ct).clause_type == ct

    def test_invalid_clause_type_falls_back_to_general(self):
        assert ClauseEnrichmentResult(clause_type="bogus").clause_type == "general"
        assert ClauseEnrichmentResult(clause_type="").clause_type == "general"

    def test_valid_risk_levels(self):
        for rl in ("high", "medium", "low"):
            assert ClauseEnrichmentResult(risk_level=rl).risk_level == rl

    def test_invalid_risk_level_becomes_none(self):
        assert ClauseEnrichmentResult(risk_level="critical").risk_level is None
        assert ClauseEnrichmentResult(risk_level="MEDIUM").risk_level is None

    def test_parties_non_list_becomes_empty(self):
        assert ClauseEnrichmentResult(parties_mentioned="Vendor").parties_mentioned == []
        assert ClauseEnrichmentResult(parties_mentioned=None).parties_mentioned == []

    def test_parties_list_filters_blanks(self):
        r = ClauseEnrichmentResult(parties_mentioned=["Vendor", "", "  ", "Client"])
        assert r.parties_mentioned == ["Vendor", "Client"]

    def test_key_dates_non_dict_becomes_empty(self):
        assert ClauseEnrichmentResult(key_dates="2025-01-01").key_dates == {}
        assert ClauseEnrichmentResult(key_dates=["2025-01-01"]).key_dates == {}

    def test_key_dates_dict_preserved(self):
        r = ClauseEnrichmentResult(key_dates={"effective_date": "2025-01-01"})
        assert r.key_dates == {"effective_date": "2025-01-01"}

    def test_monetary_values_non_list_becomes_empty(self):
        assert ClauseEnrichmentResult(monetary_values="$500").monetary_values == []
        assert ClauseEnrichmentResult(monetary_values={"amount": 500}).monetary_values == []

    def test_monetary_values_list_of_dicts_preserved(self):
        mv = [{"amount": 500000, "currency": "USD", "description": "fee"}]
        r = ClauseEnrichmentResult(monetary_values=mv)
        assert r.monetary_values == mv

    def test_monetary_values_filters_non_dicts(self):
        r = ClauseEnrichmentResult(monetary_values=["bad", {"amount": 100}, None])
        assert r.monetary_values == [{"amount": 100}]


# ── _parse_response ────────────────────────────────────────────────────────────

class TestParseResponse:
    def _json(self, count: int = 1, **overrides) -> str:
        return json.dumps([_enrichment_json(**overrides)] * count)

    def test_valid_array_parsed(self):
        result = _parse_response(self._json(2), 2)
        assert result is not None
        assert len(result) == 2
        assert result[0].clause_type == "obligation"
        assert result[0].risk_level == "medium"
        assert result[0].obligor == "Vendor"
        assert result[0].key_dates == {"due": "2025-06-30"}
        assert result[0].monetary_values == [{"amount": 10000, "currency": "USD", "description": "monthly fee"}]

    def test_markdown_fences_stripped(self):
        raw = "```json\n" + self._json(1) + "\n```"
        result = _parse_response(raw, 1)
        assert result is not None
        assert result[0].clause_type == "obligation"

    def test_pads_with_fallback_when_too_few_items(self):
        result = _parse_response(self._json(1), 3)
        assert result is not None
        assert len(result) == 3
        assert result[0].clause_type == "obligation"
        assert result[1].clause_type == "general"
        assert result[2].clause_type == "general"

    def test_trims_when_too_many_items(self):
        result = _parse_response(self._json(5), 2)
        assert result is not None
        assert len(result) == 2

    def test_completely_invalid_json_returns_none(self):
        assert _parse_response("not json at all", 1) is None
        assert _parse_response("", 1) is None

    def test_json_object_not_array_returns_none(self):
        assert _parse_response('{"clause_type": "obligation"}', 1) is None

    def test_recovers_embedded_array(self):
        inner = json.dumps([_enrichment_json(clause_type="termination", risk_level=None,
                                             risk_rationale=None, obligor=None, obligee=None,
                                             parties_mentioned=[], key_dates={}, monetary_values=[])])
        raw = f"Here is the result: {inner} done."
        result = _parse_response(raw, 1)
        assert result is not None
        assert result[0].clause_type == "termination"

    def test_non_dict_item_in_array_uses_fallback(self):
        raw = json.dumps(["not a dict", _enrichment_json(clause_type="governing_law")])
        result = _parse_response(raw, 2)
        assert result is not None
        assert result[0].clause_type == "general"
        assert result[1].clause_type == "governing_law"

    def test_item_with_invalid_clause_type_coerced(self):
        raw = json.dumps([_enrichment_json(clause_type="made_up_type")])
        result = _parse_response(raw, 1)
        assert result is not None
        assert result[0].clause_type == "general"

    def test_null_risk_level_in_json(self):
        raw = json.dumps([_enrichment_json(risk_level=None, risk_rationale=None)])
        result = _parse_response(raw, 1)
        assert result is not None
        assert result[0].risk_level is None


# ── _apply ─────────────────────────────────────────────────────────────────────

class TestApply:
    def test_all_fields_applied(self):
        clause = _clause(0)
        e = ClauseEnrichmentResult(
            clause_type="indemnification",
            risk_level="high",
            risk_rationale="Broad unlimited indemnity",
            obligor="Supplier",
            obligee="Buyer",
            parties_mentioned=["Supplier", "Buyer"],
            key_dates={"expiry": "2026-12-31"},
            monetary_values=[{"amount": 1000000, "currency": "USD", "description": "cap"}],
        )
        _apply(clause, e)
        assert clause.clause_type == "indemnification"
        assert clause.risk_level == "high"
        assert clause.risk_rationale == "Broad unlimited indemnity"
        assert clause.obligor == "Supplier"
        assert clause.obligee == "Buyer"
        assert clause.parties_mentioned == ["Supplier", "Buyer"]
        assert clause.key_dates == {"expiry": "2026-12-31"}
        assert clause.monetary_values == [{"amount": 1000000, "currency": "USD", "description": "cap"}]

    def test_fallback_enrichment_sets_safe_defaults(self):
        clause = _clause(0)
        _apply(clause, ClauseEnrichmentResult())
        assert clause.clause_type == "general"
        assert clause.risk_level is None
        assert clause.parties_mentioned == []
        assert clause.key_dates == {}
        assert clause.monetary_values == []


# ── enrich_clauses_batch ───────────────────────────────────────────────────────

class TestEnrichClausesBatch:
    @pytest.fixture(autouse=True)
    def _mock_settings(self, monkeypatch):
        """Ensure settings has a valid GEMMA4_BASE_URL for all tests in this class
        (unless the test itself overrides it)."""
        monkeypatch.setattr("app.services.clause_enrichment_service.settings.GEMMA4_BASE_URL", "http://mock-gemma")
        monkeypatch.setattr("app.services.clause_enrichment_service.settings.GEMMA4_MODEL_NAME", "gemma4-test")
        monkeypatch.setattr("app.services.clause_enrichment_service.settings.GEMMA4_API_KEY", "")
        monkeypatch.setattr("app.services.clause_enrichment_service.settings.GEMMA4_TIMEOUT_SECONDS", 30)

    def test_empty_list_returns_same_empty(self):
        result = enrich_clauses_batch([])
        assert result == []

    def test_no_gemma_url_skips_enrichment(self, monkeypatch):
        monkeypatch.setattr("app.services.clause_enrichment_service.settings.GEMMA4_BASE_URL", "")
        clauses = [_clause(0)]
        result = enrich_clauses_batch(clauses)
        assert result[0].clause_type == "general"
        assert result[0].risk_level is None

    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_successful_single_clause(self, mock_chat):
        mock_chat.return_value = json.dumps([_enrichment_json()])
        clauses = [_clause(0)]
        result = enrich_clauses_batch(clauses)
        assert result is clauses  # same list, mutated in place
        assert result[0].clause_type == "obligation"
        assert result[0].risk_level == "medium"
        assert result[0].obligor == "Vendor"
        assert result[0].key_dates == {"due": "2025-06-30"}

    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_http_error_uses_fallback(self, mock_chat):
        mock_chat.side_effect = httpx.HTTPError("connection refused")

        clauses = [_clause(0)]
        result = enrich_clauses_batch(clauses)
        assert result[0].clause_type == "general"
        assert result[0].risk_level is None

    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_invalid_json_from_gemma_uses_fallback(self, mock_chat):
        mock_chat.return_value = "I cannot process this request."
        clauses = [_clause(0)]
        result = enrich_clauses_batch(clauses)
        assert result[0].clause_type == "general"

    @patch("app.services.clause_enrichment_service.time.sleep")
    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_retry_then_success(self, mock_chat, mock_sleep):
        call_count = [0]
        good = json.dumps([_enrichment_json(clause_type="warranty", risk_level="low",
                                            risk_rationale="r", obligor=None, obligee=None,
                                            parties_mentioned=[], key_dates={}, monetary_values=[])])

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                raise httpx.HTTPError("timeout")
            return good

        mock_chat.side_effect = side_effect

        clauses = [_clause(0)]
        result = enrich_clauses_batch(clauses)
        assert result[0].clause_type == "warranty"
        assert mock_sleep.called  # retry sleep happened

    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_seven_clauses_split_into_two_batches(self, mock_chat):
        """7 clauses → batch of 5 + batch of 2, both enriched."""
        good_5 = json.dumps([_enrichment_json(clause_type="obligation")] * 5)
        call_count = [0]

        def side_effect(*args, **kwargs):
            nonlocal_count = call_count
            # First batch call returns 5-item array, second returns 2-item array
            # (order non-deterministic due to threads; return a safe 5-item array always)
            call_count[0] += 1
            return good_5

        mock_chat.side_effect = side_effect

        clauses = [_clause(i) for i in range(7)]
        result = enrich_clauses_batch(clauses)
        assert len(result) == 7
        assert all(c.clause_type == "obligation" for c in result)
        assert call_count[0] == 2  # 2 batches

    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_one_batch_failure_does_not_affect_others(self, mock_chat):
        """Batch 0 fails, batch 1 succeeds → batch 0 clauses get fallback."""
        call_count = [0]
        good = json.dumps([_enrichment_json(clause_type="right")] * 5)

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise httpx.HTTPError("timeout")
            return good

        mock_chat.side_effect = side_effect

        clauses = [_clause(i) for i in range(10)]
        result = enrich_clauses_batch(clauses)
        assert len(result) == 10
        # Some clauses enriched ("right"), others fallback ("general") — both valid
        types = {c.clause_type for c in result}
        assert types <= {"right", "general"}
        assert "right" in types  # at least one successful batch

    @patch("app.services.clause_enrichment_service.gemma_client.chat")
    def test_returns_same_list_instance(self, mock_chat):
        mock_chat.return_value = json.dumps([_enrichment_json()])
        clauses = [_clause(0)]
        result = enrich_clauses_batch(clauses)
        assert result is clauses
