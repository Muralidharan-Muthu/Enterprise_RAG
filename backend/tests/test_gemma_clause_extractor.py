"""Unit tests for gemma_clause_extractor (Gemma-first legal clause extraction)."""
import json
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.models.document import LegalClause, ParsedDocument, TextBlock
from app.services.gemma_clause_extractor import (
    ExtractionMeta,
    MAX_RETRIES,
    MAX_SEGMENT_CHARS,
    MIN_SEGMENT_COVERAGE,
    _GemmaClause,
    _build_segments,
    _dedup_pairs,
    _extract_segment,
    _fingerprint,
    _is_continuation,
    _parse_gemma_response,
    _retry_low_coverage_segment,
    _segment_coverage,
    _stitch_continuations,
    _to_legal_clause,
    extract_clauses_gemma,
)


# ── Test fixtures ──────────────────────────────────────────────────────────────

def _make_doc(raw_text: str = "", blocks: Optional[list] = None) -> ParsedDocument:
    return ParsedDocument(
        doc_id="test-doc",
        filename="contract.pdf",
        raw_text=raw_text,
        text_blocks=blocks or [],
        tables=[],
        page_count=1,
        word_count=len(raw_text.split()),
        has_tables=False,
        has_images=False,
    )


def _make_block(text: str, page: int = 1) -> TextBlock:
    return TextBlock(text=text, page_number=page, block_type="paragraph")


def _make_gemma_clause(**overrides) -> _GemmaClause:
    defaults = {
        "clause_number": "1.1",
        "clause_title": "Payment Terms",
        "clause_text": "Customer shall pay all invoices within 30 days of receipt.",
        "clause_type": "obligation",
        "risk_level": "medium",
        "risk_rationale": "Creates payment obligation",
        "obligor": "Customer",
        "obligee": "Vendor",
        "parties_mentioned": ["Customer", "Vendor"],
        "key_dates": {"due": "2025-06-30"},
        "monetary_values": [{"amount": 10000, "currency": "USD", "description": "invoice"}],
    }
    defaults.update(overrides)
    return _GemmaClause(**defaults)


def _gemma_response(clauses: list[dict]) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"clauses": clauses})}}]}


def _mock_client(response_json: dict):
    resp = MagicMock()
    resp.json.return_value = response_json
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = resp
    return client


def _clause_dict(**overrides) -> dict:
    base = {
        "clause_number": "2.1",
        "clause_title": "Confidentiality",
        "clause_text": "Each party shall keep the other's information confidential.",
        "clause_type": "confidentiality",
        "risk_level": "high",
        "risk_rationale": "Breach exposes sensitive data",
        "obligor": "Both parties",
        "obligee": "Both parties",
        "parties_mentioned": ["Party A", "Party B"],
        "key_dates": {},
        "monetary_values": [],
    }
    base.update(overrides)
    return base


# ── _build_segments ────────────────────────────────────────────────────────────

class TestBuildSegments:
    def test_empty_raw_text_returns_one_empty_segment(self):
        doc = _make_doc("")
        segs = _build_segments(doc)
        assert len(segs) == 1
        assert segs[0][0] == ""

    def test_short_raw_text_single_segment(self):
        text = "This is clause 1.\n\nThis is clause 2."
        doc = _make_doc(text)
        segs = _build_segments(doc)
        assert len(segs) == 1
        assert "clause 1" in segs[0][0]
        assert "clause 2" in segs[0][0]

    def test_long_raw_text_splits_into_multiple_segments(self):
        chunk = "A" * 1000 + "\n\n"
        raw = chunk * 20  # 20 000 chars — well over MAX_SEGMENT_CHARS
        doc = _make_doc(raw)
        segs = _build_segments(doc)
        assert len(segs) >= 2
        for text, _ in segs:
            assert len(text) <= MAX_SEGMENT_CHARS + 2000  # overlap may push slightly over

    def test_uses_text_blocks_when_available(self):
        blocks = [_make_block(f"Block {i}", page=i + 1) for i in range(5)]
        doc = _make_doc(blocks=blocks)
        segs = _build_segments(doc)
        assert len(segs) >= 1
        assert "Block 0" in segs[0][0]

    def test_text_blocks_page_numbers_tracked(self):
        blocks = [
            _make_block("A" * 500, page=1),
            _make_block("B" * 500, page=2),
        ]
        doc = _make_doc(blocks=blocks)
        segs = _build_segments(doc)
        # start_page of first segment should be 1
        assert segs[0][1] == 1

    def test_large_block_list_creates_overlap(self):
        long_block = "X" * 3000
        blocks = [_make_block(long_block, page=i + 1) for i in range(5)]
        doc = _make_doc(blocks=blocks)
        segs = _build_segments(doc)
        assert len(segs) >= 2
        # Overlap: last 2 blocks of seg N appear in seg N+1
        last_text_seg0 = segs[0][0].split("\n\n")[-1]
        first_text_seg1 = segs[1][0].split("\n\n")[0]
        assert last_text_seg0 == first_text_seg1


# ── _parse_gemma_response ──────────────────────────────────────────────────────

class TestParseGemmaResponse:
    def test_valid_json_with_clauses(self):
        raw = json.dumps({"clauses": [_clause_dict()]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].clause_type == "confidentiality"
        assert result[0].risk_level == "high"
        assert result[0].parties_mentioned == ["Party A", "Party B"]

    def test_markdown_fenced_json(self):
        raw = "```json\n" + json.dumps({"clauses": [_clause_dict()]}) + "\n```"
        result = _parse_gemma_response(raw)
        assert result is not None
        assert result[0].clause_type == "confidentiality"

    def test_empty_clauses_list_is_valid(self):
        raw = json.dumps({"clauses": []})
        result = _parse_gemma_response(raw)
        assert result == []

    def test_missing_clause_text_skips_item(self):
        raw = json.dumps({"clauses": [{"clause_type": "general"}]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert len(result) == 0

    def test_invalid_clause_type_coerced_to_general(self):
        raw = json.dumps({"clauses": [_clause_dict(clause_type="magic_type")]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert result[0].clause_type == "general"

    def test_invalid_risk_level_coerced_to_none(self):
        raw = json.dumps({"clauses": [_clause_dict(risk_level="critical")]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert result[0].risk_level is None

    def test_null_optional_fields_accepted(self):
        raw = json.dumps({"clauses": [
            _clause_dict(clause_number=None, clause_title=None, obligor=None,
                         obligee=None, risk_level=None, risk_rationale=None)
        ]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert result[0].clause_number is None
        assert result[0].obligor is None

    def test_invalid_json_returns_none(self):
        assert _parse_gemma_response("not json") is None
        assert _parse_gemma_response("") is None

    def test_json_array_not_object_returns_none(self):
        assert _parse_gemma_response(json.dumps([_clause_dict()])) is None

    def test_object_without_clauses_key_returns_none(self):
        assert _parse_gemma_response(json.dumps({"data": []})) is None

    def test_recovers_embedded_json_object(self):
        embedded = json.dumps({"clauses": [_clause_dict()]})
        raw = f"Here is the extraction result:\n{embedded}\nEnd."
        result = _parse_gemma_response(raw)
        assert result is not None
        assert len(result) == 1

    def test_non_dict_items_in_list_skipped(self):
        raw = json.dumps({"clauses": ["bad", _clause_dict(), None]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].clause_type == "confidentiality"

    def test_multiple_valid_clauses(self):
        raw = json.dumps({"clauses": [_clause_dict(), _clause_dict(clause_number="3.1", clause_type="termination")]})
        result = _parse_gemma_response(raw)
        assert result is not None
        assert len(result) == 2
        assert result[1].clause_type == "termination"


# ── _fingerprint + _dedup_pairs ────────────────────────────────────────────────

class TestFingerprintAndDedup:
    def test_fingerprint_uses_clause_number_when_present(self):
        gc = _make_gemma_clause(clause_number="12.3.1")
        assert _fingerprint(gc) == "num:12.3.1"

    def test_fingerprint_falls_back_to_text(self):
        gc = _make_gemma_clause(clause_number=None, clause_title=None,
                                 clause_text="The Vendor shall deliver goods.")
        fp = _fingerprint(gc)
        assert fp.startswith("txt:")
        assert "vendor shall deliver" in fp

    def test_fingerprint_normalizes_whitespace(self):
        gc1 = _make_gemma_clause(clause_number=None, clause_text="A  B  C")
        gc2 = _make_gemma_clause(clause_number=None, clause_text="A B C")
        assert _fingerprint(gc1) == _fingerprint(gc2)

    def test_dedup_removes_same_clause_number(self):
        gc = _make_gemma_clause(clause_number="1.1")
        pairs = [(gc, 1), (gc, 2), (gc, 3)]
        result = _dedup_pairs(pairs)
        assert len(result) == 1

    def test_dedup_keeps_different_clause_numbers(self):
        gc1 = _make_gemma_clause(clause_number="1.1")
        gc2 = _make_gemma_clause(clause_number="1.2")
        gc3 = _make_gemma_clause(clause_number="2.1")
        result = _dedup_pairs([(gc1, 1), (gc2, 1), (gc3, 2)])
        assert len(result) == 3

    def test_dedup_preserves_first_occurrence_page(self):
        gc = _make_gemma_clause(clause_number="1.1")
        pairs = [(gc, 5), (gc, 6)]
        result = _dedup_pairs(pairs)
        assert result[0][1] == 5  # first segment's page wins

    def test_dedup_empty_list(self):
        assert _dedup_pairs([]) == []


# ── _to_legal_clause ───────────────────────────────────────────────────────────

class TestToLegalClause:
    def test_all_fields_mapped(self):
        gc = _make_gemma_clause()
        clause = _to_legal_clause(0, gc, pages=[3])
        assert clause.clause_index == 0
        assert clause.clause_text == gc.clause_text
        assert clause.clause_number == "1.1"
        assert clause.clause_title == "Payment Terms"
        assert clause.page_number == 3
        assert clause.page_numbers == [3]
        assert clause.section_path == ["1.1"]
        assert clause.clause_type == "obligation"
        assert clause.risk_level == "medium"
        assert clause.risk_rationale == "Creates payment obligation"
        assert clause.obligor == "Customer"
        assert clause.obligee == "Vendor"
        assert clause.parties_mentioned == ["Customer", "Vendor"]
        assert clause.key_dates == {"due": "2025-06-30"}
        assert clause.monetary_values == [{"amount": 10000, "currency": "USD", "description": "invoice"}]

    def test_no_clause_number_gives_empty_section_path(self):
        gc = _make_gemma_clause(clause_number=None)
        clause = _to_legal_clause(0, gc, pages=[1])
        assert clause.section_path == []

    def test_index_set_correctly(self):
        gc = _make_gemma_clause()
        for i in range(5):
            assert _to_legal_clause(i, gc, pages=[1]).clause_index == i

    def test_multiple_pages_sorted_and_deduped(self):
        gc = _make_gemma_clause()
        clause = _to_legal_clause(0, gc, pages=[4, 3, 4])
        assert clause.page_number == 4          # first page passed in, unsorted
        assert clause.page_numbers == [3, 4]     # sorted + deduped for display


# ── _segment_coverage / _retry_low_coverage_segment ─────────────────────────────

class TestSegmentCoverage:
    def test_full_coverage_when_clause_text_matches_segment(self):
        text = "This is the entire segment content."
        clause = _make_gemma_clause(clause_text=text)
        assert _segment_coverage(text, [clause]) == 1.0

    def test_empty_segment_is_full_coverage_trivially(self):
        assert _segment_coverage("", []) == 1.0

    def test_low_coverage_when_clauses_account_for_little_text(self):
        """Reproduces the reported bug: a dense segment (Section A heading + 4
        sibling policy statements) where only the LAST item survived extraction —
        the returned clause accounts for a small fraction of the segment."""
        segment = (
            "Section A - Technology Policy Statements\n\n"
            "Data Retention Policy\nNovaTech Solutions and its contracted clients "
            "shall adhere to the following data retention framework...\n\n"
            "AI Model Governance Policy\nAll AI and machine learning models deployed "
            "by NovaTech within client environments shall be subject to...\n\n"
            "Cloud Security Policy\nNovaTech's managed cloud environments are "
            "operated in accordance with ISO/IEC 27001:2022...\n\n"
            "Vendor Management Policy\nNovaTech maintains a tiered vendor "
            "management programme governing all third-party technology suppliers."
        )
        only_last_item = _make_gemma_clause(
            clause_number=None, clause_title="Vendor Management Policy",
            clause_text="NovaTech maintains a tiered vendor management programme "
                        "governing all third-party technology suppliers.",
        )
        coverage = _segment_coverage(segment, [only_last_item])
        assert coverage < MIN_SEGMENT_COVERAGE

    def test_capped_at_one_when_clause_text_exceeds_segment_length(self):
        """A clause_text slightly reworded/longer than the raw segment (Gemma
        lightly paraphrasing) must not produce a coverage ratio above 1.0."""
        clause = _make_gemma_clause(clause_text="A" * 200)
        assert _segment_coverage("A" * 100, [clause]) == 1.0


class TestRetryLowCoverageSegment:
    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_merges_first_and_second_pass(self, mock_cls):
        first = [_make_gemma_clause(clause_number=None, clause_title="Vendor Management Policy")]
        second_response = _gemma_response([
            _clause_dict(clause_number=None, clause_title="Section A - Technology Policy Statements",
                         clause_text="Section A intro text."),
            _clause_dict(clause_number=None, clause_title="Data Retention Policy",
                         clause_text="NovaTech shall retain financial records for seven years."),
            _clause_dict(clause_number=None, clause_title="AI Model Governance Policy",
                         clause_text="All AI models deployed by NovaTech require human review."),
            _clause_dict(clause_number=None, clause_title="Cloud Security Policy",
                         clause_text="NovaTech's cloud environments follow ISO/IEC 27001:2022."),
        ])
        mock_cls.return_value = _mock_client(second_response)
        result = _retry_low_coverage_segment("segment text", start_page=12, first_pass=first)
        assert result is not None
        titles = {c.clause_title for c in result}
        assert titles == {
            "Vendor Management Policy", "Section A - Technology Policy Statements",
            "Data Retention Policy", "AI Model Governance Policy", "Cloud Security Policy",
        }

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_dedups_items_present_in_both_passes(self, mock_cls):
        first = [_make_gemma_clause(clause_number="4", clause_title="IP Rights")]
        second_response = _gemma_response([
            _clause_dict(clause_number="4", clause_title="IP Rights"),
            _clause_dict(clause_number=None, clause_title="Missed Heading"),
        ])
        mock_cls.return_value = _mock_client(second_response)
        result = _retry_low_coverage_segment("segment text", start_page=1, first_pass=first)
        assert len(result) == 2  # clause 4 not duplicated

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_returns_none_when_retry_also_finds_nothing(self, mock_cls):
        mock_cls.return_value = _mock_client(_gemma_response([]))
        first = [_make_gemma_clause()]
        assert _retry_low_coverage_segment("segment text", start_page=1, first_pass=first) is None

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_returns_none_when_retry_call_raises(self, mock_cls):
        mock_cls.side_effect = httpx.ConnectError("endpoint down")
        first = [_make_gemma_clause()]
        assert _retry_low_coverage_segment("segment text", start_page=1, first_pass=first) is None


class TestExtractSegmentCoverageRetry:
    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_low_coverage_triggers_retry_and_returns_merged_result(self, mock_cls):
        segment = "X" * 2000  # long segment, first pass covers almost none of it
        thin_first_pass = _gemma_response([_clause_dict(clause_text="short bit")])
        fuller_second_pass = _gemma_response([
            _clause_dict(clause_number=None, clause_title="Missed Heading 1", clause_text="Y" * 900),
            _clause_dict(clause_number=None, clause_title="Missed Heading 2", clause_text="Z" * 900),
        ])
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        resp1, resp2 = MagicMock(), MagicMock()
        resp1.json.return_value = thin_first_pass
        resp1.raise_for_status = MagicMock()
        resp2.json.return_value = fuller_second_pass
        resp2.raise_for_status = MagicMock()
        client.post.side_effect = [resp1, resp2]
        mock_cls.return_value = client

        result = _extract_segment(segment, start_page=12, seg_idx=0)
        assert client.post.call_count == 2  # first pass + corrective retry
        titles = {c.clause_title for c in result}
        assert "Missed Heading 1" in titles and "Missed Heading 2" in titles

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_good_coverage_does_not_trigger_retry(self, mock_cls):
        segment = "Short segment text."
        mock_cls.return_value = _mock_client(
            _gemma_response([_clause_dict(clause_text=segment)])
        )
        _extract_segment(segment, start_page=1, seg_idx=0)
        assert mock_cls.return_value.post.call_count == 1  # no corrective retry


# ── _is_continuation ───────────────────────────────────────────────────────────

class TestIsContinuation:
    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_true_when_gemma_says_continuation(self, mock_cls):
        mock_cls.return_value = _mock_client(
            {"choices": [{"message": {"content": '{"continuation": true}'}}]}
        )
        assert _is_continuation("...provided that NovaTech", "shall not incorporate...") is True

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_false_when_gemma_says_not_continuation(self, mock_cls):
        mock_cls.return_value = _mock_client(
            {"choices": [{"message": {"content": '{"continuation": false}'}}]}
        )
        assert _is_continuation("...end of clause 4.", "5. A brand new clause.") is False

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_fails_open_to_true_on_http_error(self, mock_cls):
        mock_cls.side_effect = httpx.ConnectError("endpoint down")
        assert _is_continuation("tail text", "fragment text") is True

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_fails_open_to_true_on_unparseable_response(self, mock_cls):
        mock_cls.return_value = _mock_client(
            {"choices": [{"message": {"content": "I'm not sure, maybe?"}}]}
        )
        assert _is_continuation("tail text", "fragment text") is True


# ── _stitch_continuations ───────────────────────────────────────────────────────

class TestStitchContinuations:
    def test_empty_list_returns_empty(self):
        assert _stitch_continuations([]) == []

    def test_no_orphans_leaves_pairs_unchanged(self):
        gc1 = _make_gemma_clause(clause_number="1", clause_title="A")
        gc2 = _make_gemma_clause(clause_number="2", clause_title="B")
        result = _stitch_continuations([(gc1, 1), (gc2, 2)])
        assert result == [(gc1, [1]), (gc2, [2])]

    @patch("app.services.gemma_clause_extractor._is_continuation", return_value=True)
    def test_orphan_fragment_merged_into_preceding_clause(self, mock_check):
        """Reproduces the reported bug: clause 4's tail lands in its own segment as
        an unlabeled fragment (its heading separated by page-footer/header noise).
        Confirmed a continuation -> merged back, not left as its own row."""
        gc4 = _make_gemma_clause(
            clause_number="4", clause_title="Intellectual Property Rights",
            clause_text="All pre-existing intellectual property... provided that NovaTech",
        )
        orphan = _make_gemma_clause(
            clause_number=None, clause_title=None,
            clause_text="shall not incorporate any Confidential Information of Meridian "
                        "into any generally available product or service without prior "
                        "written consent.",
        )
        result = _stitch_continuations([(gc4, 3), (orphan, 4)])
        assert len(result) == 1
        merged_gc, pages = result[0]
        assert merged_gc is gc4
        assert pages == [3, 4]
        assert merged_gc.clause_text == (
            "All pre-existing intellectual property... provided that NovaTech "
            "shall not incorporate any Confidential Information of Meridian into "
            "any generally available product or service without prior written consent."
        )
        mock_check.assert_called_once()

    @patch("app.services.gemma_clause_extractor._is_continuation", return_value=False)
    def test_orphan_fragment_kept_separate_when_not_a_continuation(self, mock_check):
        """A genuinely freestanding unlabeled clause (Gemma says no) must NOT be
        merged into an unrelated preceding clause."""
        gc1 = _make_gemma_clause(clause_number="1", clause_title="Definitions")
        orphan = _make_gemma_clause(
            clause_number=None, clause_title=None,
            clause_text="This preamble text has no number or heading of its own.",
        )
        result = _stitch_continuations([(gc1, 1), (orphan, 2)])
        assert len(result) == 2
        assert result[1] == (orphan, [2])

    def test_leading_orphan_with_no_preceding_clause_kept_as_is(self):
        orphan = _make_gemma_clause(clause_number=None, clause_title=None)
        result = _stitch_continuations([(orphan, 1)])
        assert result == [(orphan, [1])]

    @patch("app.services.gemma_clause_extractor._is_continuation", return_value=True)
    def test_multiple_consecutive_orphans_all_merge_into_same_clause(self, mock_check):
        """A clause split across 3+ segments (rare, but possible for very long
        clauses) should still collapse into a single row."""
        gc1 = _make_gemma_clause(clause_number="1", clause_title="A", clause_text="Start.")
        frag_a = _make_gemma_clause(clause_number=None, clause_title=None, clause_text="middle part one.")
        frag_b = _make_gemma_clause(clause_number=None, clause_title=None, clause_text="middle part two.")
        result = _stitch_continuations([(gc1, 1), (frag_a, 2), (frag_b, 3)])
        assert len(result) == 1
        merged_gc, pages = result[0]
        assert pages == [1, 2, 3]
        assert merged_gc.clause_text == "Start. middle part one. middle part two."

    @patch("app.services.gemma_clause_extractor._is_continuation")
    def test_titled_orphan_never_triggers_continuation_check(self, mock_check):
        """A clause WITH a number or title is never treated as an orphan fragment,
        even if it happens to follow another clause — no Gemma call needed."""
        gc1 = _make_gemma_clause(clause_number="1", clause_title="A")
        gc2 = _make_gemma_clause(clause_number="2", clause_title="B")
        result = _stitch_continuations([(gc1, 1), (gc2, 2)])
        assert len(result) == 2
        mock_check.assert_not_called()


# ── extract_clauses_gemma (integration-level, mocked HTTP) ────────────────────

@pytest.fixture(autouse=False)
def gemma_settings(monkeypatch):
    monkeypatch.setattr("app.services.gemma_clause_extractor.settings.GEMMA4_BASE_URL", "http://mock-gemma")
    monkeypatch.setattr("app.services.gemma_clause_extractor.settings.GEMMA4_MODEL_NAME", "gemma4-test")
    monkeypatch.setattr("app.services.gemma_clause_extractor.settings.GEMMA4_API_KEY", "")
    monkeypatch.setattr("app.services.gemma_clause_extractor.settings.GEMMA4_TIMEOUT_SECONDS", 30)


class TestExtractClausesGemma:
    @pytest.fixture(autouse=True)
    def _settings(self, gemma_settings):
        pass

    def test_no_gemma_url_returns_regex_fallback(self, monkeypatch):
        monkeypatch.setattr("app.services.gemma_clause_extractor.settings.GEMMA4_BASE_URL", "")
        doc = _make_doc("Clause 1 text.\n\nClause 2 text.")
        with patch("app.services.chunker.extract_legal_clauses", return_value=[]) as mock_regex:
            clauses, meta = extract_clauses_gemma(doc)
            assert meta.source == "regex"
            assert meta.fallback_reason is not None
            mock_regex.assert_called_once()

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_successful_single_segment(self, mock_cls):
        mock_cls.return_value = _mock_client(_gemma_response([_clause_dict()]))
        doc = _make_doc("Each party shall keep the other's information confidential.")
        clauses, meta = extract_clauses_gemma(doc)
        assert meta.source == "gemma"
        assert len(clauses) == 1
        assert clauses[0].clause_type == "confidentiality"
        assert clauses[0].risk_level == "high"
        assert meta.failed_segments == 0

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_full_metadata_populated(self, mock_cls):
        mock_cls.return_value = _mock_client(_gemma_response([_clause_dict(
            clause_number="5.1",
            clause_title="Indemnity",
            clause_type="indemnification",
            risk_level="high",
            risk_rationale="Unlimited liability exposure",
            obligor="Supplier",
            obligee="Buyer",
            parties_mentioned=["Supplier", "Buyer"],
            key_dates={"effective": "2025-01-01"},
            monetary_values=[{"amount": 5000000, "currency": "USD", "description": "cap"}],
        )]))
        doc = _make_doc("Supplier shall indemnify Buyer against all claims.")
        clauses, meta = extract_clauses_gemma(doc)
        c = clauses[0]
        assert c.clause_number == "5.1"
        assert c.clause_title == "Indemnity"
        assert c.clause_type == "indemnification"
        assert c.risk_level == "high"
        assert c.obligor == "Supplier"
        assert c.key_dates == {"effective": "2025-01-01"}
        assert c.monetary_values[0]["amount"] == 5000000

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_dedup_removes_overlap_duplicates(self, mock_cls):
        """Two segments return the same clause (overlap) → dedup keeps only one."""
        c = _clause_dict(clause_number="1.1")
        mock_cls.return_value = _mock_client(_gemma_response([c]))
        # Build a doc with 2 segments (force by making text long)
        long = "A" * 5000 + "\n\n" + "B" * 5000
        doc = _make_doc(long)
        clauses, meta = extract_clauses_gemma(doc)
        clause_numbers = [cl.clause_number for cl in clauses if cl.clause_number == "1.1"]
        assert len(clause_numbers) == 1  # deduped

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_http_error_all_segments_triggers_regex_fallback(self, mock_cls):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = httpx.HTTPError("connection refused")
        mock_cls.return_value = client

        doc = _make_doc("Clause A.\n\nClause B.")
        with patch("app.services.chunker.extract_legal_clauses", return_value=[]) as mock_regex:
            _, meta = extract_clauses_gemma(doc)
        assert meta.source == "regex"
        mock_regex.assert_called_once()

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_invalid_json_all_segments_triggers_regex_fallback(self, mock_cls):
        resp = MagicMock()
        resp.json.return_value = {"choices": [{"message": {"content": "not valid json"}}]}
        resp.raise_for_status = MagicMock()
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = resp
        mock_cls.return_value = client

        doc = _make_doc("Clause text here.")
        with patch("app.services.chunker.extract_legal_clauses", return_value=[]) as mock_regex:
            _, meta = extract_clauses_gemma(doc)
        assert meta.source == "regex"
        mock_regex.assert_called_once()

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_zero_clauses_extracted_triggers_fallback(self, mock_cls):
        """Gemma returns empty clauses list → below MIN_CLAUSES_EXPECTED → fallback."""
        mock_cls.return_value = _mock_client(_gemma_response([]))
        doc = _make_doc("Some legal text here.")
        with patch("app.services.chunker.extract_legal_clauses", return_value=[]) as mock_regex:
            _, meta = extract_clauses_gemma(doc)
        assert meta.source == "regex"
        assert "0" in meta.fallback_reason  # mentions the count

    @patch("app.services.gemma_clause_extractor.time.sleep")
    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_partial_segment_failure_below_threshold_continues(self, mock_cls, _mock_sleep):
        """1 of 2 segments fails → 50% ≤ MAX_FAILED_RATIO → use successful results."""
        call_count = [0]
        good = _gemma_response([_clause_dict(clause_number="1.1")])

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= (MAX_RETRIES + 1):  # first segment always fails
                raise httpx.HTTPError("timeout")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = good
            return resp

        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = side_effect
        mock_cls.return_value = client

        # 2-segment doc: first fails, second succeeds → 50% failed, not > 50%
        long = "A" * 5000 + "\n\n" + "B" * 5000
        doc = _make_doc(long)
        clauses, meta = extract_clauses_gemma(doc)
        assert meta.failed_segments == 1
        assert meta.segment_count == 2
        # 50% failure is NOT > MAX_FAILED_RATIO (0.5 > 0.5 is False) → use Gemma result
        assert meta.source == "gemma"
        assert len(clauses) >= 1

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_page_numbers_from_segment_start_page(self, mock_cls):
        mock_cls.return_value = _mock_client(_gemma_response([_clause_dict()]))
        blocks = [_make_block("Legal clause content here.", page=7)]
        doc = _make_doc(blocks=blocks)
        clauses, _ = extract_clauses_gemma(doc)
        assert clauses[0].page_number == 7
        assert clauses[0].page_numbers == [7]

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_meta_extracted_count_matches_clauses(self, mock_cls):
        mock_cls.return_value = _mock_client(_gemma_response([
            _clause_dict(clause_number="1.1"),
            _clause_dict(clause_number="1.2", clause_type="termination"),
        ]))
        doc = _make_doc("Short document.")
        clauses, meta = extract_clauses_gemma(doc)
        assert meta.extracted_count == len(clauses)
        assert meta.source == "gemma"

    @patch("app.services.gemma_clause_extractor.httpx.Client")
    def test_regex_fallback_enrichment_path_in_orchestrator(self, mock_cls):
        """When source='regex', meta carries the fallback_reason so caller knows to enrich."""
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = httpx.HTTPError("timeout")
        mock_cls.return_value = client

        from app.models.document import LegalClause
        regex_clause = LegalClause(
            clause_index=0, clause_text="Confidential.", clause_number="1",
            clause_title="NDA", page_number=1, page_numbers=[1], section_path=[],
        )
        doc = _make_doc("Confidential.")
        with patch("app.services.chunker.extract_legal_clauses",
                   return_value=[regex_clause]):
            clauses, meta = extract_clauses_gemma(doc)
        assert meta.source == "regex"
        assert meta.fallback_reason is not None
        # Regex clause has only structural fields — enrichment fields at defaults
        assert clauses[0].clause_type == "general"
        assert clauses[0].risk_level is None
