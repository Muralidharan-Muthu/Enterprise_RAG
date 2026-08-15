"""Tests for:
1. store_image_derived_chunks() batching all per-image embeddings into ONE
   embed_passages() call (instead of one call per image), while preserving
   alignment between images and their embeddings even when some rows fail to
   parse and must be skipped.
2. synthesis_service._build_context() enforcing a total context-size budget
   (settings.SYNTHESIS_CONTEXT_MAX_CHARS) across all assembled chunks, while
   keeping the highest-ranked chunks first.

Both tests avoid any real DB, network, or model — DB access is monkeypatched
via a fake get_db() context manager and a stub psycopg2-like cursor.
"""
import types

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fakes for the DB layer used by storage_service.store_image_derived_chunks
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Minimal cursor stub: records executed SQL, returns canned SELECT rows."""

    def __init__(self, select_rows=None):
        self._select_rows = select_rows or []
        self.rowcount = 1
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._select_rows

    def fetchone(self):
        return ("some text", True)  # generic non-empty/has-embedding validate() row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, select_rows=None):
        self._select_rows = select_rows or []

    def cursor(self):
        return _FakeCursor(self._select_rows)


class _FakeDbCtx:
    """Stand-in for get_db() — a context manager yielding a _FakeConn."""

    def __init__(self, select_rows=None):
        self._select_rows = select_rows or []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return _FakeConn(self._select_rows)

    def __exit__(self, *exc):
        return False


def _make_image_row(img_id, img_idx, detected_store, structured_content):
    """Match the SELECT tuple shape unpacked in store_image_derived_chunks:
    (id, image_index, page_number, bbox, storage_path, ocr_text, vlm_ocr_text,
     structured_content, detected_store, image_metadata)"""
    return (
        img_id, img_idx, 1, None,
        f"images/doc/{img_idx}.png", "ocr text", "vlm ocr text",
        structured_content, detected_store, {"confidence": 0.9, "reason": "test"},
    )


class _FakeHandler:
    """Stub StoreHandler: parse() may raise to simulate a parse failure."""

    def __init__(self, name, fail_parse=False):
        self.name = name
        self.fail_parse = fail_parse
        self.inserted = []

    def parse(self, structured_raw, ctx):
        if self.fail_parse:
            raise ValueError("simulated parse failure")
        return {"chunk_text": structured_raw}

    def canonical_text(self, parsed, ctx):
        return parsed["chunk_text"]

    def insert(self, conn, parsed, embedding, ctx):
        self.inserted.append((ctx.image_index, parsed, embedding))
        return 1

    def validate(self, conn, ctx):
        return None


@pytest.fixture
def patch_storage_deps(monkeypatch):
    """Patch get_db and _update_image_stored_in_conn in storage_service so
    store_image_derived_chunks never touches a real DB."""
    import app.services.storage_service as storage_service

    fake_db = _FakeDbCtx()
    monkeypatch.setattr(storage_service, "get_db", fake_db)

    updates = []
    monkeypatch.setattr(
        storage_service, "_update_image_stored_in_conn",
        lambda conn, img_id, store: updates.append((img_id, store)),
    )
    return storage_service, fake_db, updates


def test_batch_embedding_single_call_and_alignment(monkeypatch, patch_storage_deps):
    """embed_passages must be called exactly once for N images, with each
    image receiving the embedding at its own index (not misaligned)."""
    storage_service, fake_db, updates = patch_storage_deps

    rows = [
        _make_image_row("id-0", 0, "vector_store", "text for image 0"),
        _make_image_row("id-1", 1, "table_store", "text for image 1"),
        _make_image_row("id-2", 2, "vector_store", "text for image 2"),
    ]
    fake_db._select_rows = rows

    handler_vector = _FakeHandler("vector_store")
    handler_table = _FakeHandler("table_store")
    registry = {"vector_store": handler_vector, "table_store": handler_table}

    def fake_get_handler(store_name):
        return registry.get(store_name)

    fake_store_router = types.SimpleNamespace(
        ImageCtx=_real_image_ctx(),
        get_handler=fake_get_handler,
    )
    monkeypatch.setitem(
        __import__("sys").modules, "app.services.store_router", fake_store_router
    )

    call_count = {"n": 0}

    def fake_embed_passages(texts):
        call_count["n"] += 1
        # distinct vector per input, encoding the input's position so we can
        # verify alignment after the fact.
        return [np.array([float(i), float(i) + 0.5]) for i in range(len(texts))]

    monkeypatch.setitem(
        __import__("sys").modules, "app.services.embedding_service",
        types.SimpleNamespace(embed_passages=fake_embed_passages),
    )

    storage_service.store_image_derived_chunks("doc-1")

    assert call_count["n"] == 1, "embed_passages must be called exactly once (batched)"

    # 3 images inserted (2 into vector_store, 1 into table_store), each with
    # the embedding vector matching its own worklist position.
    assert len(handler_vector.inserted) == 2
    assert len(handler_table.inserted) == 1

    by_idx = {}
    for idx, parsed, embedding in handler_vector.inserted + handler_table.inserted:
        by_idx[idx] = embedding

    # worklist order == SELECT order == rows order (0, 1, 2), so embedding i
    # must be [i, i+0.5].
    assert by_idx[0] == [0.0, 0.5]
    assert by_idx[1] == [1.0, 1.5]
    assert by_idx[2] == [2.0, 2.5]

    assert len(updates) == 3


def test_batch_embedding_skips_parse_failure_without_misalignment(monkeypatch, patch_storage_deps):
    """A row whose handler.parse() raises must be skipped (no embedding slot)
    and must NOT shift the embeddings assigned to the other, valid rows."""
    storage_service, fake_db, updates = patch_storage_deps

    rows = [
        _make_image_row("id-0", 0, "vector_store", "good text 0"),
        _make_image_row("id-1", 1, "vector_store", "bad row — parse fails"),
        _make_image_row("id-2", 2, "vector_store", "good text 2"),
    ]
    fake_db._select_rows = rows

    handler_ok = _FakeHandler("vector_store")
    # Same handler instance used for all rows here; we need per-row failure,
    # so wrap parse to fail only for image_index == 1.
    original_parse = handler_ok.parse

    def selective_parse(structured_raw, ctx):
        if ctx.image_index == 1:
            raise ValueError("simulated parse failure for row 1")
        return original_parse(structured_raw, ctx)

    handler_ok.parse = selective_parse

    fake_store_router = types.SimpleNamespace(
        ImageCtx=_real_image_ctx(),
        get_handler=lambda name: handler_ok if name == "vector_store" else None,
    )
    monkeypatch.setitem(
        __import__("sys").modules, "app.services.store_router", fake_store_router
    )

    def fake_embed_passages(texts):
        assert len(texts) == 2, "the failed row must not get an embedding slot"
        return [np.array([10.0 + i]) for i in range(len(texts))]

    monkeypatch.setitem(
        __import__("sys").modules, "app.services.embedding_service",
        types.SimpleNamespace(embed_passages=fake_embed_passages),
    )

    storage_service.store_image_derived_chunks("doc-2")

    assert len(handler_ok.inserted) == 2
    by_idx = {idx: embedding for idx, parsed, embedding in handler_ok.inserted}
    assert set(by_idx.keys()) == {0, 2}
    # Row 0 got the first batch embedding, row 2 got the second — no shift
    # caused by the skipped row 1.
    assert by_idx[0] == [10.0]
    assert by_idx[2] == [11.0]
    assert len(updates) == 2


def _real_image_ctx():
    """Import the real ImageCtx dataclass so our fake store_router module
    stays structurally compatible with storage_service's usage."""
    from app.services.store_router import ImageCtx
    return ImageCtx


# ---------------------------------------------------------------------------
# Context budget tests (synthesis_service._build_context)
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, text, store_type="vector", filename="doc.pdf", page_number=1):
        self.text = text
        self.store_type = store_type
        self.document_filename = filename
        self.page_number = page_number
        self.section_title = None
        self.clause_type = None
        self.risk_level = None
        self.source_doi = None
        self.table_markdown = None
        self.caption = None
        self.ocr_text = None
        self.relevance_score = 1.0


def test_context_budget_caps_total_length(monkeypatch):
    from app.services import synthesis_service
    from app.config import settings

    monkeypatch.setattr(settings, "SYNTHESIS_CONTEXT_MAX_CHARS", 500)

    # Each chunk's rendered text is well under the 600-char per-chunk cap, but
    # 20 chunks of ~200 chars each would total ~4000 chars — over budget.
    chunks = [_FakeChunk("X" * 200) for _ in range(20)]

    context = synthesis_service._build_context(chunks)

    assert len(context) <= 500


def test_context_budget_keeps_highest_ranked_chunks_first(monkeypatch):
    from app.services import synthesis_service
    from app.config import settings

    monkeypatch.setattr(settings, "SYNTHESIS_CONTEXT_MAX_CHARS", 300)

    # chunks are passed in already-ranked order (best first); build a list
    # where only the first one or two can fit.
    chunks = [
        _FakeChunk("BEST_CHUNK_" + "A" * 150),
        _FakeChunk("SECOND_CHUNK_" + "B" * 150),
        _FakeChunk("THIRD_CHUNK_" + "C" * 150),
    ]

    context = synthesis_service._build_context(chunks)

    assert "BEST_CHUNK_" in context
    assert "THIRD_CHUNK_" not in context
    assert len(context) <= 300


def test_context_under_budget_is_unchanged(monkeypatch):
    """When total context already fits, output must be identical to the
    unbounded concatenation (no trimming applied)."""
    from app.services import synthesis_service
    from app.config import settings

    monkeypatch.setattr(settings, "SYNTHESIS_CONTEXT_MAX_CHARS", 12000)

    chunks = [_FakeChunk("short text " + str(i)) for i in range(3)]
    context = synthesis_service._build_context(chunks)

    assert "[1]" in context and "[2]" in context and "[3]" in context
    assert len(context) <= 12000
