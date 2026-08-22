from types import SimpleNamespace
from unittest.mock import patch

import app.services.ingestion_orchestrator as orch


def test_build_graph_inputs_assembles_meta_and_entities():
    parsed = SimpleNamespace(raw_text="Acme Corporation partnered with Globex Industries.")
    fake = [{"name": "Acme Corporation", "type": "org"},
            {"name": "Globex Industries", "type": "org"}]
    with patch("app.services.entity_service.extract_entities", return_value=fake) as ex:
        doc_meta, entities = orch._build_graph_inputs(parsed, "doc1", "Acme.pdf", "legal")
    assert doc_meta == {"doc_id": "doc1", "filename": "Acme.pdf", "doc_type": "legal"}
    assert entities == fake
    # raw text was passed (capped) to the extractor
    ex.assert_called_once()
    assert ex.call_args[0][0].startswith("Acme Corporation")


def test_build_graph_inputs_empty_text_skips_extraction():
    parsed = SimpleNamespace(raw_text="   ")
    with patch("app.services.entity_service.extract_entities") as ex:
        doc_meta, entities = orch._build_graph_inputs(parsed, "doc1", "f.pdf", "policy")
    assert entities == []
    ex.assert_not_called()
    assert doc_meta["doc_id"] == "doc1"


def test_build_graph_inputs_caps_raw_text_length():
    parsed = SimpleNamespace(raw_text="A" * 50000)
    with patch("app.services.entity_service.extract_entities", return_value=[]) as ex:
        orch._build_graph_inputs(parsed, "doc1", "f.pdf", "policy")
    assert len(ex.call_args[0][0]) == 20000


def test_build_graph_inputs_missing_raw_text_attr():
    parsed = SimpleNamespace()  # no raw_text
    with patch("app.services.entity_service.extract_entities") as ex:
        doc_meta, entities = orch._build_graph_inputs(parsed, "doc1", "f.pdf", "financial")
    assert entities == []
    ex.assert_not_called()


def test_assemble_chunk_records_with_clauses_and_chunks():
    from app.services.graph_build_service import assemble_chunk_records
    from app.models.document import LegalClause, Chunk

    parsed_doc = SimpleNamespace(tables=[])
    clauses = [
        LegalClause(
            clause_index=0,
            clause_number="1",
            clause_title="TERMINATION FOR CAUSE",
            clause_text="Immediate termination rights upon bankruptcy.",
            clause_type="termination",
            risk_level="high",
            risk_rationale=None,
            obligor="Vendor",
            obligee="RIL",
            parties_mentioned=["Vendor", "RIL"],
            key_dates=[],
            monetary_values=[],
            page_number=7,
            page_numbers=[7],
            section_path=["Legal"],
            clause_metadata={},
        )
    ]
    chunks = [
        Chunk(
            chunk_index=0,
            chunk_text="Financial overview for Q3.",
            page_number=1,
            page_numbers=[1],
            section_title="Financial Highlights",
            section_level=1,
            semantic_type="paragraph",
            keywords=["revenue"],
            token_count=10,
        )
    ]
    stored_ids = {
        "clause_store": ["clause-uuid-1"],
        "vector_store": ["vector-uuid-1"],
    }
    router_res = SimpleNamespace(document_type="financial, legal")

    records = assemble_chunk_records(parsed_doc, chunks, clauses, router_res, stored_ids)
    assert len(records) == 2
    assert records[0]["store"] == "clause_store"
    assert records[0]["pg_id"] == "clause-uuid-1"
    assert records[0]["text"] == "Immediate termination rights upon bankruptcy."
    assert records[1]["store"] == "vector_store"
    assert records[1]["pg_id"] == "vector-uuid-1"
    assert records[1]["text"] == "Financial overview for Q3."


def test_safe_rel_type_sanitizer():
    from app.services.graph_service import _safe_rel_type

    assert _safe_rel_type("owns") == "OWNS"
    assert _safe_rel_type("governed by") == "GOVERNED_BY"
    assert _safe_rel_type("TERMINATES") == "TERMINATES"
    assert _safe_rel_type("subject to (law)") == "SUBJECT_TO_LAW"
    assert _safe_rel_type("123bad") == "RELATES_TO"
    assert _safe_rel_type("") == "RELATES_TO"


def test_parse_graph_response():
    from app.services.graph_extraction_service import _parse_graph_response

    raw_json = """
    {
      "entities": [
        {"name": "Reliance Industries", "type": "organization", "description": "Indian conglomerate"},
        {"name": "Mumbai", "type": "location", "description": "Exclusive jurisdiction city"}
      ],
      "relationships": [
        {"source": "Reliance Industries", "target": "Mumbai", "type": "RESOLVES_DISPUTES_IN", "description": "Contracts designate Mumbai courts"}
      ]
    }
    """
    res = _parse_graph_response(raw_json)
    assert res is not None
    assert len(res["entities"]) == 2
    assert len(res["relationships"]) == 1
    assert res["relationships"][0]["type"] == "RESOLVES_DISPUTES_IN"
    assert res["relationships"][0]["source"] == "Reliance Industries"
    assert res["relationships"][0]["target"] == "Mumbai"
