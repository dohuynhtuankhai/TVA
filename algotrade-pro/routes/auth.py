"""Authentication routes – login, logout, session check."""

import logging
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from auth import create_session, destroy_session, verify_credentials
from config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("algopro.auth")

COOKIE_NAME = "algopro_session"

# ── Rate limiting ───────────────────────────────────────────────────────────
# Track failed login attempts per IP
_login_attempts: dict[str, dict] = {}  # ip → {"count": int, "first_at": float}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300  # 5 minutes


def _check_rate_limit(ip: str):
    """Raise 429 if too many failed attempts from this IP."""
    record = _login_attempts.get(ip)
    if not record:
        return
    # Reset if lockout window passed
    if time.time() - record["first_at"] > LOCKOUT_SECONDS:
        del _login_attempts[ip]
        return
    if record["count"] >= MAX_ATTEMPTS:
        remaining = int(LOCKOUT_SECONDS - (time.time() - record["first_at"]))
        raise HTTPException(429, f"Too many login attempts. Try again in {remaining}s")


def _record_failed_attempt(ip: str):
    """Record a failed login attempt."""
    record = _login_attempts.get(ip)
    now = time.time()
    if not record or now - record["first_at"] > LOCKOUT_SECONDS:
        _login_attempts[ip] = {"count": 1, "first_at": now}
    else:
        record["count"] += 1


def _clear_attempts(ip: str):
    """Clear attempts on successful login."""
    _login_attempts.pop(ip, None)


def _prune_stale_attempts():
    """Remove expired entries to prevent unbounded memory growth."""
    now = time.time()
    stale = [ip for ip, r in _login_attempts.items() if now - r["first_at"] > LOCKOUT_SECONDS]
    for ip in stale:
        del _login_attempts[ip]


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    _prune_stale_attempts()  # Prevent unbounded memory growth
    _check_rate_limit(client_ip)

    if not verify_credentials(body.username, body.password):
        _record_failed_attempt(client_ip)
        logger.warning("Failed login attempt for user: %s from %s", body.username, client_ip)
        raise HTTPException(401, "Invalid username or password")

    _clear_attempts(client_ip)

    session_id = create_session()
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        max_age=settings.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=not settings.DEBUG,  # HTTPS-only in production
    )
    logger.info("User '%s' logged in", body.username)
    return {"status": "ok"}


@router.post("/logout")
async def logout(request: Request, response: Response):
    session_id = request.cookies.get(COOKIE_NAME)
    if session_id:
        destroy_session(session_id)
    response.delete_cookie(COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/check")
async def check_session(request: Request):
    """Check if current session is valid (used by frontend)."""
    from auth import validate_session

    session_id = request.cookies.get(COOKIE_NAME)
    if validate_session(session_id):
        return {"authenticated": True}
    raise HTTPException(401, "Not authenticated")
