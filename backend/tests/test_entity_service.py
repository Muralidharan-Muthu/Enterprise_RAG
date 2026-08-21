from unittest.mock import patch

import app.services.entity_service as es


def test_canonicalize_lowercases_and_collapses_whitespace():
    assert es.canonicalize("  Acme   Corp  ") == "acme corp"
    assert es.canonicalize("ACME") == "acme"
    assert es.canonicalize("\tFoo\nBar ") == "foo bar"


def test_rule_based_extracts_capitalized_multiword_sequences():
    text = "Acme Corporation signed a deal with Globex Industries in London."
    ents = es._rule_based_entities(text, 20)
    names = {e["name"] for e in ents}
    assert "Acme Corporation" in names
    assert "Globex Industries" in names
    assert all(e["type"] == "unknown" for e in ents)


def test_rule_based_dedupes_by_canonical_form():
    text = "Acme Corp grew. ACME CORP also grew. acme corp again."
    ents = es._rule_based_entities(text, 20)
    keys = [es.canonicalize(e["name"]) for e in ents]
    assert keys.count("acme corp") == 1


def test_rule_based_drops_stoplist_and_sentence_starters():
    text = "The Company met. This Agreement applies. Section One follows."
    ents = es._rule_based_entities(text, 20)
    names = {e["name"] for e in ents}
    # bare stoplist tokens must not become entities
    assert "The" not in names
    assert "This" not in names
    assert "Section" not in names


def test_rule_based_respects_max():
    text = " ".join(f"Corp{n} Holdings" for n in range(50))
    ents = es._rule_based_entities(text, 5)
    assert len(ents) == 5


def test_extract_entities_empty_text_returns_empty():
    assert es.extract_entities("") == []
    assert es.extract_entities("   ") == []


def test_extract_entities_falls_back_when_no_endpoint():
    with patch.object(es.settings, "GROQ_BASE_URL", ""):
        ents = es.extract_entities("Acme Corporation and Globex Industries.")
    names = {e["name"] for e in ents}
    assert "Acme Corporation" in names


def test_parse_groq_entities_reads_json():
    raw = '```json\n{"entities": [{"name": "Acme Corp", "type": "org"}, {"name": "London", "type": "location"}]}\n```'
    out = es._parse_groq_entities(raw)
    assert {"name": "Acme Corp", "type": "org"} in out
    assert any(e["type"] == "location" for e in out)


def test_parse_groq_entities_dedupes_and_skips_blank():
    raw = '{"entities": [{"name": "Acme", "type": "org"}, {"name": "acme", "type": "org"}, {"name": "", "type": "x"}]}'
    out = es._parse_groq_entities(raw)
    assert len(out) == 1


def test_parse_groq_entities_bad_json_returns_none():
    assert es._parse_groq_entities("not json at all") is None


def test_parse_groq_entities_empty_list_is_valid_not_none():
    # Groq correctly finding zero entities is a valid answer, not a failure.
    out = es._parse_groq_entities('{"entities": []}')
    assert out == []
    assert out is not None


def test_extract_entities_accepts_legitimate_empty_Groq_result():
    # Regression: extract_entities used to check `if parsed:` (truthy), which is
    # False for both None (real failure) and [] (valid "no entities found"), so a
    # correct empty answer wrongly fell through to the noisy regex fallback and
    # invented pseudo-entities from capitalized phrases (e.g. "FY 2023-24") in
    # text that has no real named entities. It must now trust an explicit [].
    with patch.object(es.settings, "GROQ_BASE_URL", "http://x"), \
         patch.object(es, "_call_groq_ner", return_value=[]):
        ents = es.extract_entities("Q1 Planned Revenue was 950.00 for FY 2023-24.")
    assert ents == []


def test_extract_entities_falls_back_only_on_genuine_parse_failure():
    with patch.object(es.settings, "GROQ_BASE_URL", "http://x"), \
         patch.object(es, "_call_groq_ner", return_value=None):
        ents = es.extract_entities("Acme Corporation and Globex Industries.")
    names = {e["name"] for e in ents}
    assert "Acme Corporation" in names
