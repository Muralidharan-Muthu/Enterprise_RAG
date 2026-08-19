"""
Authentication service — password hashing, JWT creation & validation.

Uses bcrypt for password hashing and python-jose for JWT tokens.
Tokens carry user_id + email, signed with SUPABASE_SERVICE_KEY (HS256),
valid for 24 hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from app.config import settings

logger = logging.getLogger(__name__)

# JWT configuration — reuse SUPABASE_SERVICE_KEY as signing secret
_JWT_SECRET = settings.SUPABASE_SERVICE_KEY
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = 24


# ── Password hashing ────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


# ── JWT tokens ───────────────────────────────────────────────────────────────

def create_access_token(user_id: str, email: str, username: str) -> str:
    """Create a signed JWT carrying user identity."""
    expire = datetime.now(timezone.utc) + timedelta(hours=_JWT_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "username": username,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT. Returns the payload dict or None on failure."""
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None


# ── DB operations ────────────────────────────────────────────────────────────

def create_user(username: str, email: str, password: str) -> dict:
    """Insert a new user into the database. Returns the user dict.
    Raises ValueError on duplicate email/username."""
    from app.db.connection import get_db

    pw_hash = hash_password(password)

    with get_db() as conn:
        with conn.cursor() as cur:
            # Check if email or username already exists
            cur.execute(
                "SELECT id FROM multi_store_rag_working.users WHERE email = %s OR username = %s",
                (email.lower(), username.lower()),
            )
            if cur.fetchone():
                raise ValueError("A user with this email or username already exists")

            cur.execute(
                """
                INSERT INTO multi_store_rag_working.users (username, email, password_hash)
                VALUES (%s, %s, %s)
                RETURNING id, username, email, created_at
                """,
                (username.lower(), email.lower(), pw_hash),
            )
            row = cur.fetchone()
            conn.commit()

    return {
        "id": str(row[0]),
        "username": row[1],
        "email": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
    }


def authenticate_user(email: str, password: str) -> Optional[dict]:
    """Verify email + password. Returns user dict or None if invalid."""
    from app.db.connection import get_db

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, password_hash, created_at
                FROM multi_store_rag_working.users
                WHERE email = %s
                """,
                (email.lower(),),
            )
            row = cur.fetchone()

    if not row:
        return None

    if not verify_password(password, row[3]):
        return None

    return {
        "id": str(row[0]),
        "username": row[1],
        "email": row[2],
        "created_at": row[4].isoformat() if row[4] else None,
    }


def get_user_by_id(user_id: str) -> Optional[dict]:
    """Fetch a user by UUID. Returns user dict or None."""
    from app.db.connection import get_db

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, email, created_at
                FROM multi_store_rag_working.users
                WHERE id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()

    if not row:
        return None

    return {
        "id": str(row[0]),
        "username": row[1],
        "email": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
    }
