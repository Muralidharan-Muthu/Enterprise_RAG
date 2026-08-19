"""
Authentication API routes — signup, login, and user info.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, EmailStr, Field

from app.services.auth_service import (
    authenticate_user,
    create_access_token,
    create_user,
    decode_token,
    get_user_by_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request / Response models ────────────────────────────────────────────────

class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user: dict


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    created_at: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=AuthResponse)
def signup(body: SignupRequest):
    """Create a new user account and return a JWT."""
    try:
        user = create_user(
            username=body.username,
            email=body.email,
            password=body.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error("Signup failed: %s", exc)
        raise HTTPException(status_code=500, detail="Account creation failed")

    token = create_access_token(user["id"], user["email"], user["username"])
    return {"token": token, "user": user}


@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest):
    """Authenticate with email + password and return a JWT."""
    user = authenticate_user(email=body.email, password=body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user["id"], user["email"], user["username"])
    return {"token": token, "user": user}


@router.get("/me", response_model=UserResponse)
def get_current_user(authorization: Optional[str] = Header(None)):
    """Return the current user from the JWT in the Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user
