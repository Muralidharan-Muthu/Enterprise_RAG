"""
Groq (OpenAI-compatible) chat client.
Re-exports chat, chat_async, chat_async_stream from the shared LLM client.
"""
from app.services.gemma_client import (
    chat,
    chat_async,
    chat_async_stream,
    _get_client,
    _get_async_client,
)

__all__ = ["chat", "chat_async", "chat_async_stream"]
