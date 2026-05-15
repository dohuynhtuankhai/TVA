"""Authentication routes – login, logout, session check."""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from auth import create_session, destroy_session, verify_credentials
from config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("algotrade.auth")

COOKIE_NAME = "algotrade_session"


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    if not verify_credentials(body.username, body.password):
        logger.warning("Failed login attempt for user: %s", body.username)
        raise HTTPException(401, "Invalid username or password")

    session_id = create_session()
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        max_age=settings.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True if using HTTPS
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
