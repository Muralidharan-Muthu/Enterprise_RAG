import os
from pathlib import Path

import pytest

# Load real SUPABASE_* credentials from .env so DB/integration (slow) tests can
# connect. We only pull SUPABASE_* keys (DB + storage) — Groq stays unset for
# unit tests below. Falls back to dummy values when no .env is present (CI).
for _env in (
    Path(__file__).resolve().parent.parent / ".env",          # backend/.env
    Path(__file__).resolve().parent.parent.parent / ".env",   # root .env
):
    if _env.exists():
        for _line in _env.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            if _k.startswith("SUPABASE_"):
                os.environ.setdefault(_k, _v.strip().strip('"').strip("'"))

# Dummy fallbacks for unit tests with no .env present
os.environ.setdefault("SUPABASE_PASSWORD", "test")
os.environ.setdefault("GROQ_BASE_URL", "")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: tests needing models/DB/network (deselect with -m 'not slow')"
    )


@pytest.fixture
def sample_pdf_path(tmp_path):
    """
    Creates a minimal valid PDF in a temp directory.
    Replace with a real PDF path for integration tests.
    """
    try:
        from reportlab.pdfgen import canvas
        pdf_path = tmp_path / "test_document.pdf"
        c = canvas.Canvas(str(pdf_path))
        c.drawString(100, 750, "Multi-Store RAG Chatbot Test Document")
        c.drawString(100, 730, "This is a policy document about data governance.")
        c.drawString(100, 710, "Section 1: Introduction")
        c.drawString(100, 690, "This policy applies to all employees and contractors.")
        c.drawString(100, 670, "Section 2: Scope")
        c.drawString(100, 650, "The scope of this policy covers all data processing activities.")
        c.save()
        return str(pdf_path)
    except ImportError:
        pytest.skip("reportlab not installed — skipping PDF creation fixture")
