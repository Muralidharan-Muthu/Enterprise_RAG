from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class ExtractedImage:
    image_index: int
    page_number: int
    bbox: Optional[BoundingBox]
    png_bytes: bytes
    width: int = 0
    height: int = 0


@dataclass
class TextBlock:
    text: str
    page_number: int
    block_type: str      # 'paragraph' | 'list' | 'header' | 'caption' | 'footnote'
    section_title: Optional[str] = None
    section_level: Optional[int] = None
    bbox: Optional[BoundingBox] = None
    token_count: int = 0


@dataclass
class ExtractedTable:
    table_index: int
    page_number: int
    headers: list[str]
    rows: list[list[str]]
    caption: Optional[str] = None
    bbox: Optional[BoundingBox] = None
    raw_text: str = ""
    markdown_text: str = ""
    image_png_bytes: Optional[bytes] = None
    # Populated by _parse_non_pdf for oversized sheets (feature 1.2).
    # e.g. {"oversized": True, "row_count": 12000}
    table_metadata: dict = field(default_factory=dict)
    # Multi-page continuation merging (document_parser._merge_continued_tables):
    # parallel array to `rows` giving the originating page number for each row.
    # None for the common single-page case (full backward compatibility — every
    # existing reader of `page_number` for the whole table is unaffected). Only
    # populated when this ExtractedTable is the result of merging 2+ consecutive
    # per-page table fragments that Docling split a single logical table into.
    row_page_numbers: Optional[list[int]] = None


@dataclass
class ParsedDocument:
    doc_id: str
    filename: str
    raw_text: str
    text_blocks: list[TextBlock]
    tables: list[ExtractedTable]
    page_count: int
    word_count: int
    has_tables: bool
    has_images: bool
    language_detected: str = "en"
    metadata: dict = field(default_factory=dict)
    image_page_numbers: list[int] = field(default_factory=list)  # pages with embedded images
    images: list["ExtractedImage"] = field(default_factory=list)
    # metadata may contain: title, author, creation_date, subject


@dataclass
class Chunk:
    chunk_index: int
    chunk_text: str
    page_number: int
    page_numbers: list[int]
    section_title: Optional[str]
    section_level: Optional[int]
    semantic_type: str   # 'paragraph' | 'list' | 'header' | 'caption'
    keywords: list[str]
    token_count: int
    bbox: Optional[BoundingBox] = None
    chunk_metadata: dict = field(default_factory=dict)


@dataclass
class TableChunk:
    """A table extracted from a financial document — stored as-is in table_store."""
    table_index: int
    table_title: Optional[str]
    page_number: int
    raw_text: str
    markdown_text: str
    json_data: dict          # {"headers": [...], "rows": [[...]]}
    csv_data: str
    row_count: int
    col_count: int
    context_before: str
    context_after: str
    bbox: Optional[BoundingBox] = None
    table_metadata: dict = field(default_factory=dict)


@dataclass
class TableRowChunk:
    """A token-bounded row-window of a parent table — child of table_store.

    Produced by table_chunker.build_row_windows() and stored in table_chunk_store.
    The parent table_store row holds the summary embedding; each child holds a
    per-window embedding for fine-grained semantic retrieval.
    """
    table_index: int          # matches ExtractedTable.table_index (and table_store.table_index)
    chunk_index: int          # 0-based window index within this table
    row_start: int            # first data row index (0-based) included in this window
    row_end: int              # last data row index (inclusive, 0-based)
    serialized_text: str      # "Col1: val1; Col2: val2\n..." with header repeated
    page_number: int          # page of the parent table
    chunk_metadata: dict = field(default_factory=dict)


@dataclass
class LegalClause:
    """A single clause extracted from a legal document — stored in clause_store."""
    clause_index: int
    clause_text: str
    clause_number: Optional[str]
    clause_title: Optional[str]
    page_number: int
    page_numbers: list[int]
    section_path: list[str]
    clause_metadata: dict = field(default_factory=dict)
    # Phase 2 enrichment — populated by clause_enrichment_service after extraction
    clause_type: str = "general"
    risk_level: Optional[str] = None
    risk_rationale: Optional[str] = None
    obligor: Optional[str] = None
    obligee: Optional[str] = None
    parties_mentioned: list = field(default_factory=list)
    key_dates: dict = field(default_factory=dict)
    monetary_values: list = field(default_factory=list)
