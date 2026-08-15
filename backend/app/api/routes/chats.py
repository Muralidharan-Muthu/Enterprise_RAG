import json
import logging
from typing import List, Optional

import httpx
import psycopg2
import psycopg2.errors
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.db.connection import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class CreateSessionRequest(BaseModel):
    title: Optional[str] = "New Chat"


class AddMessageRequest(BaseModel):
    role: str
    content: str
    confidence: Optional[float] = None
    processing_time: Optional[float] = None
    stores_searched: Optional[List[str]] = None
    notes: Optional[str] = None
    citations: Optional[list] = None
    is_pinned: Optional[bool] = False


class UpdateSessionRequest(BaseModel):
    title: str


def _is_table_missing(exc: Exception) -> bool:
    """True when psycopg2 raises UndefinedTable (42P01) — migration not applied yet."""
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    return isinstance(cause, psycopg2.errors.UndefinedTable)


@router.post("")
def create_session(req: CreateSessionRequest):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO chat_sessions (title)
                       VALUES (%s)
                       RETURNING id, title, message_count, created_at, updated_at""",
                    (req.title or "New Chat",),
                )
                row = cur.fetchone()
        return {
            "id": str(row[0]),
            "title": row[1],
            "message_count": row[2],
            "created_at": row[3].isoformat(),
            "updated_at": row[4].isoformat(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            logger.warning("chat_sessions table missing — run migration 002_chat_sessions.sql in schema %s", settings.SUPABASE_SCHEMA)
            raise HTTPException(status_code=503, detail="Chat history unavailable — DB migration pending")
        raise


@router.get("")
def list_sessions(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, title, message_count, created_at, updated_at
                       FROM chat_sessions
                       ORDER BY updated_at DESC
                       LIMIT %s OFFSET %s""",
                    (limit, offset),
                )
                rows = cur.fetchall()
        return [
            {
                "id": str(r[0]),
                "title": r[1],
                "message_count": r[2],
                "created_at": r[3].isoformat(),
                "updated_at": r[4].isoformat(),
            }
            for r in rows
        ]
    except Exception as exc:
        if _is_table_missing(exc):
            logger.warning("chat_sessions table missing — run migration 002_chat_sessions.sql in schema %s", settings.SUPABASE_SCHEMA)
            return []
        raise


@router.get("/{session_id}")
def get_session(session_id: str):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, title, message_count, created_at, updated_at
                       FROM chat_sessions WHERE id = %s""",
                    (session_id,),
                )
                sess = cur.fetchone()
                if not sess:
                    raise HTTPException(status_code=404, detail="Session not found")

                cur.execute(
                    """SELECT id, role, content, confidence, processing_time,
                              stores_searched, notes, citations, created_at, is_pinned
                       FROM chat_messages
                       WHERE session_id = %s
                       ORDER BY created_at ASC""",
                    (session_id,),
                )
                msgs = cur.fetchall()

        return {
            "id": str(sess[0]),
            "title": sess[1],
            "message_count": sess[2],
            "created_at": sess[3].isoformat(),
            "updated_at": sess[4].isoformat(),
            "messages": [
                {
                    "id": str(m[0]),
                    "role": m[1],
                    "content": m[2],
                    "confidence": m[3],
                    "processing_time": m[4],
                    "stores_searched": m[5],
                    "notes": m[6],
                    "citations": json.loads(m[7]) if isinstance(m[7], str) else m[7],
                    "created_at": m[8].isoformat(),
                    "is_pinned": bool(m[9]),
                }
                for m in msgs
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            raise HTTPException(status_code=503, detail="Chat history unavailable — DB migration pending")
        raise


@router.post("/{session_id}/messages")
def add_message(session_id: str, req: AddMessageRequest):
    if req.role not in ("user", "assistant"):
        raise HTTPException(status_code=422, detail="role must be 'user' or 'assistant'")

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM chat_sessions WHERE id = %s", (session_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Session not found")

                cur.execute(
                    """INSERT INTO chat_messages
                           (session_id, role, content, confidence, processing_time,
                            stores_searched, notes, citations, is_pinned)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id, created_at""",
                    (
                        session_id,
                        req.role,
                        req.content,
                        req.confidence,
                        req.processing_time,
                        req.stores_searched,
                        req.notes,
                        json.dumps(req.citations) if req.citations is not None else None,
                        req.is_pinned,
                    ),
                )
                row = cur.fetchone()

        return {"id": str(row[0]), "created_at": row[1].isoformat()}
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            raise HTTPException(status_code=503, detail="Chat history unavailable — DB migration pending")
        raise


class PinMessageRequest(BaseModel):
    is_pinned: bool


@router.patch("/{session_id}/messages/{message_id}/pin")
def pin_message(session_id: str, message_id: str, req: PinMessageRequest):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE chat_messages SET is_pinned = %s WHERE id = %s AND session_id = %s",
                    (req.is_pinned, message_id, session_id)
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Message not found in session")
        return {"status": "ok", "is_pinned": req.is_pinned}
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            raise HTTPException(status_code=503, detail="Chat history unavailable")
        raise


@router.patch("/{session_id}")
def update_session(session_id: str, req: UpdateSessionRequest):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE chat_sessions
                       SET title = %s, updated_at = NOW()
                       WHERE id = %s
                       RETURNING id, title""",
                    (req.title, session_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Session not found")
        return {"id": str(row[0]), "title": row[1]}
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            raise HTTPException(status_code=503, detail="Chat history unavailable — DB migration pending")
        raise


@router.post("/{session_id}/generate-title")
def generate_title(session_id: str):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM chat_sessions WHERE id = %s", (session_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Session not found")
                cur.execute(
                    "SELECT content FROM chat_messages WHERE session_id = %s AND role = 'user' ORDER BY created_at ASC LIMIT 1",
                    (session_id,),
                )
                row = cur.fetchone()

        if not row:
            return {"title": "New Chat"}

        first_message = row[0][:300]
        title = _call_gemma_for_title(first_message)

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE chat_sessions SET title = %s, updated_at = NOW() WHERE id = %s",
                    (title, session_id),
                )

        return {"title": title}
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            raise HTTPException(status_code=503, detail="Chat history unavailable — DB migration pending")
        raise


def _call_gemma_for_title(first_message: str) -> str:
    base = settings.GEMMA4_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if getattr(settings, "GEMMA4_API_KEY", None):
        headers["Authorization"] = f"Bearer {settings.GEMMA4_API_KEY}"

    payload = {
        "model": settings.GEMMA4_MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": (
                    f'Generate a short, concise title (4-7 words) for a conversation '
                    f'that starts with: "{first_message}". '
                    f'Return only the title — no quotes, no explanation, no trailing punctuation.'
                ),
            }
        ],
        "max_tokens": 20,
        "temperature": 0.7,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(f"{base}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            title = resp.json()["choices"][0]["message"]["content"].strip().strip('"\'')
            return title[:80] if title else first_message[:60]
    except Exception as exc:
        logger.warning("Title generation failed: %s", exc)
        return first_message[:60]


@router.delete("/{session_id}")
def delete_session(session_id: str):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chat_sessions WHERE id = %s RETURNING id",
                    (session_id,),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Session not found")
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception as exc:
        if _is_table_missing(exc):
            raise HTTPException(status_code=503, detail="Chat history unavailable — DB migration pending")
        raise
