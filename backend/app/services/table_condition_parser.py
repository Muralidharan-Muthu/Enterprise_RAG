"""Natural-language filter clause -> boolean condition AST.

Turns phrases like "invoices above $10,000 in the East region" or
"companies in Chemicals or Banking" into a tree of typed Condition leaves
joined by AND/OR, for table_sql_compiler to compile into parameterized SQL
against table_cell_store.

Scope (documented, not a general NL parser):
- Only left-to-right "OR of ANDs" precedence is supported — no parenthesized
  or arbitrarily nested boolean expressions. Natural-language queries in
  practice don't use parentheses, and this covers every example query this
  engine is built for.
- BETWEEN x AND y is protected from the top-level AND-splitter via a
  placeholder substitution pass, so "between 100 and 200" isn't
  misinterpreted as two separate ANDed conditions.
- Grouping ("by <column>") and ranking ("top N", "largest", ...) clauses are
  detected separately from the condition tree and returned alongside it,
  since they modify how results are aggregated/ordered rather than which
  rows match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Union

# ── AST ──────────────────────────────────────────────────────────────────

@dataclass
class Condition:
    """A single leaf predicate: <column_hint> <op> <value>.

    column_hint is None when no explicit column name could be extracted
    from the query text (e.g. "list all companies in Chemicals" never says
    which column holds "Chemicals") — the SQL compiler resolves these by
    testing which column's actual cell VALUES match, instead of matching
    column NAMES."""
    column_hint: Optional[str]
    op: str          # EQ | NEQ | GT | GTE | LT | LTE | BETWEEN | IN | LIKE
    value: object     # scalar for EQ/GT/../LIKE, (lo, hi) for BETWEEN, list[str] for IN


@dataclass
class BoolNode:
    """AND/OR of children, which may themselves be Condition or BoolNode."""
    op: str  # "AND" | "OR"
    children: list = field(default_factory=list)


ConditionTree = Union[Condition, BoolNode]


@dataclass
class RankingClause:
    column_hint: Optional[str]
    direction: str   # "DESC" | "ASC"
    limit: int


@dataclass
class ParsedFilter:
    tree: Optional[ConditionTree]
    group_by_hint: Optional[str] = None
    ranking: Optional[RankingClause] = None


# ── Leaf condition regexes ──────────────────────────────────────────────

_BETWEEN_RE = re.compile(
    r"\bbetween\s+(.+?)\s+and\s+(.+?)(?=\s+(?:and|or)\b|\s*[\?\.]?$)",
    re.IGNORECASE,
)

# "<column> above/over/greater than/more than/at least $10,000"
_GT_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s+"
    r"(?:is\s+)?(?:above|over|greater\s+than|more\s+than|at\s+least|>=|>)\s*"
    r"\$?([\d,]+\.?\d*)",
    re.IGNORECASE,
)
_GTE_WORDS = {"at least", ">="}
_LT_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s+"
    r"(?:is\s+)?(?:below|under|less\s+than|at\s+most|<=|<)\s*"
    r"\$?([\d,]+\.?\d*)",
    re.IGNORECASE,
)
_LTE_WORDS = {"at most", "<="}

# Category-noun words that typically ARE the column name when they trail an
# "in <value> <word>" phrase — "in the Chemicals SECTOR" names the column via
# this trailing word, not via any text before "in" (which is usually just
# verb filler: "list all companies", "show every employee").
_CATEGORY_NOUNS = r"sector|category|industry|segment|region|department|division|state|country|status|type|class"

# "in <value>[, <value>]*[, or <value>] <category-noun>" — column hint comes
# from the trailing noun (fuzzy-matched against real headers downstream).
_IN_WITH_COLUMN_RE = re.compile(
    rf"\b(?:in|among)\s+(?:the\s+)?(.+?)\s+({_CATEGORY_NOUNS})\b",
    re.IGNORECASE,
)

# "<column> in X, Y, or Z" — an explicit column name precedes "in".
_IN_EXPLICIT_COLUMN_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s+(?:is\s+)?(?:in|among)\s+(?:the\s+)?(.+?)\s*[\?\.]?$",
    re.IGNORECASE,
)

# "in X, Y, or Z" with no column named anywhere — value-only; the SQL
# compiler resolves the column by testing which column's actual cell VALUES
# match, the same strategy the tier-2 Python engine already uses.
_IN_NO_COLUMN_RE = re.compile(
    r"\b(?:in|among)\s+(?:the\s+)?(.+?)\s*[\?\.]?$",
    re.IGNORECASE,
)

# List-verb / filler words that precede the real column/entity name in
# "list all X above/in/..." phrasing — stripped before column resolution so
# they're never mistaken for the column itself.
_FILLER_PREFIX_RE = re.compile(
    r"^(?:list|show|display|enumerate|find|give)\s+(?:me\s+)?(?:all|every|the)?\s*",
    re.IGNORECASE,
)

# Generic collection nouns ("companies", "employees", "invoices", ...) name
# the ENTITY being queried, never the actual column — "list all companies
# in Chemicals" doesn't mean there's a column called "companies". When the
# cleaned hint reduces to just one of these, treat it as no-column-named at
# all (None) so the compiler falls back to resolving by cell VALUE instead
# of futilely fuzzy-matching "companies" against real header names.
_GENERIC_ENTITY_NOUNS = {
    "company", "companies", "employee", "employees", "record", "records",
    "row", "rows", "item", "items", "entry", "entries", "person", "people",
    "user", "users", "customer", "customers", "product", "products",
    "policy", "policies", "invoice", "invoices", "client", "clients",
    "document", "documents", "entity", "entities", "thing", "things",
}


def _strip_filler_prefix(text: str) -> str:
    return _FILLER_PREFIX_RE.sub("", text).strip()


# "<column> containing X" / "<column> with X in the name"
_LIKE_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s+(?:containing|contains|like)\s+['\"]?([^'\"]+?)['\"]?\s*[\?\.]?$",
    re.IGNORECASE,
)

# "<column> is X" / "<column> = X" / "<column> equals X"
_EQ_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s+(?:is|=|equals|equal\s+to)\s+['\"]?([^'\"]+?)['\"]?\s*[\?\.]?$",
    re.IGNORECASE,
)

_GROUP_BY_RE = re.compile(
    r"\b(?:grouped\s+by|group\s+by|by|per)\s+([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s*[\?\.]?$",
    re.IGNORECASE,
)

_RANKING_TOP_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
_RANKING_BOTTOM_RE = re.compile(r"\bbottom\s+(\d+)\b", re.IGNORECASE)
_RANKING_DESC_WORDS = re.compile(r"\b(largest|highest|greatest|most|maximum|top)\b", re.IGNORECASE)
_RANKING_ASC_WORDS = re.compile(r"\b(smallest|lowest|least|minimum|bottom)\b", re.IGNORECASE)
_RANKING_DEFAULT_LIMIT = 10

_BETWEEN_PLACEHOLDER = "\x00BETWEEN{}\x00"


def _parse_number(s: str) -> float:
    return float(s.replace(",", ""))


def _split_in_values(value_text: str) -> list[str]:
    """Split an IN clause's value text on commas and a trailing '/or'."""
    parts = re.split(r"\s*,\s*|\s+or\s+", value_text.strip(), flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _clean_column_hint(raw: str) -> Optional[str]:
    """Strip list-verb filler from a captured column-hint prefix; None if
    nothing meaningful remains, or if what remains is just a generic
    collection noun (signals value-based resolution instead)."""
    cleaned = _strip_filler_prefix(raw).strip()
    if not cleaned:
        return None
    if cleaned.lower() in _GENERIC_ENTITY_NOUNS:
        return None
    return cleaned


def _parse_leaf(segment: str) -> Optional[Condition]:
    segment = segment.strip()
    if not segment:
        return None

    m = _BETWEEN_RE.search(segment)
    if m:
        # Column hint is whatever precedes "between" in this segment.
        prefix = segment[:m.start()].strip()
        col_match = re.search(r"([A-Za-z][A-Za-z0-9 _\-\(\)/%]*)\s*$", prefix)
        column_hint = _clean_column_hint(col_match.group(1)) if col_match else None
        try:
            lo = _parse_number(m.group(1).strip().lstrip("$"))
            hi = _parse_number(m.group(2).strip().lstrip("$"))
        except ValueError:
            return None
        return Condition(column_hint=column_hint, op="BETWEEN", value=(lo, hi))

    m = _GT_RE.search(segment)
    if m:
        op = "GTE" if any(w in segment.lower() for w in _GTE_WORDS) else "GT"
        try:
            return Condition(column_hint=_clean_column_hint(m.group(1)), op=op, value=_parse_number(m.group(2)))
        except ValueError:
            pass

    m = _LT_RE.search(segment)
    if m:
        op = "LTE" if any(w in segment.lower() for w in _LTE_WORDS) else "LT"
        try:
            return Condition(column_hint=_clean_column_hint(m.group(1)), op=op, value=_parse_number(m.group(2)))
        except ValueError:
            pass

    m = _LIKE_RE.search(segment)
    if m:
        return Condition(column_hint=_clean_column_hint(m.group(1)), op="LIKE", value=m.group(2).strip())

    # IN detection — three tiers, most-specific first:
    #  1. Trailing category noun names the column ("in the Chemicals SECTOR").
    #  2. An explicit column name precedes "in" ("REGION in East or West").
    #  3. No column signal at all — value-only, compiler resolves by cell value.
    m = _IN_WITH_COLUMN_RE.search(segment)
    if m:
        values = _split_in_values(m.group(1))
        if values:
            return Condition(column_hint=m.group(2).strip(), op="IN", value=values)

    m = _IN_EXPLICIT_COLUMN_RE.search(segment)
    if m:
        hint = _clean_column_hint(m.group(1))
        if hint is not None:
            values = _split_in_values(m.group(2))
            if values:
                return Condition(column_hint=hint, op="IN", value=values)

    m = _IN_NO_COLUMN_RE.search(segment)
    if m:
        values = _split_in_values(m.group(1))
        if values:
            return Condition(column_hint=None, op="IN", value=values)

    m = _EQ_RE.search(segment)
    if m:
        return Condition(column_hint=_clean_column_hint(m.group(1)), op="EQ", value=m.group(2).strip())

    return None


# An IN clause's own value list can use "or" as a separator ("Chemicals or
# Banking") — that "or" must NOT be mistaken for a top-level boolean
# connector splitting two separate conditions. Protect the whole "in ... or
# ..." span the same way BETWEEN spans are protected, before the top-level
# OR splitter ever runs.
_IN_OR_SPAN_RE = re.compile(
    rf"\b(?:in|among)\s+(?:the\s+)?[A-Za-z0-9][A-Za-z0-9 \-]*?"
    rf"(?:\s*,\s*[A-Za-z0-9][A-Za-z0-9 \-]*?)*"
    rf"\s+or\s+[A-Za-z0-9][A-Za-z0-9 \-]*?"
    rf"(?=\s+(?:{_CATEGORY_NOUNS})\b|\s*[\?\.]?$)",
    re.IGNORECASE,
)
_IN_OR_PLACEHOLDER = "\x00INOR{}\x00"


def _split_top_level(text: str, connector: str) -> list[str]:
    """Split on a boolean connector word, but never inside a protected
    placeholder span (placeholders contain no spaces around 'and'/'or')."""
    pattern = re.compile(rf"\s+{connector}\s+", re.IGNORECASE)
    return [p.strip() for p in pattern.split(text) if p.strip()]


def _protect_spans(text: str, pattern: re.Pattern, placeholder_template: str,
                    protected: dict[str, str]) -> str:
    """Replace every match of `pattern` with a unique placeholder token,
    recording the original text so it can be restored after splitting.
    Shares the `protected` dict across multiple protection passes (BETWEEN,
    IN-or-lists) so restoration is a single combined pass."""
    counter = len(protected)

    def _sub(m: re.Match) -> str:
        nonlocal counter
        key = placeholder_template.format(counter)
        counter += 1
        protected[key] = m.group(0)
        return key

    return pattern.sub(_sub, text)


def _restore_protected(segment: str, protected: dict[str, str]) -> str:
    for key, original in protected.items():
        if key in segment:
            segment = segment.replace(key, original)
    return segment


def parse_condition_tree(filter_text: str) -> Optional[ConditionTree]:
    """Parse a filter clause into an AND/OR tree of Conditions. Returns None
    if no leaf condition could be extracted at all."""
    if not filter_text or not filter_text.strip():
        return None

    placeholders: dict[str, str] = {}
    protected_text = _protect_spans(filter_text, _BETWEEN_RE, _BETWEEN_PLACEHOLDER, placeholders)
    protected_text = _protect_spans(protected_text, _IN_OR_SPAN_RE, _IN_OR_PLACEHOLDER, placeholders)

    or_branches = _split_top_level(protected_text, "or")
    or_nodes: list[ConditionTree] = []

    for branch in or_branches:
        and_segments = _split_top_level(branch, "and")
        and_nodes: list[ConditionTree] = []
        for seg in and_segments:
            restored = _restore_protected(seg, placeholders)
            cond = _parse_leaf(restored)
            if cond is not None:
                and_nodes.append(cond)

        if not and_nodes:
            continue
        if len(and_nodes) == 1:
            or_nodes.append(and_nodes[0])
        else:
            or_nodes.append(BoolNode(op="AND", children=and_nodes))

    if not or_nodes:
        return None
    if len(or_nodes) == 1:
        return or_nodes[0]
    return BoolNode(op="OR", children=or_nodes)


_RANKING_TRIGGER_RE = re.compile(
    r"(?:largest|highest|greatest|most|maximum|top\s*\d*|smallest|lowest|least|minimum|bottom\s*\d*)",
    re.IGNORECASE,
)
# "top 10 companies BY PRICE" — the column being ranked on is named AFTER
# "by", not before it. This must take priority over the entity-word fallback
# below (which would otherwise grab "companies") AND over parse_group_by
# (which would otherwise misread "by price" as a GROUP BY clause instead of
# a sort key) — see parse_filter, which skips group-by detection whenever
# this pattern already claimed the "by ..." clause.
_RANKING_BY_COLUMN_RE = re.compile(
    _RANKING_TRIGGER_RE.pattern + r".*?\bby\s+([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s*[\?\.]?$",
    re.IGNORECASE,
)


def parse_ranking(query: str) -> tuple[Optional[RankingClause], bool]:
    """Returns (ranking_clause, consumed_by_clause). consumed_by_clause is
    True when a "by <column>" suffix was claimed as the ranking sort key —
    the caller must then skip GROUP BY detection for the same query."""
    m_top = _RANKING_TOP_RE.search(query)
    m_bottom = _RANKING_BOTTOM_RE.search(query)

    direction: Optional[str] = None
    limit = _RANKING_DEFAULT_LIMIT

    if m_bottom:
        direction = "ASC"
        limit = int(m_bottom.group(1))
    elif m_top:
        direction = "DESC"
        limit = int(m_top.group(1))
    elif _RANKING_ASC_WORDS.search(query):
        direction = "ASC"
    elif _RANKING_DESC_WORDS.search(query):
        direction = "DESC"

    if direction is None:
        return None, False

    by_match = _RANKING_BY_COLUMN_RE.search(query)
    if by_match:
        column_hint = by_match.group(1).strip()
        return RankingClause(column_hint=column_hint, direction=direction, limit=limit), True

    # No "by <column>" — fall back to the entity word between the ranking
    # trigger and end of string (works for "largest revenue": "revenue" IS
    # the metric column itself, not just an entity noun).
    col_match = re.search(
        _RANKING_TRIGGER_RE.pattern + r"\s+(?:\d+\s+)?(?:the\s+)?([A-Za-z][A-Za-z0-9 _\-\(\)/%]*?)\s*[\?\.]?$",
        query,
        re.IGNORECASE,
    )
    column_hint = col_match.group(1).strip() if col_match else None
    return RankingClause(column_hint=column_hint, direction=direction, limit=limit), False


def parse_group_by(query: str) -> Optional[str]:
    m = _GROUP_BY_RE.search(query.strip())
    if not m:
        return None
    hint = m.group(1).strip()
    # Avoid false positives like ranking's "top 5 X" being caught by a bare "by".
    if not hint or len(hint) < 2:
        return None
    return hint


def parse_filter(query: str, filter_text: Optional[str] = None) -> ParsedFilter:
    """Top-level entry: parse a full query (or an already-extracted filter
    clause) into conditions + optional grouping/ranking."""
    text = filter_text if filter_text is not None else query
    tree = parse_condition_tree(text)
    ranking, consumed_by = parse_ranking(query)
    group_by_hint = None if consumed_by else parse_group_by(query)
    return ParsedFilter(tree=tree, group_by_hint=group_by_hint, ranking=ranking)
