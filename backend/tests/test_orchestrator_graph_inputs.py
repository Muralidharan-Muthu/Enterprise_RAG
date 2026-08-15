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
        doc_meta, entities = orch._build_graph_inputs(parsed, "doc1", "f.pdf", "research")
    assert entities == []
    ex.assert_not_called()
