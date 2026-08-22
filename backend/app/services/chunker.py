"""
Enterprise-grade semantic chunker for Multi-Store RAG Chatbot.

Two chunking paths — controlled by ``CHUNK_USE_SEMANTIC`` in config:

Semantic path (default, CHUNK_USE_SEMANTIC=True):
  - Groups blocks into logical units (section boundaries, list coalescing,
    image-block isolation) using the unchanged _build_logical_units() pipeline.
  - Within each logical unit, sentences are embedded with BGE and split at
    cosine-distance breakpoints exceeding the configured percentile (LlamaIndex
    SemanticSplitterNodeParser algorithm).
  - No overlap.  Variable chunk size.
  - Chunks are batch-enriched via Gemma: section_title, keywords, semantic_type.
  - Graceful fallback to heuristics on any Gemma parse/HTTP failure.

Legacy fixed-size path (CHUNK_USE_SEMANTIC=False):
  - Sentence-boundary aware splits at a fixed token target (CHUNK_SIZE_TOKENS).
  - Sentence-level overlap (CHUNK_OVERLAP_TOKENS).
  - Retained verbatim so a single env-var flip reverts the behaviour without
    a code redeploy.

Other improvements preserved on both paths:
  - Section-boundary hard breaks (headers never merged across).
  - Context prefix enrichment (section breadcrumb prepended to each chunk).
  - List block coalescing.
  - image_analysis blocks preserved whole.
  - Rich chunk_metadata for downstream filtering.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.config import settings
from app.models.document import Chunk, LegalClause, ParsedDocument, TextBlock

logger = logging.getLogger(__name__)

# ── Legal clause boundary patterns ───────────────────────────────────────────
CLAUSE_PATTERNS = [
    r"^\d+(\.\d+)*\.?\s+[A-Z]",                                                         # 1. TERMINATION, 1.1 CLAUSE, 1 TERMINATION
    r"^(Article|Section|Clause|Schedule|Annexure|Exhibit|Provision|Term)\s+[\w\d\.]+",  # Article 4, Section 2.1
    r"^[IVXLCDM]+[\.\)]\s+[A-Z]",                                                       # I. TITLE, IV. DISPUTE
    r"^\([a-zA-Z0-9]+\)\s+[A-Z]",                                                       # (a) TERMINATION, (1) NOTICE
    r"^[A-Z0-9\s\-_–—:]{4,80}$",                                                        # ALL-CAPS HEADERS
]
CLAUSE_RE = re.compile("|".join(CLAUSE_PATTERNS), re.MULTILINE)

_LEGAL_KEYWORDS_MAP: dict[str, list[str]] = {
    "termination": ["termination", "terminate", "notice provision", "for cause", "without cause", "exit option", "cancellation"],
    "dispute_resolution": ["dispute resolution", "arbitration", "jurisdiction", "exclusive jurisdiction", "conciliation act", "litigation", "governing law", "choice of law", "court of law", "arbitrator"],
    "liability": ["limitation of liability", "liability", "consequential damages", "indirect damages", "cap on liability", "indemnification", "indemnity", "hold harmless"],
    "confidentiality": ["confidentiality", "confidential information", "non-disclosure", "proprietary information", "trade secret"],
    "warranty": ["warranty", "warranties", "representation and warranty", "merchantability", "as is"],
    "obligation": ["covenant", "obligations", "compliance with laws", "code of conduct", "statutory obligation"],
    "force_majeure": ["force majeure", "act of god", "unforeseen event"],
    "general": ["contractual clause", "legal framework", "agreement", "severability", "entire agreement", "amendment", "waiver", "assignment"],
}


def detect_clause_nature(
    text: str,
    section_title: Optional[str] = None,
    block_types: Optional[list[str]] = None,
) -> tuple[bool, Optional[str], Optional[str], str]:
    """
    Detect if text or a chunk represents a legal clause/contractual term.
    Returns: (is_clause, clause_number, clause_title, clause_type)
    """
    text_clean = text.strip()
    if text_clean.startswith("Context:"):
        parts = text_clean.split("\n\n", 1)
        if len(parts) > 1:
            text_clean = parts[1].strip()

    first_line = text_clean.split("\n")[0].strip()
    low_text = text_clean.lower()
    low_title = (section_title or "").lower()

    # 1. Match numbering or title prefix (e.g. '1. TERMINATION FOR CAUSE (IMMEDIATE ACTION)')
    c_num = None
    c_title = None

    m_num = re.match(r"^(\d+(\.\d+)*\.?|[IVXLCDM]+[\.\)]|\([a-zA-Z0-9]+\))\s+(.*)", first_line)
    if m_num:
        c_num = m_num.group(1).rstrip(".")
        c_title = m_num.group(3).split("\n")[0].strip()
        if len(c_title) > 80:
            c_title = c_title[:80].rsplit(" ", 1)[0]
    elif re.match(r"^(Article|Section|Clause|Schedule|Annexure|Exhibit|Provision)\s+[\w\d\.]+", first_line, re.IGNORECASE):
        c_title = first_line[:80]
        c_num = first_line.split()[1] if len(first_line.split()) > 1 else None

    # 2. Check section title for contractual keywords
    is_legal_section = any(k in low_title for k in [
        "contractual clause", "legal framework", "terms and condition",
        "governing law", "jurisdiction", "termination", "dispute resolution",
        "legal", "contract", "covenant", "warranty", "liability"
    ])

    # 3. Detect clause type from keywords
    matched_type = None
    for ctype, kws in _LEGAL_KEYWORDS_MAP.items():
        if any(kw in low_text or kw in low_title for kw in kws):
            matched_type = ctype
            break

    # Decision criteria:
    # A) Has clause numbering/heading + matched legal/contract keyword
    if (c_num or is_legal_section or CLAUSE_RE.match(first_line)) and matched_type:
        return True, c_num, c_title or section_title, matched_type

    # B) Section title explicitly says 'contractual clause' / 'legal framework'
    if is_legal_section and len(text_clean.split()) >= 10:
        return True, c_num, c_title or section_title, matched_type or "general"

    # C) Strong contract terms (termination rights, exclusive jurisdiction, binding arbitration)
    if any(phrase in low_text for phrase in [
        "unilateral, immediate termination", "termination without cause", "termination for cause",
        "exclusive jurisdiction", "binding dispute resolution", "arbitration and conciliation act",
        "shall indemnify and hold harmless", "limitation of liability", "governed by the laws of"
    ]):
        return True, c_num, c_title or section_title, matched_type or "general"

    return False, None, None, "general"


def convert_chunk_to_legal_clause(chunk: Chunk, clause_index: int) -> LegalClause:
    """Convert a chunk classified as legal_clause into a structured LegalClause object."""
    meta = chunk.chunk_metadata or {}
    core_text = chunk.chunk_text
    if core_text.startswith("Context:"):
        parts = core_text.split("\n\n", 1)
        if len(parts) > 1:
            core_text = parts[1].strip()

    c_num = meta.get("clause_number")
    c_title = meta.get("clause_title") or chunk.section_title
    c_type = meta.get("clause_type") or "general"

    if not c_num or not c_title or c_type == "general":
        _, h_num, h_title, h_type = detect_clause_nature(core_text, chunk.section_title)
        c_num = c_num or h_num
        c_title = c_title or h_title
        if c_type == "general" and h_type != "general":
            c_type = h_type

    return LegalClause(
        clause_index=clause_index,
        clause_text=core_text,
        clause_number=str(c_num) if c_num else f"{clause_index + 1}",
        clause_title=str(c_title) if c_title else f"Clause {clause_index + 1}",
        clause_type=c_type if c_type in (
            "obligation", "prohibition", "right", "definition", "liability",
            "indemnification", "termination", "confidentiality", "dispute_resolution",
            "force_majeure", "warranty", "penalty", "governing_law", "general"
        ) else "general",
        risk_level=meta.get("risk_level"),
        risk_rationale=meta.get("risk_rationale"),
        obligor=meta.get("obligor"),
        obligee=meta.get("obligee"),
        parties_mentioned=meta.get("parties_mentioned") or [],
        key_dates=meta.get("key_dates") or {},
        monetary_values=meta.get("monetary_values") or [],
        page_number=chunk.page_number,
        page_numbers=chunk.page_numbers or ([chunk.page_number] if chunk.page_number else []),
        section_path=meta.get("section_path") or ([chunk.section_title] if chunk.section_title else []),
    )

# ── Sentence splitter ─────────────────────────────────────────────────────────
_SENT_END = re.compile(r'(?<=[.!?])(?:\s+|$)')


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, preserving non-empty results."""
    raw = _SENT_END.split(text)
    return [s.strip() for s in raw if s.strip()]


def _word_count_approx(text: str) -> int:
    return max(1, len(text.split()))


def default_token_counter(text: str) -> int:
    """Real BGE subword token counter, gated by settings.CHUNK_USE_REAL_TOKENIZER.

    Falls back to the legacy whitespace approximation when the flag is False
    so existing behaviour/tests are unaffected unless explicitly opted in.
    Lazily imports embedding_service so the API process never eagerly loads
    the 1.3 GB BGE model just by importing chunker.py.
    """
    if not settings.CHUNK_USE_REAL_TOKENIZER:
        return _word_count_approx(text)
    from app.services import embedding_service
    return max(1, embedding_service.count_tokens(text))


# Module-level indirection used throughout this file. Tests may monkeypatch
# this reference (or pass an injected counter directly to functions that
# accept one) to avoid loading the BGE model.
def _token_count(text: str) -> int:
    return default_token_counter(text)


def _build_context_prefix(section_path: list[str]) -> str:
    """Build retrieval-boosting prefix from section hierarchy."""
    if not section_path:
        return ""
    return "Context: " + " > ".join(section_path) + "\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def chunk_document(parsed_doc: ParsedDocument, document_type: str) -> list[Chunk]:
    if document_type == "legal":
        return _chunk_by_clauses(parsed_doc)
    elif document_type == "financial":
        return _chunk_financial(parsed_doc)
    else:
        return _chunk_semantic(parsed_doc)


def extract_legal_clauses(parsed_doc: ParsedDocument) -> list[LegalClause]:
    clauses: list[LegalClause] = []
    blocks = parsed_doc.text_blocks
    if not blocks:
        return clauses

    current_texts: list[str] = []
    current_num: Optional[str] = None
    current_title: Optional[str] = None
    current_page = blocks[0].page_number
    current_pages: list[int] = []
    section_path: list[str] = []
    clause_index = 0

    def flush() -> None:
        nonlocal clause_index
        if current_texts:
            clauses.append(LegalClause(
                clause_index=clause_index,
                clause_text=" ".join(current_texts).strip(),
                clause_number=current_num,
                clause_title=current_title,
                page_number=current_page,
                page_numbers=sorted(set(current_pages)),
                section_path=list(section_path),
            ))
            clause_index += 1

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        is_boundary = bool(CLAUSE_RE.match(text)) or block.block_type == "header"
        if is_boundary and current_texts:
            flush()
            current_texts = []
            current_pages = []
            m = re.match(r"^(\d+(\.\d+)*|[IVXLC]+\.?)\s+(.*)", text)
            if m:
                current_num = m.group(1)
                current_title = m.group(3)[:100]
            else:
                current_num = None
                current_title = text[:100]
            if block.section_title and block.section_title not in section_path:
                section_path.append(block.section_title)
        current_texts.append(text)
        current_pages.append(block.page_number)
        current_page = block.page_number

    flush()
    return clauses


# ─────────────────────────────────────────────────────────────────────────────
# Chunking strategies
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_semantic(parsed_doc: ParsedDocument) -> list[Chunk]:
    """
    Dispatcher for the two chunking paths.

    When ``settings.CHUNK_USE_SEMANTIC`` is True (default), uses the
    embedding-breakpoint algorithm implemented in ``semantic_chunker.py``
    (LlamaIndex SemanticSplitterNodeParser).  Chunk boundaries are placed where
    consecutive sentence embeddings are most dissimilar; Gemma enriches every
    chunk with section_title, keywords, and semantic_type.

    When False, falls back to the original fixed-size sentence-boundary splitter
    with sentence-level overlap — identical to the pre-semantic-chunking code.
    """
    logical_units = _build_logical_units(parsed_doc.text_blocks)
    if not logical_units:
        return []

    if settings.CHUNK_USE_SEMANTIC:
        return _chunk_semantic_breakpoint(logical_units)
    else:
        return _chunk_semantic_legacy(logical_units)


# ── New semantic-breakpoint path ──────────────────────────────────────────────

def _chunk_semantic_breakpoint(logical_units: list[_LogicalUnit]) -> list[Chunk]:
    """Embedding-breakpoint chunker (semantic path).

    Steps:
    1. For each logical unit, split its sentences using cosine-distance
       breakpoints from BGE embeddings (via semantic_chunker).
    2. Image-analysis units are kept whole (never split).
    3. Build _RawChunk objects without overlap.
    4. Batch-enrich all chunks via Gemma; fall back to heuristics on failure.
    5. Return final Chunk dataclass list.
    """
    from app.services import semantic_chunker as _sc
    from app.services import groq_client

    # Lazy import of BGE embed function — already warm in the worker; avoids
    # loading the 1.3 GB model in API processes that import chunker.
    from app.services.embedding_service import embed_passages as _embed_passages

    percentile = settings.CHUNK_SEMANTIC_BREAKPOINT_PERCENTILE
    max_tokens = settings.CHUNK_MAX_TOKENS
    min_tokens = settings.MIN_CHUNK_SIZE_TOKENS
    batch_size = settings.CHUNK_ENRICH_BATCH_SIZE

    raw_chunks: list[_RawChunk] = []

    for unit in logical_units:
        if unit.is_image_analysis:
            # Image analysis blocks are kept intact; never split or merged.
            rc = _RawChunk(unit)
            rc.sentences = [unit.text]
            raw_chunks.append(rc)
            continue

        sentences = _split_sentences(unit.text)
        if not sentences:
            continue

        # Delegate to semantic_chunker — returns list[list[str]] (sentence groups)
        groups = _sc.split_sentences_semantically(
            sentences=sentences,
            embed_fn=_embed_passages,
            percentile=percentile,
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            token_counter=_token_count,
        )

        for group in groups:
            rc = _RawChunk(unit)
            rc.sentences = group
            # Page tracking: propagate the unit's page numbers to each chunk;
            # per-sentence page attribution is not available at this layer.
            raw_chunks.append(rc)

    if not raw_chunks:
        return []

    # ── Groq enrichment ───────────────────────────────────────────────────────
    # Collect plain texts (no prefix) for the enrichment prompts.
    core_texts = [rc.text for rc in raw_chunks]
    section_paths = [rc.section_path for rc in raw_chunks]
    block_types_list = [rc.block_types for rc in raw_chunks]

    enrichments = _sc.enrich_chunks_with_groq(
        chunk_texts=core_texts,
        section_paths=section_paths,
        block_types_list=block_types_list,
        groq_chat_fn=groq_client.chat,
        batch_size=batch_size,
    )

    # ── Assemble final Chunk objects ──────────────────────────────────────────
    chunks: list[Chunk] = []
    for idx, (rc, enrichment) in enumerate(zip(raw_chunks, enrichments)):
        core_text = rc.text
        prefix = _build_context_prefix(rc.section_path)
        enriched_text = prefix + core_text

        # Prefer Gemma's section_title; fall back to the breadcrumb tail.
        section_title = (
            enrichment.get("section_title")
            or (rc.section_path[-1] if rc.section_path else None)
        )
        keywords = enrichment.get("keywords") or []
        semantic_type = enrichment.get("semantic_type") or (
            "image_analysis" if rc.is_image_analysis else
            ("list" if "list" in rc.block_types else "paragraph")
        )

        # Detect if this chunk is a legal clause even if LLM returned generic paragraph
        is_clause, h_cnum, h_ctitle, h_ctype = detect_clause_nature(
            core_text, section_title, rc.block_types
        )
        if is_clause and not rc.is_image_analysis:
            semantic_type = "legal_clause"

        # Override semantic_type for image_analysis blocks regardless of Gemma
        if rc.is_image_analysis:
            semantic_type = "image_analysis"

        clause_number = enrichment.get("clause_number") or h_cnum
        clause_title = enrichment.get("clause_title") or h_ctitle or section_title
        clause_type = enrichment.get("clause_type") or h_ctype or "general"
        risk_level = enrichment.get("risk_level")
        risk_rationale = enrichment.get("risk_rationale")
        parties_mentioned = enrichment.get("parties_mentioned") or []
        obligor = enrichment.get("obligor")
        obligee = enrichment.get("obligee")

        chunks.append(Chunk(
            chunk_index=idx,
            chunk_text=enriched_text,
            page_number=rc.page_number,
            page_numbers=rc.page_numbers,
            section_title=section_title,
            section_level=len(rc.section_path) if rc.section_path else None,
            semantic_type=semantic_type,
            keywords=keywords if keywords else _extract_keywords(core_text),
            token_count=_token_count(enriched_text),
            chunk_metadata={
                "section_path": rc.section_path,
                "block_types": list(set(rc.block_types)),
                "has_image_content": rc.is_image_analysis,
                "overlap_applied": False,   # semantic path has no overlap
                "enriched_by_groq": bool(enrichment.get("section_title")),
                "clause_number": clause_number,
                "clause_title": clause_title,
                "clause_type": clause_type,
                "risk_level": risk_level,
                "risk_rationale": risk_rationale,
                "parties_mentioned": parties_mentioned,
                "obligor": obligor,
                "obligee": obligee,
            },
        ))

    return chunks


# ── Legacy fixed-size path (unchanged logic, gated behind CHUNK_USE_SEMANTIC=False) ──

def _chunk_semantic_legacy(logical_units: list[_LogicalUnit]) -> list[Chunk]:
    """
    Original fixed-size sentence-boundary chunker with sentence-level overlap.
    Activated when CHUNK_USE_SEMANTIC=False.  Code is unchanged from the
    pre-semantic implementation — only refactored into its own function.
    """
    target = settings.CHUNK_SIZE_TOKENS
    overlap_tokens = settings.CHUNK_OVERLAP_TOKENS
    min_tokens = settings.MIN_CHUNK_SIZE_TOKENS

    raw_chunks = _units_to_raw_chunks(logical_units, target, min_tokens)
    return _apply_overlap_and_enrich(raw_chunks, overlap_tokens, min_tokens)


def _chunk_financial(parsed_doc: ParsedDocument) -> list[Chunk]:
    """Financial: same as semantic but exclude captions (tables stored separately)."""
    filtered_blocks = [b for b in parsed_doc.text_blocks if b.block_type != "caption"]
    filtered_doc = ParsedDocument(
        doc_id=parsed_doc.doc_id,
        filename=parsed_doc.filename,
        raw_text=parsed_doc.raw_text,
        text_blocks=filtered_blocks,
        tables=[],
        page_count=parsed_doc.page_count,
        word_count=parsed_doc.word_count,
        has_tables=False,
        has_images=parsed_doc.has_images,
        image_page_numbers=parsed_doc.image_page_numbers,
        metadata=parsed_doc.metadata,
    )
    return _chunk_semantic(filtered_doc)


def _chunk_by_clauses(parsed_doc: ParsedDocument) -> list[Chunk]:
    clauses = extract_legal_clauses(parsed_doc)
    chunks: list[Chunk] = []
    for c in clauses:
        words = c.clause_text.split()
        if len(words) < 5:
            continue
        prefix = _build_context_prefix(c.section_path) if c.section_path else ""
        enriched = prefix + c.clause_text
        chunks.append(Chunk(
            chunk_index=c.clause_index,
            chunk_text=enriched,
            page_number=c.page_number,
            page_numbers=c.page_numbers,
            section_title=c.clause_title or c.clause_number,
            section_level=None,
            semantic_type="clause",
            keywords=_extract_keywords(c.clause_text),
            token_count=_token_count(enriched),
            chunk_metadata={
                "section_path": c.section_path,
                "clause_number": c.clause_number,
                "block_types": ["clause"],
                "has_image_content": False,
            },
        ))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Logical unit builder
# ─────────────────────────────────────────────────────────────────────────────

class _LogicalUnit:
    """A semantically coherent group of blocks within one section."""
    __slots__ = ("texts", "pages", "section_path", "block_types", "is_image_analysis")

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.pages: list[int] = []
        self.section_path: list[str] = []
        self.block_types: list[str] = []
        self.is_image_analysis: bool = False

    def add(self, block: TextBlock) -> None:
        self.texts.append(block.text.strip())
        self.pages.append(block.page_number)
        self.block_types.append(block.block_type)
        if block.block_type == "image_analysis":
            self.is_image_analysis = True

    @property
    def text(self) -> str:
        return " ".join(self.texts)

    @property
    def token_count(self) -> int:
        return _token_count(self.text)

    @property
    def page_number(self) -> int:
        return self.pages[0] if self.pages else 1

    @property
    def page_numbers(self) -> list[int]:
        return sorted(set(self.pages))

    @property
    def section_title(self) -> Optional[str]:
        return self.section_path[-1] if self.section_path else None


def _build_logical_units(blocks: list[TextBlock]) -> list[_LogicalUnit]:
    """
    Coalesce blocks into logical units:
    - Headers create section boundaries and start a new unit
    - Consecutive list items are merged into one unit
    - image_analysis blocks become their own unit (never split)
    - Paragraphs accumulate within the current section
    """
    units: list[_LogicalUnit] = []
    current: Optional[_LogicalUnit] = None
    section_path: list[str] = []
    in_list = False

    def new_unit() -> _LogicalUnit:
        u = _LogicalUnit()
        u.section_path = list(section_path)
        return u

    def push(u: _LogicalUnit) -> None:
        if u.texts:
            units.append(u)

    for block in blocks:
        text = block.text.strip()
        if len(text.split()) < 2:
            continue

        if block.block_type == "header":
            # Flush current, update section path, start fresh
            if current:
                push(current)
            # Maintain section hierarchy by level
            level = block.section_level or 1
            # Trim path to current level
            section_path = section_path[: level - 1]
            section_path.append(text)
            in_list = False
            current = new_unit()
            # Header text itself included as context hint — don't add to unit text
            # (it's captured in section_path for prefix generation)
            continue

        if block.block_type == "image_analysis":
            # Flush current, store image block alone
            if current:
                push(current)
            img_unit = new_unit()
            img_unit.add(block)
            push(img_unit)
            current = new_unit()
            in_list = False
            continue

        if block.block_type == "list":
            if not in_list:
                # Start a new list unit
                if current and current.texts and not all(t == "list" for t in current.block_types):
                    push(current)
                    current = new_unit()
                in_list = True
            if current is None:
                current = new_unit()
            current.add(block)
            continue

        # Regular paragraph
        in_list = False
        if current is None:
            current = new_unit()
        current.add(block)

    if current:
        push(current)

    return units


# ─────────────────────────────────────────────────────────────────────────────
# Units → raw chunks (sentence-boundary aware)
# ─────────────────────────────────────────────────────────────────────────────

class _RawChunk:
    __slots__ = ("sentences", "pages", "section_path", "block_types", "is_image_analysis")

    def __init__(self, unit: _LogicalUnit) -> None:
        self.sentences: list[str] = []
        self.pages: list[int] = list(unit.pages)
        self.section_path: list[str] = list(unit.section_path)
        self.block_types: list[str] = list(unit.block_types)
        self.is_image_analysis: bool = unit.is_image_analysis

    @property
    def text(self) -> str:
        return " ".join(self.sentences)

    @property
    def token_count(self) -> int:
        return _token_count(self.text)

    @property
    def page_number(self) -> int:
        return self.pages[0] if self.pages else 1

    @property
    def page_numbers(self) -> list[int]:
        return sorted(set(self.pages))


def _units_to_raw_chunks(
    units: list[_LogicalUnit],
    target: int,
    min_tokens: int,
) -> list[_RawChunk]:
    """
    Convert logical units to raw chunks. Units smaller than target are merged
    across sections only when they are adjacent and both tiny. Oversized units
    are split at sentence boundaries.
    """
    raw: list[_RawChunk] = []

    for unit in units:
        if unit.is_image_analysis:
            rc = _RawChunk(unit)
            rc.sentences = [unit.text]
            raw.append(rc)
            continue

        sentences = _split_sentences(unit.text)
        if not sentences:
            continue

        if unit.token_count <= target:
            # Unit fits — but try to merge with previous tiny chunk if same section
            if (raw and
                    raw[-1].token_count + unit.token_count <= target and
                    raw[-1].section_path == unit.section_path and
                    not raw[-1].is_image_analysis):
                raw[-1].sentences.extend(sentences)
                raw[-1].pages.extend(unit.pages)
                raw[-1].block_types.extend(unit.block_types)
            else:
                rc = _RawChunk(unit)
                rc.sentences = sentences
                raw.append(rc)
        else:
            # Split oversized unit at sentence boundaries
            current_rc = _RawChunk(unit)
            current_rc.pages = []
            token_acc = 0
            for sent in sentences:
                tc = _token_count(sent)
                if token_acc + tc > target and current_rc.sentences:
                    raw.append(current_rc)
                    current_rc = _RawChunk(unit)
                    current_rc.pages = []
                    token_acc = 0
                current_rc.sentences.append(sent)
                current_rc.pages.extend(unit.pages)
                token_acc += tc
            if current_rc.sentences:
                raw.append(current_rc)

    # Drop degenerate chunks
    raw = [rc for rc in raw if rc.token_count >= min_tokens]
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Overlap + context enrichment → final Chunk objects
# ─────────────────────────────────────────────────────────────────────────────

def _apply_overlap_and_enrich(
    raw: list[_RawChunk],
    overlap_tokens: int,
    min_tokens: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []

    for idx, rc in enumerate(raw):
        sentences = list(rc.sentences)

        # Prepend tail sentences from previous chunk as overlap
        if idx > 0 and not rc.is_image_analysis and not raw[idx - 1].is_image_analysis:
            prev_sents = raw[idx - 1].sentences
            overlap_sents: list[str] = []
            acc = 0
            for s in reversed(prev_sents):
                tc = _token_count(s)
                if acc + tc > overlap_tokens:
                    break
                overlap_sents.insert(0, s)
                acc += tc
            sentences = overlap_sents + sentences

        core_text = " ".join(sentences)
        prefix = _build_context_prefix(rc.section_path)
        enriched_text = prefix + core_text

        section_title = rc.section_path[-1] if rc.section_path else None
        section_level = len(rc.section_path) if rc.section_path else None
        semantic_type = "image_analysis" if rc.is_image_analysis else (
            "list" if "list" in rc.block_types else "paragraph"
        )

        chunks.append(Chunk(
            chunk_index=idx,
            chunk_text=enriched_text,
            page_number=rc.page_number,
            page_numbers=rc.page_numbers,
            section_title=section_title,
            section_level=section_level,
            semantic_type=semantic_type,
            keywords=_extract_keywords(core_text),
            token_count=_token_count(enriched_text),
            chunk_metadata={
                "section_path": rc.section_path,
                "block_types": list(set(rc.block_types)),
                "has_image_content": rc.is_image_analysis,
                "overlap_applied": idx > 0 and not rc.is_image_analysis,
            },
        ))

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Keyword extraction
# ─────────────────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "that", "this", "it", "its", "as", "not", "no", "shall", "will", "may",
    "which", "who", "any", "all", "each", "such", "their", "has", "have",
    "had", "do", "does", "did", "would", "could", "should", "also", "than",
    "then", "when", "where", "into", "upon", "under", "over", "about",
}


def _extract_keywords(text: str, max_kw: int = 10) -> list[str]:
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w not in _STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    # Also score bigrams
    tokens = [w for w in words if w not in _STOPWORDS]
    for i in range(len(tokens) - 1):
        bg = tokens[i] + "_" + tokens[i + 1]
        freq[bg] = freq.get(bg, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:max_kw]]
