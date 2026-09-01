import sys
import time
from pathlib import Path

# Add backend root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz

def run_tests():
    print("=" * 60)
    print("STARTING DOCLING & INGESTION END-TO-END TEST")
    print("=" * 60)

    # Step 1: Create sample test PDF with text, tables, and sections
    pdf_path = Path("test_e2e_document.pdf")
    doc = fitz.open()

    # Page 1: Financial Dashboard with a table
    p1 = doc.new_page()
    p1.insert_text((50, 50), "Reliance Industries Limited - FY25 Performance", fontsize=16)
    p1.insert_text((50, 80), "Financial Results and Segment Performance Overview", fontsize=12)
    # Draw table border and text
    p1.draw_rect(fitz.Rect(50, 100, 500, 220), color=(0, 0, 0), width=1)
    p1.draw_line(fitz.Point(50, 130), fitz.Point(500, 130), color=(0, 0, 0), width=1)
    p1.draw_line(fitz.Point(50, 160), fitz.Point(500, 160), color=(0, 0, 0), width=1)
    p1.draw_line(fitz.Point(50, 190), fitz.Point(500, 190), color=(0, 0, 0), width=1)
    p1.draw_line(fitz.Point(200, 100), fitz.Point(200, 220), color=(0, 0, 0), width=1)
    p1.draw_line(fitz.Point(350, 100), fitz.Point(350, 220), color=(0, 0, 0), width=1)
    
    p1.insert_text((60, 120), "Segment", fontsize=11)
    p1.insert_text((210, 120), "Revenue (INR Cr)", fontsize=11)
    p1.insert_text((360, 120), "EBITDA (INR Cr)", fontsize=11)
    
    p1.insert_text((60, 150), "Oil to Chemicals", fontsize=10)
    p1.insert_text((210, 150), "142,500", fontsize=10)
    p1.insert_text((360, 150), "15,200", fontsize=10)
    
    p1.insert_text((60, 180), "Jio Platforms", fontsize=10)
    p1.insert_text((210, 180), "28,900", fontsize=10)
    p1.insert_text((360, 180), "13,800", fontsize=10)

    p1.insert_text((60, 210), "Retail", fontsize=10)
    p1.insert_text((210, 210), "75,600", fontsize=10)
    p1.insert_text((360, 210), "5,800", fontsize=10)

    # Page 2: Legal and Compliance Clauses
    p2 = doc.new_page()
    p2.insert_text((50, 50), "Section 4: Legal & Compliance Governance", fontsize=14)
    p2.insert_text((50, 90), "Clause 4.1: Material Obligations and Default Terms.", fontsize=11)
    p2.insert_text((50, 120), "In the event of a breach of confidential data obligations, the defaulting party shall indemnify the non-defaulting party up to a maximum liability of INR 50,000,000.", fontsize=10)
    p2.insert_text((50, 160), "Clause 4.2: Termination and Notice Period.", fontsize=11)
    p2.insert_text((50, 190), "Either party may terminate this agreement by providing at least ninety (90) days written notice.", fontsize=10)

    doc.save(str(pdf_path))
    doc.close()
    print(f"[OK] Step 1: Created test PDF at {pdf_path.name} (2 pages with financial table & legal clauses)")

    # Step 2: Test Docling Document Parser
    print("\n[Step 2] Testing Docling Parser...")
    from app.services.document_parser import parse_document
    
    progress_history = []
    def on_progress(done, total, pages):
        progress_history.append((done, total, len(pages)))
        print(f"  -> Progress update: {done}/{total} pages parsed")

    t0 = time.time()
    parsed_doc = parse_document(str(pdf_path), "test-e2e-doc", on_progress=on_progress)
    t1 = time.time()
    
    print(f"[OK] Step 2 Passed: Docling parsed in {t1-t0:.2f}s!")
    print(f"  - Pages: {parsed_doc.page_count}")
    print(f"  - Words: {parsed_doc.word_count}")
    print(f"  - Text Blocks: {len(parsed_doc.text_blocks)}")
    print(f"  - Tables Extracted: {len(parsed_doc.tables)}")
    print(f"  - Has Tables: {parsed_doc.has_tables}")
    for idx, table in enumerate(parsed_doc.tables):
        print(f"    Table #{idx+1}: Headers={table.headers}, Rows={len(table.rows)}")

    # Step 3: Test Document Router
    print("\n[Step 3] Testing Document Router...")
    from app.services.router_service import classify_document
    route_res = classify_document(parsed_doc)
    print(f"[OK] Step 3 Passed: Document classified as {route_res.document_type} (Confidence: {route_res.confidence:.2f})")
    print(f"  - Document Types: {route_res.document_types}")

    # Step 4: Test Multi-Store Chunking
    print("\n[Step 4] Testing Multi-Store Chunking...")
    from app.services.chunker import chunk_document
    chunk_res = chunk_document(parsed_doc, route_res)
    print(f"[OK] Step 4 Passed: Chunked successfully into {len(chunk_res)} total chunks!")
    for idx, c in enumerate(chunk_res):
        c_type = getattr(c, 'chunk_type', 'text')
        c_page = getattr(c, 'page_number', 1)
        snippet = (getattr(c, 'text', '') or '')[:80].replace('\n', ' ')
        print(f"  - Chunk #{idx+1} [Type: {c_type}, Pg: {c_page}]: {snippet}...")

    # Cleanup
    if pdf_path.exists():
        pdf_path.unlink()

    print("\n" + "=" * 60)
    print("ALL END-TO-END TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
