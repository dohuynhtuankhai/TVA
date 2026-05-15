"""Simple session-based authentication with in-memory session cache."""

import hashlib
import hmac
import secrets
import time
from typing import Optional

from config import settings

# ── In-memory session store ──────────────────────────────────────────────────
# Maps session_id → {"created_at": timestamp}
_sessions: dict[str, dict] = {}


def _hash_password(password: str) -> str:
    """SHA-256 hash for comparison (not stored, just for runtime check)."""
    return hashlib.sha256(password.encode()).hexdigest()


# Pre-compute the expected hash at startup
_expected_hash = _hash_password(settings.AUTH_PASSWORD)


def verify_credentials(username: str, password: str) -> bool:
    """Check username and password against config values."""
    username_ok = hmac.compare_digest(username, settings.AUTH_USERNAME)
    password_ok = hmac.compare_digest(_hash_password(password), _expected_hash)
    return username_ok and password_ok


def create_session() -> str:
    """Create a new session and return the session ID."""
    session_id = secrets.token_urlsafe(48)
    _sessions[session_id] = {"created_at": time.time()}
    return session_id


def validate_session(session_id: Optional[str]) -> bool:
    """Check if a session ID is valid and not expired."""
    if not session_id or session_id not in _sessions:
        return False
    session = _sessions[session_id]
    elapsed = time.time() - session["created_at"]
    if elapsed > settings.SESSION_MAX_AGE:
        # Expired — clean up
        del _sessions[session_id]
        return False
    return True


def destroy_session(session_id: str):
    """Remove a session (logout)."""
    _sessions.pop(session_id, None)


def cleanup_expired():
    """Remove all expired sessions from cache."""
    now = time.time()
    expired = [
        sid for sid, data in _sessions.items()
        if now - data["created_at"] > settings.SESSION_MAX_AGE
    ]
    for sid in expired:
        del _sessions[sid]
