"""Application configuration using pydantic-settings."""

import os
import secrets
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "AlgoTrade Pro Engine"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./algotrade.db"

    # Redis (optional – falls back to in-memory if unavailable)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Encryption key for API secrets at rest (generate once, persist)
    ENCRYPTION_KEY: str = secrets.token_urlsafe(32)

    # ── Binance Testnet ───────────────────────────────────────────────
    TESTNET_MODE: bool = True  # True = fake money, False = real money
    BINANCE_TESTNET_API_URL: str = "https://testnet.binancefuture.com"
    BINANCE_TESTNET_WS_URL: str = "wss://stream.binancefuture.com"
    BINANCE_LIVE_API_URL: str = "https://fapi.binance.com"
    BINANCE_LIVE_WS_URL: str = "wss://fstream.binance.com"

    @property
    def binance_api_url(self) -> str:
        return self.BINANCE_TESTNET_API_URL if self.TESTNET_MODE else self.BINANCE_LIVE_API_URL

    @property
    def binance_ws_url(self) -> str:
        return self.BINANCE_TESTNET_WS_URL if self.TESTNET_MODE else self.BINANCE_LIVE_WS_URL

    # ── Authentication ──────────────────────────────────────────────
    AUTH_USERNAME: str = "dohuynhtuankhai"
    AUTH_PASSWORD: str = "@Ab123456"
    SESSION_SECRET: str = secrets.token_urlsafe(32)
    SESSION_MAX_AGE: int = 14 * 24 * 3600  # 2 weeks in seconds

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Webhook secret (optional extra auth layer)
    WEBHOOK_SECRET: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
