"""AlgoTrade Pro Engine – main application entry point."""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette.types import ASGIApp, Receive, Scope, Send

from auth import validate_session
from config import settings
from database import init_db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-5s  %(message)s",
    stream=sys.stdout,
)


class _WebSocketNoiseFilter(logging.Filter):
    """Suppress repetitive WebSocket lifecycle logs from the dashboard stream."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            "WebSocket /ws" in message
            or message in {"connection open", "connection closed"}
        )


_ws_noise_filter = _WebSocketNoiseFilter()
for _logger_name in (
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.websockets_impl",
    "websockets.server",
):
    logging.getLogger(_logger_name).addFilter(_ws_noise_filter)

# Keep the console focused on app logs and errors instead of every HTTP request.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger("algotrade")


# ── Auth middleware (raw ASGI — supports WebSocket) ─────────────────────────

# Paths that don't require authentication
PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/webhook", "/ws"}
PUBLIC_PREFIXES = ("/static/",)

COOKIE_NAME = "algotrade_session"


class AuthMiddleware:
    """Raw ASGI middleware so WebSocket connections pass through cleanly."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # Only check HTTP requests — let WebSocket through (it does its own auth)
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Allow public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Parse session cookie from headers
        session_id = None
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"cookie":
                for part in header_value.decode().split(";"):
                    part = part.strip()
                    if part.startswith(f"{COOKIE_NAME}="):
                        session_id = part[len(COOKIE_NAME) + 1:]
                        break
                break

        if validate_session(session_id):
            await self.app(scope, receive, send)
            return

        # Not authenticated
        if path.startswith("/api/"):
            from starlette.responses import JSONResponse
            response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
        else:
            response = RedirectResponse("/login", status_code=302)

        await response(scope, receive, send)


# ── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """Startup / shutdown lifecycle hook."""
    import asyncio
    from utility_watcher import run_utility_watcher_loop

    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    await init_db()
    logger.info("Database initialised")
    watcher_task = asyncio.create_task(run_utility_watcher_loop())
    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.APP_NAME)
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass


# ── App ──────────────────────────────────────────────────────────────────────
_app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan,
)

# Wrap with raw ASGI auth middleware (supports WebSocket)
app = AuthMiddleware(_app)

# Static files & templates
_app.mount("/static", StaticFiles(directory="static"), name="static")
jinja_env = Environment(loader=FileSystemLoader("templates"), autoescape=True)


# ── Register API routers ────────────────────────────────────────────────────

from routes.auth import router as auth_router          # noqa: E402
from routes.webhook import router as webhook_router    # noqa: E402
from routes.accounts import router as accounts_router  # noqa: E402
from routes.settings import router as settings_router  # noqa: E402
from routes.trades import router as trades_router      # noqa: E402
from routes.dashboard import router as dashboard_router  # noqa: E402
from routes.positions import router as positions_router  # noqa: E402
from routes.utility import router as utility_router    # noqa: E402

_app.include_router(auth_router)
_app.include_router(webhook_router)
_app.include_router(accounts_router)
_app.include_router(settings_router)
_app.include_router(trades_router)
_app.include_router(dashboard_router)
_app.include_router(positions_router)
_app.include_router(utility_router)


# ── Page routes (serve Jinja2 templates) ─────────────────────────────────────

@_app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    # If already logged in, redirect to dashboard
    session_id = request.cookies.get(COOKIE_NAME)
    if validate_session(session_id):
        return RedirectResponse("/", status_code=302)
    template = jinja_env.get_template("login.html")
    return HTMLResponse(template.render())


@_app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    template = jinja_env.get_template("dashboard.html")
    return HTMLResponse(template.render(page="dashboard"))


@_app.get("/accounts", response_class=HTMLResponse)
async def page_accounts(request: Request):
    template = jinja_env.get_template("accounts.html")
    return HTMLResponse(template.render(page="accounts"))


@_app.get("/trades", response_class=HTMLResponse)
async def page_trades(request: Request):
    template = jinja_env.get_template("trades.html")
    return HTMLResponse(template.render(page="trades"))


@_app.get("/positions", response_class=HTMLResponse)
async def page_positions(request: Request):
    template = jinja_env.get_template("positions.html")
    return HTMLResponse(template.render(page="positions"))


@_app.get("/webhook-logs", response_class=HTMLResponse)
async def page_webhook_logs(request: Request):
    template = jinja_env.get_template("webhook_logs.html")
    return HTMLResponse(template.render(page="webhook_logs"))


@_app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    template = jinja_env.get_template("settings.html")
    return HTMLResponse(template.render(page="settings"))


@_app.get("/utility", response_class=HTMLResponse)
async def page_utility(request: Request):
    template = jinja_env.get_template("utility.html")
    return HTMLResponse(template.render(page="utility"))


# ── Health check ─────────────────────────────────────────────────────────────

@_app.get("/api/health")
async def health():
    from bot_engine import get_testnet_mode
    testnet = await get_testnet_mode()
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "testnet": testnet,
    }


# ── Run with uvicorn ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        access_log=False,
    )
