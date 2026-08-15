"""Classifies a query's *shape* for table retrieval routing.

Six categories:
  semantic_qa   -> unchanged agentic/classic ANN pipeline
  exact_lookup  -> Structured Query Engine (single best row)
  filter        -> Structured Query Engine (ALL matching rows, exhaustive)
  aggregation   -> Structured Query Engine (SUM/AVG/COUNT/MIN/MAX, optionally GROUP BY)
  ranking       -> Structured Query Engine (top/bottom N by a column)
  mixed         -> Structured Query Engine for the row set, then synthesis
                    summarizes/compares over those exact rows

Pure regex/keyword — no LLM call, deliberately fast and deterministic so
it's safe to run on every query (same design philosophy as
table_query_engine.py's existing aggregate/lookup/list detectors).
Intentionally self-contained (no imports from table_query_engine.py) to
avoid a circular import, since table_query_engine.py calls into this
module — some pattern overlap with table_query_engine's regexes is
acceptable here since these are cheap "does this look like X" checks, not
the actual extraction logic.
"""
import re

from app.services.table_condition_parser import parse_filter

_MIXED_VERBS_RE = re.compile(
    r"\b(summariz|explain|compare|trend|analy[sz]e|discuss|describe|overview)\w*\b",
    re.IGNORECASE,
)

_LOOKUP_RE = re.compile(
    r"\bwhat\s+is\s+the\s+(.+?)\s+(?:value\s+)?(?:for|of|where)\s+(.+?)\s*[\?\.]?$",
    re.IGNORECASE,
)

# Attribute-lookup phrasing routed to exact_lookup — kept in sync with
# table_query_engine._ATTR_LOOKUP_RE (modules stay import-independent to avoid
# a cycle, so the pattern is intentionally duplicated). "which sector does
# <entity> belong to", "what industry is <entity> in", etc.
_ATTR_LOOKUP_RE = re.compile(
    r"\b(?:which|what)\s+(.+?)\s+(?:do(?:es)?|is|are|did)\s+(.+?)"
    r"\s+(?:(?:belong|fall|come|classif\w*|categor\w*|includ\w*|plac\w*|"
    r"group\w*|operat\w*|list\w*)\s*)?"
    r"(?:to|under|in|into|as|within)\s*[\?\.]?$",
    re.IGNORECASE,
)

# Natural subject-first variants: "HDFC Bank is what sector?" and
# "HDFC Bank belongs to which sector?".
_SUBJECT_FIRST_ATTR_LOOKUP_RE = re.compile(
    r"^\s*(.+?)\s+(?:(?:is\s+)?belong\w*\s+to|is\s+(?:in|under|within))\s+"
    r"(?:which|what)\s+(.+?)\s*[\?\.]?$|"
    r"^\s*(.+?)\s+is\s+(?:which|what)\s+(.+?)\s*[\?\.]?$",
    re.IGNORECASE,
)

_RANKING_RE = re.compile(
    r"\btop\s+\d+\b|\bbottom\s+\d+\b|\b(largest|highest|greatest|smallest|lowest|least)\b",
    re.IGNORECASE,
)

_FILTER_RE = re.compile(
    r"\b(list|show|display|enumerate|find)\b.*\b(all|every|out)\b|\bwhich\b.+\bare\b|\bwhat\s+are\b",
    re.IGNORECASE,
)

_AGGREGATE_RE = re.compile(
    r"\b(total|sum(?:\s+of)?|average|mean|how\s+many|count(?:\s+of)?|number\s+of)\b",
    re.IGNORECASE,
)


def _has_filter_clause(query: str) -> bool:
    """True when the condition parser can extract at least one leaf
    condition from the query — used to distinguish "compare all Chemical
    companies" (mixed: semantic verb + filter) from "compare X and Y"
    (semantic_qa: semantic verb, no filter)."""
    parsed = parse_filter(query)
    return parsed.tree is not None


def classify_table_intent(query: str) -> str:
    q = query.strip()
    if not q:
        return "semantic_qa"

    has_mixed_verb = bool(_MIXED_VERBS_RE.search(q))
    has_filter = bool(_FILTER_RE.search(q)) or _has_filter_clause(q)

    if has_mixed_verb and has_filter:
        return "mixed"

    if (_LOOKUP_RE.search(q) or _ATTR_LOOKUP_RE.search(q)
            or _SUBJECT_FIRST_ATTR_LOOKUP_RE.search(q)):
        return "exact_lookup"

    if _RANKING_RE.search(q):
        return "ranking"

    if has_filter:
        return "filter"

    if _AGGREGATE_RE.search(q):
        return "aggregation"

    return "semantic_qa"
