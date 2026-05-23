"""Application configuration using pydantic-settings."""

import os
import secrets
import sys
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "AlgoTrade Pro Engine"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./algotrade.db"

    # Redis (optional – falls back to in-memory if unavailable)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Encryption key for API secrets at rest — MUST be set in .env
    # Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    ENCRYPTION_KEY: str = ""

    # ── Binance Spot + Futures / Testnet ──────────────────────────────
    TESTNET_MODE: bool = True  # True = fake money, False = real money
    BINANCE_SPOT_TESTNET_API_URL: str = "https://testnet.binance.vision"
    BINANCE_SPOT_TESTNET_WS_URL: str = "wss://testnet.binance.vision"
    BINANCE_SPOT_LIVE_API_URL: str = "https://api.binance.com"
    BINANCE_SPOT_LIVE_WS_URL: str = "wss://stream.binance.com:9443"
    BINANCE_FUTURES_TESTNET_API_URL: str = "https://testnet.binancefuture.com"
    BINANCE_FUTURES_TESTNET_WS_URL: str = "wss://stream.binancefuture.com"
    BINANCE_FUTURES_LIVE_API_URL: str = "https://fapi.binance.com"
    BINANCE_FUTURES_LIVE_WS_URL: str = "wss://fstream.binance.com"

    @property
    def binance_api_url(self) -> str:
        return self.BINANCE_FUTURES_TESTNET_API_URL if self.TESTNET_MODE else self.BINANCE_FUTURES_LIVE_API_URL

    @property
    def binance_ws_url(self) -> str:
        return self.BINANCE_FUTURES_TESTNET_WS_URL if self.TESTNET_MODE else self.BINANCE_FUTURES_LIVE_WS_URL

    # ── Authentication ──────────────────────────────────────────────
    AUTH_USERNAME: str = ""
    AUTH_PASSWORD: str = ""
    SESSION_SECRET: str = ""  # Loaded from .env; generates random if missing
    SESSION_MAX_AGE: int = 14 * 24 * 3600  # 2 weeks in seconds

    # Server
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Webhook secret (optional extra auth layer)
    WEBHOOK_SECRET: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def validate_required(self):
        """Fail fast if critical settings are missing."""
        errors = []
        if not self.ENCRYPTION_KEY or self.ENCRYPTION_KEY == "your-generated-key-here":
            errors.append("ENCRYPTION_KEY must be set in .env (generate with: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\")")
        if not self.AUTH_USERNAME:
            errors.append("AUTH_USERNAME must be set in .env")
        if not self.AUTH_PASSWORD:
            errors.append("AUTH_PASSWORD must be set in .env")
        if errors:
            for e in errors:
                print(f"  FATAL: {e}", file=sys.stderr)
            sys.exit(1)

        # Non-fatal: generate random SESSION_SECRET if not set (sessions won't survive restart)
        if not self.SESSION_SECRET:
            self.SESSION_SECRET = secrets.token_urlsafe(32)
            print("  WARN: SESSION_SECRET not set in .env — generated random (sessions won't survive restart)", file=sys.stderr)


settings = Settings()
settings.validate_required()
