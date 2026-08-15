"""Tests for _build_table_embeddings — the TABLE_CHILD_SEARCH_ENABLED gate.

Confirmed low-severity perf issue: row-window chunking (chunk_tables()) and
BGE embedding of table children ran UNCONDITIONALLY whenever a document had
tables, even though TABLE_CHILD_SEARCH_ENABLED only gated the later
table_chunk_store insert. This wasted an embed_passages() call (and the
chunk_tables() split) whenever the flag was off.

_build_table_embeddings() is the extracted, directly-testable unit that now
skips chunk_tables()/embed_passages(child_texts) entirely when the flag is
False, while still building the parent-summary embeddings tables always need.

No model / DB / network — embed_passages is monkeypatched.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from app.models.document import ExtractedTable
from app.services.ingestion_orchestrator import _build_table_embeddings


def _make_table(n_rows: int, table_index: int = 0) -> ExtractedTable:
    headers = ["ID", "Name"]
    rows = [[str(i), f"Person{i}"] for i in range(n_rows)]
    return ExtractedTable(
        table_index=table_index,
        page_number=1,
        headers=headers,
        rows=rows,
        caption="Employees",
        raw_text="",
        markdown_text="",
    )


def _fake_embed_passages(texts):
    return np.zeros((len(texts), 1024), dtype="float32")


class TestNoTables:
    def test_empty_tables_returns_all_empty_without_calling_embed(self):
        with patch(
            "app.services.embedding_service.embed_passages",
            side_effect=AssertionError("must not be called when there are no tables"),
        ):
            table_embeddings, children, child_embeddings = _build_table_embeddings([], "doc-1")

        assert len(table_embeddings) == 0
        assert children == []
        assert len(child_embeddings) == 0


class TestChildSearchDisabled:
    def test_skips_chunk_tables_and_child_embedding_call(self, monkeypatch):
        """TABLE_CHILD_SEARCH_ENABLED=False must skip chunk_tables() and the
        embed_passages(child_texts) call entirely — only the parent-summary
        embed_passages() call should happen."""
        from app.config import settings
        monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", False)

        tables = [_make_table(500, table_index=0)]

        embed_calls = []

        def spy_embed_passages(texts):
            embed_calls.append(list(texts))
            return _fake_embed_passages(texts)

        with patch(
            "app.services.embedding_service.embed_passages",
            side_effect=spy_embed_passages,
        ), patch(
            "app.services.table_chunker.chunk_tables",
            side_effect=AssertionError("chunk_tables must not run when flag is False"),
        ) as mock_chunk_tables:
            table_embeddings, children, child_embeddings = _build_table_embeddings(
                tables, "doc-1"
            )

        mock_chunk_tables.assert_not_called()
        # Exactly one embed_passages call (the parent summaries) — no second
        # call for table children.
        assert len(embed_calls) == 1
        assert len(table_embeddings) == 1  # one parent summary vector
        assert children == []
        assert len(child_embeddings) == 0

    def test_parent_summary_still_reflects_table_content(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", False)

        tables = [_make_table(3, table_index=0)]
        captured = {}

        def spy_embed_passages(texts):
            captured["texts"] = list(texts)
            return _fake_embed_passages(texts)

        with patch(
            "app.services.embedding_service.embed_passages",
            side_effect=spy_embed_passages,
        ):
            _build_table_embeddings(tables, "doc-1")

        assert len(captured["texts"]) == 1
        assert "Employees" in captured["texts"][0]


class TestChildSearchEnabled:
    def test_still_builds_children_and_embeds_them(self, monkeypatch):
        """TABLE_CHILD_SEARCH_ENABLED=True (default) must behave exactly as
        before: chunk_tables() runs and children get their own embed_passages()
        call in addition to the parent-summary call."""
        from app.config import settings
        monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

        tables = [_make_table(500, table_index=0)]

        embed_calls = []

        def spy_embed_passages(texts):
            embed_calls.append(list(texts))
            return _fake_embed_passages(texts)

        with patch(
            "app.services.embedding_service.embed_passages",
            side_effect=spy_embed_passages,
        ):
            table_embeddings, children, child_embeddings = _build_table_embeddings(
                tables, "doc-1"
            )

        # Two embed_passages calls: parents, then children.
        assert len(embed_calls) == 2
        assert len(table_embeddings) == 1
        assert len(children) > 0
        assert len(child_embeddings) == len(children)

    def test_zero_row_table_has_no_children_but_has_summary(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "TABLE_CHILD_SEARCH_ENABLED", True)

        tables = [_make_table(0, table_index=0)]

        with patch(
            "app.services.embedding_service.embed_passages",
            side_effect=_fake_embed_passages,
        ):
            table_embeddings, children, child_embeddings = _build_table_embeddings(
                tables, "doc-1"
            )

        assert len(table_embeddings) == 1
        assert children == []
        assert len(child_embeddings) == 0
