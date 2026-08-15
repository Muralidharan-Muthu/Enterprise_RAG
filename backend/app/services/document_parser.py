"""
Docling-based document parser.
Converts a document file into a ParsedDocument containing text blocks and tables.

PDF path (default):
  parse_document → parse_document_chunked (bounded memory, live progress)
                 → _parse_with_docling  (whole-doc fallback)
                 → _parse_fallback      (PyMuPDF text dump, last resort)

Non-PDF path (DOCX / PPTX / XLSX / HTML / MD):
  parse_document → _parse_non_pdf       (single Docling pass, no fitz/PyMuPDF)

Non-PDF failures propagate cleanly — never call fitz on non-PDF content.
"""
import io
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

from app.core.exceptions import ParsingError
from app.models.document import (
    BoundingBox,
    ExtractedImage,
    ExtractedTable,
    ParsedDocument,
    TextBlock,
)

logger = logging.getLogger(__name__)

# Extensions handled natively by this parser.
SUPPORTED_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md"}


def _simple_token_count(text: str) -> int:
    return max(1, len(text.split()))


def _pil_to_png_bytes(pil_image) -> tuple[bytes, int, int]:
    """Encode a PIL image to PNG bytes; return (bytes, width, height)."""
    buf = io.BytesIO()
    rgb = pil_image.convert("RGB") if pil_image.mode not in ("RGB", "RGBA") else pil_image
    rgb.save(buf, format="PNG")
    return buf.getvalue(), pil_image.width, pil_image.height


def parse_document(
    file_path: str,
    doc_id: str,
    on_progress: Optional[Callable[[int, int, list], None]] = None,
    prescan: Optional[list] = None,
) -> ParsedDocument:
    """
    Parse a document.  Routing is by file extension:

    .pdf → page-chunked Docling path (bounded memory + live progress):
      1. parse_document_chunked — page-chunked Docling.
      2. _parse_with_docling   — whole-document Docling (one pass).
      3. _parse_fallback       — PyMuPDF raw-text dump (last resort).

    .docx / .pptx / .xlsx / .html / .htm / .md → _parse_non_pdf:
      Single Docling pass; reuses _extract_items / _extract_tables unchanged.
      Failures propagate cleanly — never falls back to fitz for non-PDF.
    """
    path = Path(file_path)
    if not path.exists():
        raise ParsingError(path.name, "File not found")

    ext = path.suffix.lower()

    if ext != ".pdf":
        # Non-PDF: single Docling pass via the multi-format converter.
        # Let any exception bubble up — the orchestrator marks the doc failed.
        return _parse_non_pdf(path, doc_id)

    # PDF: chunked path with fallbacks.
    try:
        return parse_document_chunked(path, doc_id, prescan=prescan, on_progress=on_progress)
    except Exception as exc:
        logger.warning("Chunked parse failed for %s: %s — trying whole-doc Docling", path.name, exc)
    try:
        return _parse_with_docling(path, doc_id)
    except Exception as exc:
        logger.warning("Docling parse failed for %s: %s — using PyMuPDF fallback", path.name, exc)
        return _parse_fallback(path, doc_id)


def _resolve_do_ocr(path: Optional[Path]) -> bool:
    """OCR decision. settings.DOCLING_DO_OCR is the master switch.

    OCR is honoured whenever enabled — we do NOT skip it based on a text-layer
    probe. A doc-average "has text layer" heuristic silently dropped real tables:
    a page whose tables are baked into a rendered graphic (e.g. a designed
    newsletter) carries enough title/header text to look text-based, yet its
    table CELLS need OCR. Without it Docling returns 0×0 grids and the tables
    vanish. Correctness (every table extracted) beats the parse-time saving."""
    from app.config import settings

    return bool(settings.DOCLING_DO_OCR)


def _make_converter(do_ocr: Optional[bool] = None):
    """Build a Docling converter from settings. Models load once and the
    converter is reused across page chunks (no per-chunk 1.3 GB reload).

    do_ocr overrides the OCR setting per document (see _resolve_do_ocr)."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    # Store models locally — bypasses HF hub's symlink-based cache which
    # requires SeCreateSymbolicLinkPrivilege (WinError 1314 on Windows
    # without Developer Mode). Local path uses plain file copies instead.
    artifacts_dir = Path(__file__).resolve().parent.parent.parent / "docling_models"
    artifacts_dir.mkdir(exist_ok=True)

    from app.config import settings
    pipeline_options = PdfPipelineOptions(
        do_ocr=settings.DOCLING_DO_OCR if do_ocr is None else do_ocr,
        do_table_structure=settings.DOCLING_DO_TABLE_STRUCTURE,
        artifacts_path=artifacts_dir,
        generate_picture_images=True,
        generate_table_images=True,
        images_scale=settings.DOCLING_IMAGES_SCALE,
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def _make_converter_multi():
    """Build a Docling DocumentConverter that handles PDF (with full pipeline
    options) plus DOCX / PPTX / XLSX / HTML / MD using Docling defaults.

    PDF keeps all existing options (artifacts dir, OCR, table-structure,
    image generation, scale) exactly as in _make_converter().  The other
    formats are registered with Docling's default pipeline so no extra
    configuration is needed — passing only the PDF format_options is
    sufficient; Docling accepts the remaining InputFormats without explicit
    options when they are listed in allowed_formats."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    artifacts_dir = Path(__file__).resolve().parent.parent.parent / "docling_models"
    artifacts_dir.mkdir(exist_ok=True)

    from app.config import settings
    pipeline_options = PdfPipelineOptions(
        do_ocr=settings.DOCLING_DO_OCR,
        do_table_structure=settings.DOCLING_DO_TABLE_STRUCTURE,
        artifacts_path=artifacts_dir,
        generate_picture_images=True,
        generate_table_images=True,
        images_scale=settings.DOCLING_IMAGES_SCALE,
    )
    return DocumentConverter(
        allowed_formats=[
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.PPTX,
            InputFormat.XLSX,
            InputFormat.HTML,
            InputFormat.MD,
        ],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )


def _ensure_html_body(path: Path) -> Path:
    """Return a path to an HTML file guaranteed to have a <body> element.

    Docling's HTML backend calls ``soup.body.find_all(...)`` unconditionally.
    If the file has no <body> tag (fragment, XHTML, partial page) soup.body
    is None and parsing crashes with AttributeError.  We parse with
    BeautifulSoup, wrap the content if necessary, and write a fixed temp file.
    The caller is responsible for deleting the returned path when it differs
    from the original.
    """
    from bs4 import BeautifulSoup

    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    if soup.body is not None:
        # Already has a body — return original path, no temp needed.
        return path

    logger.debug("HTML file %s has no <body> — wrapping content before Docling parse", path.name)
    # Grab whatever is at the top level; preserve existing <html> wrapper if any.
    inner = str(soup) if soup.html is None else str(soup.html)
    wrapped = f"<html><body>{inner}</body></html>"
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
    tmp.write(wrapped)
    tmp.close()
    return Path(tmp.name)


def _docx_paragraph_pages(path: Path) -> list[tuple[str, int]]:
    """Return [(paragraph_text, page_number), ...] in document order for a DOCX.

    Word documents are reflowable and carry no fixed page structure, so Docling
    reports a single page. Word DOES cache its last on-screen pagination as
    ``w:lastRenderedPageBreak`` elements (written at save time) plus any explicit
    ``w:br w:type="page"`` breaks. Counting these in document order reconstructs
    the page each paragraph belongs to — matching what the user sees in Word.

    A .docx is a zip; word/document.xml is plain XML, so this needs no extra
    dependency. Returns [] on any error (fail-open → caller keeps page_count=1).
    """
    import zipfile
    from xml.etree import ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: list[tuple[str, int]] = []
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        body = root.find(f"{W}body")
        if body is None:
            return []
        page = 1
        for p in body.iter(f"{W}p"):
            para_page: Optional[int] = None
            parts: list[str] = []
            # Walk the paragraph's descendants in document order so a break that
            # precedes the text bumps the page before the text is tagged, while a
            # break after the text only affects following paragraphs.
            for node in p.iter():
                tag = node.tag
                if tag == f"{W}t" and node.text:
                    if para_page is None:
                        para_page = page
                    parts.append(node.text)
                elif tag == f"{W}lastRenderedPageBreak":
                    page += 1
                elif tag == f"{W}br" and node.get(f"{W}type") == "page":
                    page += 1
            text = "".join(parts).strip()
            if text:
                out.append((text, para_page if para_page is not None else page))
        return out
    except Exception:
        return []


def _assign_docx_pages(text_blocks: list, para_pages: list[tuple[str, int]]) -> None:
    """Reassign TextBlock.page_number in place from the DOCX paragraph→page map.

    Docling text blocks and Word paragraphs are ~1:1 in document order. We walk
    both in order with a single forward pointer and match by normalised text
    prefix; unmatched blocks inherit the last known page (monotonic, no regress).
    Best-effort: any block that cannot be matched keeps the running page.
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s or "").strip().lower()

    n = len(para_pages)
    j = 0
    last_page = 1
    for blk in text_blocks:
        bt = _norm(blk.text)
        if not bt:
            blk.page_number = last_page
            continue
        k = j
        while k < n:
            pt = _norm(para_pages[k][0])
            if pt and (pt[:30] == bt[:30] or pt.startswith(bt[:20]) or bt.startswith(pt[:20])):
                last_page = para_pages[k][1]
                j = k + 1
                break
            k += 1
        blk.page_number = last_page


def _parse_html_fallback(path: Path, doc_id: str) -> ParsedDocument:
    """BeautifulSoup plain-text fallback when Docling fails on HTML."""
    from bs4 import BeautifulSoup

    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "html.parser")
    # Remove script/style noise.
    for tag in soup(["script", "style"]):
        tag.decompose()
    raw_text = soup.get_text(separator="\n", strip=True)
    blocks = [
        TextBlock(
            text=para,
            page_number=1,
            block_type="paragraph",
            token_count=_simple_token_count(para),
        )
        for para in raw_text.split("\n")
        if para.strip()
    ]
    return ParsedDocument(
        doc_id=doc_id,
        filename=path.name,
        raw_text=raw_text,
        text_blocks=blocks,
        tables=[],
        page_count=1,
        word_count=len(raw_text.split()),
        has_tables=False,
        has_images=False,
        image_page_numbers=[],
        images=[],
        metadata={},
    )


def _parse_non_pdf(path: Path, doc_id: str) -> ParsedDocument:
    """Single Docling pass for DOCX / PPTX / XLSX / HTML / MD.

    Reuses _extract_items() and _extract_tables() unchanged — they are
    format-agnostic helpers that operate on any Docling document object.
    Builds a ParsedDocument with the same fields as _parse_with_docling().

    Never calls fitz / PyMuPDF.  Any exception propagates to the caller
    (ingestion_orchestrator marks the job as failed at the parsing stage).

    Excel oversized-table guard: when a table has more rows than
    XLSX_MAX_ROWS_PER_SHEET, its table_metadata is annotated with
    {"oversized": True, "row_count": N} so downstream observability /
    feature 1.5 can act on it.  Data is NOT truncated.

    HTML resilience: Docling's HTML backend requires a <body> element.
    Files that lack one are preprocessed via _ensure_html_body() before
    the converter sees them.  If Docling still fails, _parse_html_fallback()
    extracts plain text via BeautifulSoup (no tables/images, but never 500s).
    """
    from app.config import settings as _settings

    ext = path.suffix.lower()
    fixed_path: Path | None = None  # temp file to clean up (HTML only)

    if ext in (".html", ".htm"):
        fixed_path = _ensure_html_body(path)
        parse_path = fixed_path
    else:
        parse_path = path

    try:
        converter = _make_converter_multi()
        doc = converter.convert(str(parse_path)).document
    except Exception as _docling_exc:
        if ext in (".html", ".htm"):
            logger.warning(
                "[%s] Docling HTML parse failed (%s) — using BeautifulSoup fallback",
                doc_id, _docling_exc,
            )
            return _parse_html_fallback(path, doc_id)
        raise
    finally:
        # Single cleanup point — covers success, HTML fallback return, and re-raise.
        if fixed_path and fixed_path != path:
            try:
                os.unlink(fixed_path)
            except OSError:
                pass

    text_blocks, raw_parts, images, image_pages = _extract_items(doc)
    tables = _extract_tables(doc)

    # Oversized-table guard: XLSX-only, non-destructive metadata flag.
    # Only meaningful for spreadsheets; DOCX/PPTX/HTML tables have no row limit.
    if ext in (".xlsx",):
        xlsx_max = _settings.XLSX_MAX_ROWS_PER_SHEET
        for tbl in tables:
            row_count = len(tbl.rows) if tbl.rows else 0
            if row_count > xlsx_max:
                if tbl.table_metadata is None:
                    tbl.table_metadata = {}
                tbl.table_metadata["oversized"] = True
                tbl.table_metadata["row_count"] = row_count
                logger.warning(
                    "[%s] Table %d has %d rows (> XLSX_MAX_ROWS_PER_SHEET=%d); flagged as oversized",
                    doc_id, tbl.table_index, row_count, xlsx_max,
                )

    raw_text = "\n\n".join(raw_parts)
    page_count = len(doc.pages) if hasattr(doc, "pages") and doc.pages else 1

    # DOCX pagination: Docling reports 1 page for reflowable Word docs. Recover
    # the real page count + per-block page numbers from Word's cached page-break
    # markers so the UI shows the true page breakdown instead of "Pg 1 only".
    if ext == ".docx":
        try:
            para_pages = _docx_paragraph_pages(path)
            if para_pages:
                max_page = max(pg for _, pg in para_pages)
                if max_page > 1:
                    _assign_docx_pages(text_blocks, para_pages)
                    page_count = max(page_count, max_page)
                    logger.info("[%s] DOCX pagination recovered: %d pages", doc_id, page_count)
        except Exception as _pp_exc:
            logger.warning("[%s] DOCX page recovery failed (non-fatal): %s", doc_id, _pp_exc)

    return ParsedDocument(
        doc_id=doc_id,
        filename=path.name,
        raw_text=raw_text,
        text_blocks=text_blocks,
        tables=tables,
        page_count=page_count,
        word_count=len(raw_text.split()),
        has_tables=len(tables) > 0,
        has_images=len(image_pages) > 0,
        image_page_numbers=sorted(image_pages),
        images=images,
        metadata=_docling_metadata(doc),
    )


def _extract_items(doc, page_offset: int = 0, image_index_start: int = 0):
    """Pull text blocks + images out of a Docling document. page_offset shifts
    page numbers when the doc is a chunk (Docling numbers each chunk from 1).
    Returns (text_blocks, raw_parts, images, image_pages)."""
    text_blocks: list[TextBlock] = []
    raw_parts: list[str] = []
    images: list[ExtractedImage] = []
    image_pages: set[int] = set()
    current_section: Optional[str] = None
    current_level: Optional[int] = None

    block_types = {"SectionHeaderItem": "header", "TextItem": "paragraph", "ListItem": "list"}

    for item, _ in doc.iterate_items():
        item_type = type(item).__name__
        if item_type in block_types:
            if item_type == "SectionHeaderItem":
                current_section = item.text
                current_level = getattr(item, "level", 1)
            text_blocks.append(TextBlock(
                text=item.text,
                page_number=_get_page(item) + page_offset,
                block_type=block_types[item_type],
                section_title=current_section,
                section_level=current_level,
                bbox=_get_bbox(item),
                token_count=_simple_token_count(item.text),
            ))
            raw_parts.append(item.text)

        elif item_type == "PictureItem":
            pg = _get_page(item) + page_offset
            image_pages.add(pg)
            try:
                pil = item.get_image(doc)
                if pil is not None:
                    png, w, h = _pil_to_png_bytes(pil)
                    images.append(ExtractedImage(
                        image_index=image_index_start + len(images),
                        page_number=pg,
                        bbox=_get_bbox(item),
                        png_bytes=png,
                        width=w,
                        height=h,
                    ))
            except Exception as img_exc:
                logger.warning("Picture extraction failed on page %s: %s", pg, img_exc)

    return text_blocks, raw_parts, images, image_pages


def _render_table_crop_fitz(pdf_path, table, doc) -> Optional[bytes]:
    """Fallback table crop rendered straight from the PDF via PyMuPDF, used when
    Docling's ``table.get_image(doc)`` returned nothing (e.g. the page image was
    not retained for that page). Guarantees a crop exists so the VLM stage runs
    for EVERY data table — there is no Docling-only path.

    Coordinates: the Docling prov bbox is in Docling page points; convert to a
    top-left origin using the Docling page height, then scale into fitz page
    points (``page.rect``) in case the two page boxes differ. Fail-open — returns
    None on any problem so that one table degrades to Docling-only rather than
    failing the whole parse.
    """
    if not pdf_path:
        return None
    try:
        import fitz  # PyMuPDF
        from app.config import settings

        prov = table.prov[0] if table.prov else None
        if prov is None or prov.bbox is None:
            return None
        page_no = prov.page_no  # 1-based, relative to the doc fitz will open
        dpage = doc.pages.get(page_no) if getattr(doc, "pages", None) else None
        if dpage is None or dpage.size is None:
            return None
        pw, ph = float(dpage.size.width), float(dpage.size.height)
        if pw <= 0 or ph <= 0:
            return None
        tl = prov.bbox.to_top_left_origin(ph)  # l/t/r/b in top-left origin

        scale = float(getattr(settings, "DOCLING_IMAGES_SCALE", 2.0) or 2.0)
        with fitz.open(str(pdf_path)) as pdf:
            if page_no - 1 < 0 or page_no - 1 >= pdf.page_count:
                return None
            fpage = pdf.load_page(page_no - 1)
            frect = fpage.rect
            sx = frect.width / pw if pw else 1.0
            sy = frect.height / ph if ph else 1.0
            x0, x1 = sorted((tl.l * sx, tl.r * sx))
            y0, y1 = sorted((tl.t * sy, tl.b * sy))
            clip = fitz.Rect(x0, y0, x1, y1) & frect  # clamp to page
            if clip.is_empty or clip.width < 2 or clip.height < 2:
                return None
            pix = fpage.get_pixmap(clip=clip, matrix=fitz.Matrix(scale, scale))
            return pix.tobytes("png")
    except Exception as exc:  # fail-open
        logger.debug("fitz table-crop fallback failed: %s", exc)
        return None


def _extract_tables(doc, page_offset: int = 0, table_index_start: int = 0,
                    pdf_path=None) -> list[ExtractedTable]:
    tables: list[ExtractedTable] = []
    for table in doc.tables:
        # Merged-cell + header-row-count span detection (table-store
        # enterprise follow-up — see table_reconstruction._grid_wellformed):
        # captured here from Docling's raw table_cells (the dense `grid` used
        # below always reports uniform row widths, so it can never itself
        # surface span/header info) and preserved in table_metadata for the
        # faithfulness gate and future consumers. Computed BEFORE
        # _parse_table_data so a genuine multi-row header (header_row_count>1,
        # positively confirmed via Docling's own column_header cell flags —
        # never guessed from row content) can be combined into a single
        # header row instead of being misread as data rows.
        span_info = _detect_merged_cells(table)
        headers, rows = _parse_table_data(table, header_row_count=span_info["header_row_count"])
        # Docling's TableFormer flags chart/figure regions (bar charts, pie
        # charts, icon clusters) as tables but extracts no cell grid → 0×0.
        # These produce empty "No table data" entries and placeholder table-crop
        # images. Skip them: their visual content is captured by the image
        # pipeline (VLM → image_store) instead. A real data table always has at
        # least a header row or a body row.
        if not headers and not rows:
            logger.info(
                "Skipping empty-grid table on page %s (no cell data — likely a chart/figure)",
                _get_table_page(table) + page_offset,
            )
            continue
        table_png = None
        try:
            tpil = table.get_image(doc)
            if tpil is not None:
                table_png, _, _ = _pil_to_png_bytes(tpil)
        except Exception:
            table_png = None
        # Guarantee a crop for EVERY data table (universal VLM pipeline): when
        # Docling did not retain a rendered image for this table, render the
        # bbox region straight from the PDF. Without this, crop-less tables fell
        # onto a Docling-only path and never reached the VLM.
        if table_png is None:
            table_png = _render_table_crop_fitz(pdf_path, table, doc)
        has_span_info = span_info["has_merged_cells"] or span_info["header_row_count"] > 1 or span_info["cells"]
        # Docling's TableItem has no `.caption` attribute — only a caption_text(doc)
        # method that resolves the caption ref(s) against the parent document.
        # getattr(table, "caption", None) silently returned None for every table
        # (AttributeError swallowed by the default), even when Docling had
        # correctly detected a caption.
        caption_text = table.caption_text(doc) or None
        tables.append(ExtractedTable(
            # Contiguous index even when earlier tables were skipped.
            table_index=table_index_start + len(tables),
            page_number=_get_table_page(table) + page_offset,
            headers=headers,
            rows=rows,
            caption=caption_text,
            bbox=_get_table_bbox(table),
            raw_text=_table_to_text(headers, rows),
            markdown_text=_table_to_markdown(headers, rows),
            image_png_bytes=table_png,
            table_metadata={"merged_cells": span_info} if has_span_info else {},
        ))
    return tables


def _merge_continued_tables(tables: list[ExtractedTable]) -> list[ExtractedTable]:
    """Merge multi-page table continuations that Docling splits into separate
    table objects at page boundaries — even within a single whole-document
    convert() call with zero chunking (empirically confirmed: a 70-row
    synthetic PDF with continuous row numbering came back as 3 separate
    Docling tables, one per page).

    Detection heuristic (two tables A, B are a continuation pair when ALL hold):
      (a) B.page_number == A.page_number + 1        — strictly consecutive pages
      (b) A.headers == B.headers                     — identical header list,
          exact match (this is exactly what reportlab's repeatRows produces
          for a genuine multi-page continuation in a real report)
      (c) every row in both tables has len(row) == len(headers)  — same column
          count, checked directly on the rows rather than merely implied by (b)

    Consecutive pairs are chained transitively so 3+ page continuations (A
    continues B continues C) collapse into a single group, not just adjacent
    pairs.

    KNOWN FALSE-POSITIVE TRADEOFF (accepted, not a bug): two genuinely
    independent tables with identical column headers on consecutive pages
    (e.g. two unrelated "Name / Value" tables) will be merged by this
    heuristic. This is an explicit, accepted tradeoff for this feature —
    header+adjacency is the strongest cheap signal available without deeper
    semantic analysis, and real multi-page continuations vastly outnumber
    coincidental identical-header adjacent tables in practice.

    Non-continuation tables (the common case) pass through unchanged: no
    `table_metadata['continuation']` key, `row_page_numbers` stays None,
    `table_index` values are still renumbered contiguously (0..N-1) exactly as
    the pre-merge list already guaranteed — this function does not change that
    contract, it just may reduce the number of surviving tables first.
    """
    if not tables:
        return tables

    # Preserve encounter order but group strictly by ascending page number so
    # transitive chaining only ever looks at the immediately-preceding table.
    ordered = sorted(tables, key=lambda t: (t.page_number, t.table_index))

    groups: list[list[ExtractedTable]] = []
    for t in ordered:
        if groups:
            prev = groups[-1][-1]
            same_headers = prev.headers == t.headers
            consecutive_page = t.page_number == prev.page_number + 1
            rows_match_header = all(
                len(r) == len(t.headers) for r in t.rows
            ) and all(
                len(r) == len(prev.headers) for r in prev.rows
            )
            if consecutive_page and same_headers and rows_match_header and t.headers:
                groups[-1].append(t)
                continue
        groups.append([t])

    merged: list[ExtractedTable] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue

        first = group[0]
        combined_rows: list[list[str]] = []
        combined_row_pages: list[int] = []
        for frag in group:
            combined_rows.extend(frag.rows)
            combined_row_pages.extend([frag.page_number] * len(frag.rows))

        merged_metadata = dict(first.table_metadata or {})
        merged_metadata["continuation"] = {
            "is_continuation": True,
            "fragment_count": len(group),
            "fragment_pages": [f.page_number for f in group],
            "fragment_table_indices": [f.table_index for f in group],
        }

        merged_table = ExtractedTable(
            table_index=first.table_index,  # renumbered contiguously below
            page_number=first.page_number,
            headers=first.headers,
            rows=combined_rows,
            caption=first.caption,
            bbox=first.bbox,
            raw_text=_table_to_text(first.headers, combined_rows),
            markdown_text=_table_to_markdown(first.headers, combined_rows),
            image_png_bytes=first.image_png_bytes,
            table_metadata=merged_metadata,
            row_page_numbers=combined_row_pages,
        )
        merged.append(merged_table)
        logger.info(
            "Merged %d-page table continuation (pages %s) into one logical table",
            len(group), [f.page_number for f in group],
        )

    # Renumber table_index contiguously across the final merged list — same
    # contract the pre-merge extraction already provided (contiguous even when
    # earlier tables were skipped), now also holding after continuation-merge
    # collapses some entries. Downstream code (crop rendering, table_store's
    # unique (document_id, table_index) index) depends on this contract.
    for i, t in enumerate(merged):
        t.table_index = i

    return merged


# ── caption re-association ────────────────────────────────────────────────────

# A genuine table caption line STARTS with "Table N" followed by a separator
# (":", ".", en/em dash). This deliberately excludes prose that merely mentions
# a table mid-sentence ("… Table 6 presents this assessment …"), which does not
# start with "Table N".
_TABLE_CAPTION_RE = re.compile(r"^\s*Table\s+(\d+)\s*[:.–—\-]", re.IGNORECASE)
# Looser: any "Table N" token, used only to read a table's OWN number out of its
# embedded title bar / header / body cells (never out of its Docling caption,
# which may itself be the wrong one we're trying to correct).
_TABLE_NUM_RE = re.compile(r"\bTable\s+(\d+)\b", re.IGNORECASE)


def _caption_candidates(text_blocks: list) -> list[dict]:
    """Text blocks that look like a standalone table caption ("Table N: …").
    Returns dicts {num, text, page, y1} in reading order (page, then vertical)."""
    out: list[dict] = []
    for b in text_blocks:
        t = (b.text or "").strip()
        if not t or len(t) > 300:
            continue
        m = _TABLE_CAPTION_RE.match(t)
        if not m:
            continue
        y1 = b.bbox.y1 if getattr(b, "bbox", None) else 0.0
        out.append({"num": int(m.group(1)), "text": t, "page": b.page_number, "y1": y1})
    out.sort(key=lambda c: (c["page"], c["y1"]))
    return out


def _table_own_number(table) -> Optional[int]:
    """Read the table's own number from its embedded content — header cells, the
    first couple of rows, or the serialized text (which is where a "Table N — …"
    title bar lands). Deliberately does NOT look at table.caption, since that is
    exactly the possibly-wrong value we may be correcting."""
    fields: list[str] = [" ".join(str(h) for h in (table.headers or []))]
    for row in (table.rows or [])[:2]:
        fields.append(" ".join(str(c) for c in row))
    fields.append((table.raw_text or "")[:200])
    fields.append((table.markdown_text or "")[:200])
    for f in fields:
        m = _TABLE_NUM_RE.search(f)
        if m:
            return int(m.group(1))
    return None


def _reassign_table_captions(tables: list, text_blocks: list) -> list:
    """Correct table captions that Docling mis-associated.

    Docling resolves each table's caption against the (page-batch-local) document
    it was parsed in, so when a caption and its table are split across a chunk
    boundary — or two independent tables are merged — a table can end up carrying
    a *neighbouring* table's title. This pass re-derives the caption from the
    aggregated text blocks, which carry global page numbers and are immune to
    batch boundaries.

    Strategy per table, in document reading order:
      1. If the table's own embedded number K is detectable AND exactly one
         unconsumed caption candidate is numbered K → use it (high confidence).
      2. Otherwise use the nearest unconsumed caption candidate positioned at or
         above the table (same page above it, or an earlier page).
    Each candidate is consumed once so two tables never share one caption. A
    table keeps its existing caption when no candidate can be matched (never
    downgraded to None).
    """
    if not tables:
        return tables

    candidates = _caption_candidates(text_blocks)
    if not candidates:
        return tables

    def _table_pos(t) -> tuple[int, float]:
        y1 = t.bbox.y1 if getattr(t, "bbox", None) else 0.0
        return (t.page_number, y1)

    tables_in_order = sorted(tables, key=lambda t: (_table_pos(t), t.table_index))
    consumed: set[int] = set()

    for t in tables_in_order:
        tp = _table_pos(t)
        chosen: Optional[int] = None

        # 1. Number match against the table's own embedded number.
        own = _table_own_number(t)
        if own is not None:
            matches = [
                i for i, c in enumerate(candidates)
                if i not in consumed and c["num"] == own
            ]
            if len(matches) == 1:
                chosen = matches[0]

        # 2. Positional fallback: nearest unconsumed caption at/above the table.
        if chosen is None:
            best_i, best_pos = None, None
            for i, c in enumerate(candidates):
                if i in consumed:
                    continue
                cpos = (c["page"], c["y1"])
                # caption must not sit below the table (allow small tolerance for
                # a caption and table sharing a line/near-identical y).
                if cpos > (tp[0], tp[1] + 2.0):
                    continue
                if best_pos is None or cpos > best_pos:
                    best_pos, best_i = cpos, i
            chosen = best_i

        if chosen is not None:
            new_caption = candidates[chosen]["text"]
            if new_caption != (t.caption or ""):
                logger.info(
                    "Re-assigned caption for table %d (page %s): %r → %r",
                    t.table_index, t.page_number,
                    (t.caption or "")[:60], new_caption[:60],
                )
            t.caption = new_caption
            consumed.add(chosen)

    return tables


def _docling_metadata(doc) -> dict:
    metadata: dict = {}
    if hasattr(doc, "metadata") and doc.metadata:
        m = doc.metadata
        metadata["title"] = getattr(m, "title", None)
        metadata["author"] = getattr(m, "author", None)
        metadata["creation_date"] = str(getattr(m, "creation_date", None))
    return metadata


def _parse_with_docling(path: Path, doc_id: str) -> ParsedDocument:
    """Whole-document pass (single Docling convert). Fallback for the chunked
    path; correct but no progress and unbounded memory on huge PDFs."""
    converter = _make_converter(do_ocr=_resolve_do_ocr(path))
    doc = converter.convert(str(path)).document

    text_blocks, raw_parts, images, image_pages = _extract_items(doc)
    tables = _extract_tables(doc, pdf_path=path)
    tables = _merge_continued_tables(tables)
    tables = _reassign_table_captions(tables, text_blocks)
    raw_text = "\n\n".join(raw_parts)

    return ParsedDocument(
        doc_id=doc_id,
        filename=path.name,
        raw_text=raw_text,
        text_blocks=text_blocks,
        tables=tables,
        page_count=len(doc.pages) if hasattr(doc, "pages") else 1,
        word_count=len(raw_text.split()),
        has_tables=len(tables) > 0,
        has_images=len(image_pages) > 0,
        image_page_numbers=sorted(image_pages),
        images=images,
        metadata=_docling_metadata(doc),
    )


def _adaptive_chunk_size(total_pages: int) -> int:
    """Pages per Docling convert. Small docs → 1 (finest progress); large docs
    grow the chunk so we stay at ~25–40 progress updates while capping memory
    at ≤8 pages in flight. 5p→1, 50p→2, 200p→8, 1000p→8."""
    if total_pages <= 1:
        return 1
    return max(1, min(8, math.ceil(total_pages / 25)))


def _merge_pages(total: int, real: dict, prescan: Optional[list]) -> list[dict]:
    """Per-page list for the UI: real counts for finished pages (done=True),
    pre-scan estimate for pages not parsed yet (done=False)."""
    pre = {p["page"]: p for p in (prescan or [])}
    out: list[dict] = []
    for p in range(1, total + 1):
        if p in real:
            out.append({**real[p], "page": p, "done": True})
        else:
            est = pre.get(p, {})
            out.append({
                "page": p,
                "images": est.get("images", 0),
                "est_words": est.get("est_words", 0),
                "done": False,
            })
    return out


def parse_document_chunked(
    path: Path,
    doc_id: str,
    prescan: Optional[list] = None,
    on_progress: Optional[Callable[[int, int, list], None]] = None,
    chunk_size: Optional[int] = None,
) -> ParsedDocument:
    """Parse a PDF a few pages at a time. Memory stays bounded to one chunk, a
    failed chunk is skipped (not fatal), and on_progress fires after every chunk
    with (pages_done, total_pages, per_page_list) for live UI updates.

    Page numbers/indices are offset so the aggregated result is equivalent to a
    whole-document parse (caveat: a table spanning a chunk boundary splits)."""
    import fitz  # PyMuPDF — page splitting

    converter = _make_converter(do_ocr=_resolve_do_ocr(path))
    src = fitz.open(str(path))
    try:
        total = src.page_count
        if total <= 0:
            raise ParsingError(path.name, "0 pages")
        cs = chunk_size or _adaptive_chunk_size(total)

        all_blocks: list[TextBlock] = []
        all_raw: list[str] = []
        all_images: list[ExtractedImage] = []
        all_tables: list[ExtractedTable] = []
        image_pages: set[int] = set()
        real_by_page: dict[int, dict] = {}

        for start in range(0, total, cs):
            end = min(start + cs, total)  # exclusive, 0-based
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.close()
            chunk = fitz.open()
            try:
                chunk.insert_pdf(src, from_page=start, to_page=end - 1)
                chunk.save(tmp.name)
            finally:
                chunk.close()

            try:
                cdoc = converter.convert(tmp.name).document
                tb, rp, imgs, ip = _extract_items(cdoc, page_offset=start, image_index_start=len(all_images))
                # pdf_path = this chunk's temp PDF (page_no from Docling is
                # chunk-local, so the fitz fallback opens the same chunk file).
                tbls = _extract_tables(cdoc, page_offset=start, table_index_start=len(all_tables),
                                       pdf_path=tmp.name)
                all_blocks += tb
                all_raw += rp
                all_images += imgs
                all_tables += tbls
                image_pages |= ip
                # fresh per-page counts for this chunk's pages (1-based global)
                for p in range(start + 1, end + 1):
                    real_by_page[p] = {"page": p, "blocks": 0, "tables": 0, "images": 0, "est_words": 0}
                for b in tb:
                    e = real_by_page.get(b.page_number)
                    if e:
                        e["blocks"] += 1
                        e["est_words"] += b.token_count or len(b.text.split())
                for t in tbls:
                    e = real_by_page.get(t.page_number)
                    if e:
                        e["tables"] += 1
                for im in imgs:
                    e = real_by_page.get(im.page_number)
                    if e:
                        e["images"] += 1
            except Exception as ce:
                logger.warning("[%s] chunk pages %d-%d failed (skipped): %s", doc_id, start + 1, end, ce)
                for p in range(start + 1, end + 1):
                    real_by_page[p] = {"page": p, "blocks": 0, "tables": 0, "images": 0, "est_words": 0, "failed": True}
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

            if on_progress:
                try:
                    on_progress(end, total, _merge_pages(total, real_by_page, prescan))
                except Exception as pe:
                    logger.debug("[%s] progress callback error: %s", doc_id, pe)

        meta = {"title": src.metadata.get("title") if src.metadata else None,
                "author": src.metadata.get("author") if src.metadata else None}
    finally:
        src.close()

    raw_text = "\n\n".join(all_raw)
    all_tables = _merge_continued_tables(all_tables)
    all_tables = _reassign_table_captions(all_tables, all_blocks)
    return ParsedDocument(
        doc_id=doc_id,
        filename=path.name,
        raw_text=raw_text,
        text_blocks=all_blocks,
        tables=all_tables,
        page_count=total,
        word_count=len(raw_text.split()),
        has_tables=len(all_tables) > 0,
        has_images=len(image_pages) > 0,
        image_page_numbers=sorted(image_pages),
        images=all_images,
        metadata=meta,
    )


def _parse_fallback(path: Path, doc_id: str) -> ParsedDocument:
    """Minimal fallback using PyMuPDF if Docling fails."""
    try:
        import fitz  # PyMuPDF
        pdf = fitz.open(str(path))
        pages_text = [page.get_text() for page in pdf]
        raw_text = "\n\n".join(pages_text)
        pdf.close()
        page_count = len(pages_text)
    except Exception:
        raw_text = ""
        page_count = 0

    blocks = [
        TextBlock(
            text=para.strip(),
            page_number=i + 1,
            block_type="paragraph",
            token_count=_simple_token_count(para.strip()),
        )
        for i, page_text in enumerate(raw_text.split("\n\n"))
        for para in [page_text.strip()]
        if para.strip()
    ]

    return ParsedDocument(
        doc_id=doc_id,
        filename=path.name,
        raw_text=raw_text,
        text_blocks=blocks,
        tables=[],
        page_count=page_count,
        word_count=len(raw_text.split()),
        has_tables=False,
        has_images=False,
        metadata={},
    )


# ── Helpers ───────────────────────────────────────────────────

def _get_page(item) -> int:
    try:
        return item.prov[0].page_no if item.prov else 1
    except Exception:
        return 1


def _get_bbox(item) -> Optional[BoundingBox]:
    try:
        b = item.prov[0].bbox
        return BoundingBox(x1=b.l, y1=b.t, x2=b.r, y2=b.b)
    except Exception:
        return None


def _get_table_page(table) -> int:
    try:
        return table.prov[0].page_no if table.prov else 1
    except Exception:
        return 1


def _get_table_bbox(table) -> Optional[BoundingBox]:
    try:
        b = table.prov[0].bbox
        return BoundingBox(x1=b.l, y1=b.t, x2=b.r, y2=b.b)
    except Exception:
        return None


def _parse_table_data(table, header_row_count: int = 0) -> tuple[list[str], list[list[str]]]:
    """Split Docling's dense ``grid`` into (headers, rows).

    Conservative multi-row-header fix: when ``header_row_count`` (computed by
    _detect_merged_cells from Docling's OWN column_header cell flags — never
    from heuristics about row content) is 2 or more, combine that many leading
    grid rows into a single header row by joining each column's text across
    those rows (e.g. "Q1 2024" over "Budget" -> "Q1 2024 - Budget"), and
    exclude all of those rows from the data ``rows`` list.

    For header_row_count == 0 or 1 (the overwhelming common case, and the only
    case possible when the caller doesn't pass span info) behaviour is
    IDENTICAL to before this change: row 0 is the header, rows[1:] are data.
    header_row_count is clamped to len(grid) - 1 defensively so a table with
    only header rows and no data can't wipe out its own header via an
    out-of-range slice, and to at least 1 so it never disables the header row
    entirely.
    """
    try:
        grid = table.data.grid
        if not grid:
            return [], []

        if header_row_count and header_row_count > 1:
            # Clamp: never consume the entire grid as "header" (leave at
            # least a 0-row data section is fine, but never go negative/out
            # of range), and never go below 1 (that would mean "no header").
            hrc = max(1, min(header_row_count, len(grid) - 1)) if len(grid) > 1 else 1
        else:
            hrc = 1

        if hrc <= 1:
            headers = [str(cell.text) for cell in grid[0]]
            rows = [[str(cell.text) for cell in row] for row in grid[1:]]
            return headers, rows

        # Multi-row header: combine text from each of the leading `hrc` rows,
        # per column, skipping blank/duplicate fragments (a spanned header
        # cell's text is duplicated by Docling's `grid` into every column it
        # covers, and a sub-header directly beneath a blank super-header cell
        # should not gain a stray leading separator).
        num_cols = len(grid[0])
        combined_headers: list[str] = []
        for col_idx in range(num_cols):
            parts: list[str] = []
            for row_idx in range(hrc):
                if col_idx < len(grid[row_idx]):
                    text = str(grid[row_idx][col_idx].text or "").strip()
                    if text and (not parts or parts[-1] != text):
                        parts.append(text)
            combined_headers.append(" - ".join(parts))
        data_rows = [[str(cell.text) for cell in row] for row in grid[hrc:]]
        return combined_headers, data_rows
    except Exception:
        return [], []


def _detect_merged_cells(table) -> dict:
    """Inspect Docling's raw ``table.data.table_cells`` (NOT the dense ``grid``
    property, which duplicates a spanned cell's text into every position it
    covers and therefore always reports uniform row widths). ``table_cells``
    retains the true ``row_span``/``col_span`` Docling's TableFormer assigned,
    so this is the only place genuine merged-cell geometry is still visible.

    This is the single canonical span-detection function used across the
    pipeline: storage_service._store_tables persists its return value verbatim
    into table_metadata['merged_cells'], and table_reconstruction's
    faithfulness gate reads only the ``has_merged_cells`` key from it — both
    of those integrations are additive-only and unaffected by the new keys
    added here (header_row_count, cells).

    Verified against the installed docling_core 2.74.0
    (docling_core.types.doc.document.TableCell) field names:
      row_span: int = 1
      col_span: int = 1
      start_row_offset_idx: int   # 0-based, inclusive
      end_row_offset_idx: int     # 0-based, EXCLUSIVE — TableData.grid builds
                                  # the dense grid via range(start, end)
      start_col_offset_idx: int   # 0-based, inclusive
      end_col_offset_idx: int     # 0-based, EXCLUSIVE
      text: str
      column_header: bool = False  # TableFormer's real "this cell is part of
                                    # a header row" flag (also set by the
                                    # HTML/XML backends) — used below both for
                                    # is_header and header_row_count.
      row_header: bool = False     # marks a header *column*, not used here.
    There is no single unified ``is_header`` field on TableCell; the closest
    (and correct, per docling.models.table_structure_model usage) analogue for
    "this is a header row" is ``column_header``.

    Returns a JSON-safe summary (never raises — fail-open to "no span info" so
    a Docling version without these fields, or any parsing hiccup, never
    breaks table extraction):
      {
        "has_merged_cells": bool,
        "max_row_span": int,
        "max_col_span": int,
        "spanned_cell_count": int,
        "header_row_count": int,   # leading contiguous rows (from row 0) that
                                    # Docling's own column_header flag marks
        "cells": [                 # ONLY cells with row_span>1 or col_span>1
          {"row_start": int, "row_end": int, "col_start": int, "col_end": int,
           "text": str, "is_header": bool},
          ...
        ],
      }
    Absence of the ``table_cells``/span attributes (e.g. older Docling, or a
    non-PDF table shape) yields ``has_merged_cells: False`` rather than an
    error — callers must treat that as "unknown", not "confirmed simple".
    header_row_count/cells are computed independently of has_merged_cells
    (header_row_count matters even when there are zero merged cells).
    """
    summary = {
        "has_merged_cells": False,
        "max_row_span": 1,
        "max_col_span": 1,
        "spanned_cell_count": 0,
        "header_row_count": 0,
        "cells": [],
    }
    try:
        cells = table.data.table_cells
    except Exception:
        return summary
    if not cells:
        return summary

    spanned = 0
    max_row_span = 1
    max_col_span = 1
    span_entries: list[dict] = []
    header_rows: set[int] = set()
    for cell in cells:
        row_span = int(getattr(cell, "row_span", 1) or 1)
        col_span = int(getattr(cell, "col_span", 1) or 1)
        max_row_span = max(max_row_span, row_span)
        max_col_span = max(max_col_span, col_span)

        is_header = bool(getattr(cell, "column_header", False))
        if is_header:
            # A header cell can itself span multiple rows (e.g. a merged
            # header label) — every row it covers counts toward the leading
            # header block, not just its start row.
            try:
                r0 = int(getattr(cell, "start_row_offset_idx"))
                r1 = int(getattr(cell, "end_row_offset_idx"))
                header_rows.update(range(r0, r1))
            except Exception:
                pass

        if row_span > 1 or col_span > 1:
            spanned += 1
            try:
                span_entries.append({
                    "row_start": int(getattr(cell, "start_row_offset_idx")),
                    "row_end": int(getattr(cell, "end_row_offset_idx")),
                    "col_start": int(getattr(cell, "start_col_offset_idx")),
                    "col_end": int(getattr(cell, "end_col_offset_idx")),
                    "text": str(getattr(cell, "text", "") or ""),
                    "is_header": is_header,
                })
            except Exception:
                # Missing offset attrs on this cell (older Docling/test double)
                # — still counted above in spanned_cell_count/max spans, just
                # not representable as a concrete span-map entry.
                pass

    # header_row_count: size of the contiguous leading block of header rows
    # starting at row 0 (a header row appearing after a data row, or a doc
    # with no row-0 header cells at all, does not count).
    header_row_count = 0
    r = 0
    while r in header_rows:
        header_row_count += 1
        r += 1

    summary["has_merged_cells"] = spanned > 0
    summary["max_row_span"] = max_row_span
    summary["max_col_span"] = max_col_span
    summary["spanned_cell_count"] = spanned
    summary["header_row_count"] = header_row_count
    summary["cells"] = span_entries
    return summary


def _table_to_text(headers: list[str], rows: list[list[str]]) -> str:
    lines = [" | ".join(headers)] if headers else []
    for row in rows:
        lines.append(" | ".join(row))
    return "\n".join(lines)


def _table_to_markdown(headers: list[str], rows: list[list[str]]) -> str:
    if not headers:
        return ""
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    header_row = "| " + " | ".join(headers) + " |"
    data_rows = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_row, sep] + data_rows)
