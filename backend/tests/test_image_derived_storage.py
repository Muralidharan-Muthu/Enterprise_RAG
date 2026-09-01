"""
Tests for the generic, registry-driven store_image_derived_chunks() and related helpers.

Replaces the old _store_image_as_*_conn tests (those helpers were removed when the
registry was introduced).  All tests are pure-Python — no live DB, no real BGE model.

Coverage:
  Part 1  — generic dispatch: get_handler() is consulted per image; unknown/image_store
             detected_store is skipped; handler.parse / canonical_text / insert / validate
             are all called in order.
  Part 2  — per-image atomicity and error isolation:
             * one image's exception does not abort the batch
             * rowcount < 1 raises RuntimeError and stored_in is NOT updated
             * validate() failure leaves stored_in unchanged (txn rolls back)
             * embed_passages is called with the canonical text
  Part 3  — idempotency pre-delete runs before the per-image loop
  Part 4  — _ensure_table_crop_ocr_in_table_store SQL includes from_image_store (unchanged helper)
"""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import pytest

import app.services.storage_service as svc


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_conn():
    """Return a mock (conn, cur) pair that supports context-manager cursor usage."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return conn, cur


def _fake_embedding(dim: int = 4) -> list:
    return [0.1] * dim


def _image_row(
    img_id="id-0",
    img_idx=0,
    page_num=1,
    bbox=None,
    storage_path=None,
    ocr_text="ocr text",
    vlm_ocr_text=None,
    structured_content='{"text":"hello"}',
    detected_store="vector_store",
    image_metadata=None,
):
    """Build a fake DB row tuple matching the new SELECT column order."""
    return (
        img_id, img_idx, page_num, bbox,
        storage_path, ocr_text, vlm_ocr_text,
        structured_content, detected_store,
        image_metadata or {},
    )


# ---------------------------------------------------------------------------
# Fake StoreHandler for unit-testing the generic loop
# ---------------------------------------------------------------------------

class FakeHandler:
    """Configurable fake that records every call made to it."""

    def __init__(
        self,
        canonical="canonical text here",
        insert_rowcount=1,
        insert_raises=None,
        validate_raises=None,
    ):
        self.canonical = canonical
        self.insert_rowcount = insert_rowcount
        self.insert_raises = insert_raises
        self.validate_raises = validate_raises
        self.parse_calls = []
        self.canonical_calls = []
        self.insert_calls = []
        self.validate_calls = []

    def parse(self, structured_raw, ctx):
        self.parse_calls.append((structured_raw, ctx))
        return {"chunk_text": structured_raw or ctx.ocr_text or ""}

    def canonical_text(self, parsed, ctx):
        self.canonical_calls.append((parsed, ctx))
        return self.canonical

    def insert(self, conn, parsed, embedding, ctx):
        self.insert_calls.append((conn, parsed, embedding, ctx))
        if self.insert_raises is not None:
            raise self.insert_raises
        return self.insert_rowcount

    def validate(self, conn, ctx):
        self.validate_calls.append((conn, ctx))
        if self.validate_raises is not None:
            raise self.validate_raises


# ---------------------------------------------------------------------------
# get_db factory helpers
# ---------------------------------------------------------------------------

def _make_get_db_factory(rows_by_call):
    """
    Return a fake get_db context manager factory.

    rows_by_call: dict mapping 1-based call index -> fetchall result.
    Any call index not in the dict yields a plain mock conn.
    """
    state = {"n": 0}

    @contextmanager
    def fake_get_db():
        state["n"] += 1
        n = state["n"]
        if n in rows_by_call:
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.fetchall.return_value = rows_by_call[n]
            conn = MagicMock()
            conn.cursor.return_value = cur
            yield conn
        else:
            conn, _ = _make_conn()
            yield conn

    return fake_get_db


def _simple_get_db(db_rows):
    """
    Convenience factory for store_image_derived_chunks():
      call 1 = SELECT (returns db_rows)
      call 2 = idempotency pre-delete (plain mock conn)
      calls 3+ = per-image write connections (plain mock conn)
    """
    return _make_get_db_factory({1: db_rows})


# ---------------------------------------------------------------------------
# Part 1 — generic dispatch
# ---------------------------------------------------------------------------

class TestGenericDispatch:
    """get_handler() is consulted; handler methods are called in correct order."""

    def test_handler_is_consulted(self):
        """get_handler() must be called with the row's detected_store."""
        handler = FakeHandler()
        rows = [_image_row(detected_store="vector_store")]

        fake_embed = MagicMock(return_value=[MagicMock(tolist=lambda: _fake_embedding())])
        # Wrap tolist so it works: embed_passages([text])[0].tolist()
        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler) as mock_get_handler, \
             patch("app.services.embedding_service.embed_passages", fake_embed):
            svc.store_image_derived_chunks("doc1")

        mock_get_handler.assert_called_once_with("vector_store")

    def test_parse_canonical_insert_validate_order(self):
        """parse → canonical_text → embed → insert → validate — in that order."""
        handler = FakeHandler(canonical="the canonical text")
        rows = [_image_row(structured_content='{"text":"hello"}', detected_store="vector_store")]

        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages", fake_embed):
            svc.store_image_derived_chunks("doc1")

        assert len(handler.parse_calls) == 1
        assert len(handler.canonical_calls) == 1
        assert len(handler.insert_calls) == 1
        assert len(handler.validate_calls) == 1
        # embed_passages was called with the canonical text
        fake_embed.assert_called_once_with(["the canonical text"])

    def test_none_handler_skips_image(self):
        """When get_handler returns None, the image is skipped (stored_in unchanged)."""
        rows = [_image_row(detected_store="unknown_store")]

        update_calls = []

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=None), \
             patch("app.services.storage_service._update_image_stored_in_conn",
                   side_effect=lambda c, i, s: update_calls.append(s)):
            svc.store_image_derived_chunks("doc1")

        assert update_calls == [], "stored_in must not be updated when handler is None"

    def test_multiple_images_each_get_own_handler_lookup(self):
        """Each image row triggers a separate get_handler() call."""
        handler_a = FakeHandler(canonical="text a")
        handler_b = FakeHandler(canonical="text b")
        rows = [
            _image_row(img_id="id-0", img_idx=0, detected_store="vector_store"),
            _image_row(img_id="id-1", img_idx=1, detected_store="clause_store"),
        ]

        # embeddings are now computed in ONE batched call — return one vector
        # per input text so the two work-list entries stay aligned.
        def fake_embed_impl(texts):
            out = []
            for _ in texts:
                arr = MagicMock()
                arr.tolist.return_value = _fake_embedding()
                out.append(arr)
            return out
        fake_embed = MagicMock(side_effect=fake_embed_impl)

        handlers = {"vector_store": handler_a, "clause_store": handler_b}

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", side_effect=lambda s: handlers.get(s)), \
             patch("app.services.embedding_service.embed_passages", fake_embed):
            svc.store_image_derived_chunks("doc1")

        assert len(handler_a.insert_calls) == 1
        assert len(handler_b.insert_calls) == 1


# ---------------------------------------------------------------------------
# Part 2 — atomicity and error isolation
# ---------------------------------------------------------------------------

class TestAtomicityAndErrorIsolation:
    """Per-image failure must not abort the batch; stored_in stays honest on error."""

    def test_exception_in_one_image_does_not_abort_batch(self):
        """When image 1's insert raises, images 0 and 2 still succeed.

        get_db call order:
          1 = SELECT (rows returned)
          2 = pre-delete
          3 = image 0 write (vector_store — succeeds)
          4 = image 1 write (clause_store — handler.insert raises, caught by get_db context)
          5 = image 2 write (table_store — succeeds)
        """
        rows = [
            _image_row(img_id="id-0", img_idx=0, detected_store="vector_store"),
            _image_row(img_id="id-1", img_idx=1, detected_store="clause_store"),
            _image_row(img_id="id-2", img_idx=2, detected_store="table_store"),
        ]

        handler_ok = FakeHandler(canonical="good text")
        handler_fail = FakeHandler(
            canonical="bad text",
            insert_raises=RuntimeError("DB write failed"),
        )
        handlers = {
            "vector_store": handler_ok,
            "clause_store": handler_fail,
            "table_store": handler_ok,
        }

        # embeddings are now computed in ONE batched call — return one vector
        # per input text so all three work-list entries stay aligned.
        def fake_embed_impl(texts):
            out = []
            for _ in texts:
                arr = MagicMock()
                arr.tolist.return_value = _fake_embedding()
                out.append(arr)
            return out
        fake_embed = MagicMock(side_effect=fake_embed_impl)

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", side_effect=lambda s: handlers.get(s)), \
             patch("app.services.embedding_service.embed_passages", fake_embed):
            # Must not raise even though clause_store insert fails
            svc.store_image_derived_chunks("doc1")

        # handler_ok is shared for vector_store (idx 0) and table_store (idx 2) = 2 calls
        assert len(handler_ok.insert_calls) == 2
        # handler_fail attempted once
        assert len(handler_fail.insert_calls) == 1

    def test_rowcount_zero_raises_and_stored_in_not_updated(self):
        """insert() returning 0 must trigger RuntimeError and leave stored_in unchanged."""
        rows = [_image_row(detected_store="vector_store")]

        handler = FakeHandler(canonical="text", insert_rowcount=0)
        update_calls = []

        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages", fake_embed), \
             patch("app.services.storage_service._update_image_stored_in_conn",
                   side_effect=lambda c, i, s: update_calls.append(s)):
            svc.store_image_derived_chunks("doc1")

        assert update_calls == [], "stored_in must not be updated when rowcount=0"

    def test_validate_failure_leaves_stored_in_unchanged(self):
        """validate() raising ValueError must leave stored_in = 'image_store'."""
        rows = [_image_row(detected_store="table_store")]

        handler = FakeHandler(
            canonical="table text",
            insert_rowcount=1,
            validate_raises=ValueError("validate failed: embedding NULL"),
        )
        update_calls = []

        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages", fake_embed), \
             patch("app.services.storage_service._update_image_stored_in_conn",
                   side_effect=lambda c, i, s: update_calls.append(s)):
            svc.store_image_derived_chunks("doc1")

        assert update_calls == [], "stored_in must not be updated when validate() raises"

    def test_embed_passages_called_with_canonical_text(self):
        """embed_passages must receive the handler's canonical_text output."""
        rows = [_image_row(detected_store="vector_store", structured_content='{"text":"hello"}')]

        handler = FakeHandler(canonical="the canonical chunk text")

        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages", fake_embed):
            svc.store_image_derived_chunks("doc1")

        fake_embed.assert_called_once_with(["the canonical chunk text"])

    def test_empty_canonical_with_fallback_skips_image(self):
        """When canonical_text is empty AND structured_content+ocr_text are empty, skip."""
        rows = [
            _image_row(
                detected_store="vector_store",
                ocr_text="",
                structured_content="",
            )
        ]

        handler = FakeHandler(canonical="")  # returns empty canonical
        update_calls = []
        embed_calls = []

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages",
                   side_effect=lambda t: embed_calls.append(t)), \
             patch("app.services.storage_service._update_image_stored_in_conn",
                   side_effect=lambda c, i, s: update_calls.append(s)):
            svc.store_image_derived_chunks("doc1")

        assert embed_calls == [], "embed_passages must not be called when canonical is empty"
        assert update_calls == [], "stored_in must not be updated when canonical is empty"

    def test_fallback_to_structured_content_when_canonical_empty(self):
        """When canonical_text is empty but structured_content is not, use it as fallback."""
        rows = [
            _image_row(
                detected_store="vector_store",
                ocr_text="",
                structured_content="fallback text from structured",
            )
        ]

        handler = FakeHandler(canonical="")  # handler returns empty; fallback kicks in
        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages", fake_embed):
            svc.store_image_derived_chunks("doc1")

        # embed_passages must be called with the structured_content fallback
        fake_embed.assert_called_once_with(["fallback text from structured"])

    def test_stored_in_updated_on_success(self):
        """On a clean success path stored_in is flipped to detected_store."""
        rows = [_image_row(img_id="img-uuid", detected_store="clause_store")]

        handler = FakeHandler(canonical="clause content")
        update_calls = []

        arr = MagicMock()
        arr.tolist.return_value = _fake_embedding()
        fake_embed = MagicMock(return_value=[arr])

        def fake_update(conn, image_id, stored_in):
            update_calls.append((image_id, stored_in))

        with patch("app.services.storage_service.get_db", _simple_get_db(rows)), \
             patch("app.services.store_router.get_handler", return_value=handler), \
             patch("app.services.embedding_service.embed_passages", fake_embed), \
             patch("app.services.storage_service._update_image_stored_in_conn",
                   side_effect=fake_update):
            svc.store_image_derived_chunks("doc1")

        assert len(update_calls) == 1
        assert update_calls[0] == ("img-uuid", "clause_store")


# ---------------------------------------------------------------------------
# Part 3 — idempotency pre-delete
# ---------------------------------------------------------------------------

class TestIdempotencyPreDelete:
    """Before the per-image loop, prior image-derived rows (index >= 50 000)
    must be deleted from all four destination stores."""

    def test_pre_delete_runs_for_all_four_stores(self):
        """DELETE statements for vector_store, document_store, clause_store, table_store
        must all be issued before the per-image loop begins.

        Call order inside store_image_derived_chunks():
          1 = SELECT (returns rows — must be non-empty so the function doesn't return early)
          2 = idempotency pre-delete connection (we capture execute calls here)
          3+ = per-image write connections
        """
        # Need at least one row so the function doesn't short-circuit after the SELECT.
        # get_handler returns None so no per-image work is done, keeping the test minimal.
        rows = [_image_row(detected_store="unknown_store")]

        delete_calls = []
        state = {"n": 0}

        @contextmanager
        def fake_get_db():
            state["n"] += 1
            n = state["n"]
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            if n == 1:
                # SELECT call — return the fake rows
                cur.fetchall.return_value = rows
            elif n == 2:
                # Pre-delete call — capture the SQL
                def capture_execute(sql, params):
                    delete_calls.append(sql)
                cur.execute.side_effect = capture_execute
            conn = MagicMock()
            conn.cursor.return_value = cur
            yield conn

        with patch("app.services.storage_service.get_db", fake_get_db), \
             patch("app.services.store_router.get_handler", return_value=None):
            svc.store_image_derived_chunks("doc1")

        # All three active DELETE statements must be present
        joined = "\n".join(delete_calls)
        for store in ("vector_store", "clause_store", "table_store"):
            assert store in joined, f"DELETE for {store} not found in pre-delete SQL"


# ---------------------------------------------------------------------------
# Part 4 — _ensure_table_crop_ocr_in_table_store (unchanged helper)
# ---------------------------------------------------------------------------

# NOTE: TestEnsureTableCropOcrFromImageStore was removed in Slice 2a. The
# _ensure_table_crop_ocr_in_table_store backfill function was deleted — table_store's
# source_image_id is now set at insert time by _store_tables (write-time lineage), so
# the post-hoc crop-OCR backfill path no longer exists.
