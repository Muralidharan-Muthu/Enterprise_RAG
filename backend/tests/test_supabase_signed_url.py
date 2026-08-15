from unittest.mock import MagicMock, patch
import app.services.supabase_storage as sb


def _patched_client(signed_response):
    client = MagicMock()
    bucket = MagicMock()
    bucket.create_signed_url.return_value = signed_response
    client.storage.from_.return_value = bucket
    return client


def test_create_signed_url_handles_camel_key():
    client = _patched_client({"signedURL": "https://x/y?token=abc"})
    with patch.object(sb, "_client", return_value=client):
        url = sb.create_signed_url("rag-documents", "images/d/0.png", expires_in=600)
    assert url == "https://x/y?token=abc"
    client.storage.from_.assert_called_with("rag-documents")


def test_create_signed_url_handles_snake_key():
    client = _patched_client({"signed_url": "https://x/snake"})
    with patch.object(sb, "_client", return_value=client):
        url = sb.create_signed_url("rag-documents", "images/d/1.png")
    assert url == "https://x/snake"
