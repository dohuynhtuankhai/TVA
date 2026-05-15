"""AlgoTrade Pro Engine – main application entry point."""

import logging
import sys

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.base import BaseHTTPMiddleware

from auth import validate_session
from config import settings
from database import init_db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-5s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("algotrade")


# ── Auth middleware ──────────────────────────────────────────────────────────

# Paths that don't require authentication
PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/webhook"}
PUBLIC_PREFIXES = ("/static/",)

COOKIE_NAME = "algotrade_session"


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths through
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Check session cookie
        session_id = request.cookies.get(COOKIE_NAME)
        if validate_session(session_id):
            return await call_next(request)

        # Not authenticated — redirect pages, return 401 for API
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        else:
            return RedirectResponse("/login", status_code=302)


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(AuthMiddleware)

# Static files & templates
app.mount("/static", StaticFiles(directory="static"), name="static")
jinja_env = Environment(loader=FileSystemLoader("templates"), autoescape=True)

# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    await init_db()
    logger.info("Database initialised")


# ── Register API routers ────────────────────────────────────────────────────

from routes.auth import router as auth_router          # noqa: E402
from routes.webhook import router as webhook_router    # noqa: E402
from routes.accounts import router as accounts_router  # noqa: E402
from routes.settings import router as settings_router  # noqa: E402
from routes.trades import router as trades_router      # noqa: E402
from routes.dashboard import router as dashboard_router  # noqa: E402
from routes.positions import router as positions_router  # noqa: E402

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(accounts_router)
app.include_router(settings_router)
app.include_router(trades_router)
app.include_router(dashboard_router)
app.include_router(positions_router)


# ── Page routes (serve Jinja2 templates) ─────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    # If already logged in, redirect to dashboard
    session_id = request.cookies.get(COOKIE_NAME)
    if validate_session(session_id):
        return RedirectResponse("/", status_code=302)
    template = jinja_env.get_template("login.html")
    return HTMLResponse(template.render())


@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    template = jinja_env.get_template("dashboard.html")
    return HTMLResponse(template.render(page="dashboard"))


@app.get("/accounts", response_class=HTMLResponse)
async def page_accounts(request: Request):
    template = jinja_env.get_template("accounts.html")
    return HTMLResponse(template.render(page="accounts"))


@app.get("/trades", response_class=HTMLResponse)
async def page_trades(request: Request):
    template = jinja_env.get_template("trades.html")
    return HTMLResponse(template.render(page="trades"))


@app.get("/positions", response_class=HTMLResponse)
async def page_positions(request: Request):
    template = jinja_env.get_template("positions.html")
    return HTMLResponse(template.render(page="positions"))


@app.get("/webhook-logs", response_class=HTMLResponse)
async def page_webhook_logs(request: Request):
    template = jinja_env.get_template("webhook_logs.html")
    return HTMLResponse(template.render(page="webhook_logs"))


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    template = jinja_env.get_template("settings.html")
    return HTMLResponse(template.render(page="settings"))


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/api/health")
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
    )
