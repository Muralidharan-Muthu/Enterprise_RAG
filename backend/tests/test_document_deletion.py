"""
Unit tests for document deletion and storage cascade.
"""
from unittest.mock import MagicMock, patch, call
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.supabase_storage import delete_all_document_storage, delete_folder


def test_delete_folder_lists_and_removes_files():
    mock_client = MagicMock()
    mock_bucket = MagicMock()
    mock_client.storage.from_.return_value = mock_bucket
    mock_bucket.list.return_value = [
        {"name": "0.png"},
        {"name": "1.png"},
        {"name": ".emptyFolderPlaceholder"},
    ]

    with patch("app.services.supabase_storage._client", return_value=mock_client), \
         patch("app.services.supabase_storage.delete_files") as mock_delete_files:
        deleted = delete_folder("rag-documents", "images/doc-123")
        assert deleted == 2
        mock_delete_files.assert_called_once_with("rag-documents", ["images/doc-123/0.png", "images/doc-123/1.png"])


def test_delete_all_document_storage_cleans_all_folders():
    with patch("app.services.supabase_storage.delete_files") as mock_del_files, \
         patch("app.services.supabase_storage.delete_folder") as mock_del_folder:
        delete_all_document_storage("doc-123", storage_path="documents/test.pdf", bucket="rag-documents")

        mock_del_files.assert_called_once_with("rag-documents", ["documents/test.pdf"])
        assert mock_del_folder.call_count == 3
        mock_del_folder.assert_has_calls([
            call("rag-documents", "images/doc-123"),
            call("rag-documents", "tables/doc-123"),
            call("rag-documents", "staging/doc-123"),
        ])
