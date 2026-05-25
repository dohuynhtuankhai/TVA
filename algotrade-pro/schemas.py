"""Pydantic schemas for request/response validation."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Webhook ──────────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    symbol: str = Field(..., example="BTCUSDT")
    action: str = Field(..., pattern="^(ENTRY|EXIT|LONG|SHORT|BUY|SELL)$")
    timeframe: str = Field(..., example="5m")
    price: Optional[float] = None


# ── Exchange Account ─────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    api_key: str = Field(..., min_length=10)
    api_secret: str = Field(..., min_length=10)
    trading_size_type: Optional[str] = Field(None, pattern="^(percent|fixed)$")
    trading_size_value: Optional[float] = Field(None, gt=0)
    leverage: Optional[int] = Field(None, ge=1, le=125)
    stoploss_percent: Optional[float] = Field(None, ge=0, le=100)
    trail_activation_pct: Optional[float] = Field(None, ge=0, le=50)
    trail_callback_pct: Optional[float] = Field(None, ge=0.1, le=10)
    trade_mode: Optional[str] = Field(None, pattern="^(single|multi)$")


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    is_active: Optional[bool] = None
    trading_size_type: Optional[str] = Field(None, pattern="^(percent|fixed)$")
    trading_size_value: Optional[float] = Field(None, gt=0)
    leverage: Optional[int] = Field(None, ge=1, le=125)
    stoploss_percent: Optional[float] = Field(None, ge=0, le=100)
    trail_activation_pct: Optional[float] = Field(None, ge=0, le=50)
    trail_callback_pct: Optional[float] = Field(None, ge=0.1, le=10)
    trade_mode: Optional[str] = Field(None, pattern="^(single|multi)$")


class AccountResponse(BaseModel):
    id: int
    name: str
    api_key_preview: str
    is_active: bool
    futures_enabled: bool
    spot_enabled: bool
    trading_size_type: str
    trading_size_value: float
    leverage: int
    stoploss_percent: Optional[float]
    trail_activation_pct: Optional[float]
    trail_callback_pct: Optional[float]
    trade_mode: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Symbol Mapping ───────────────────────────────────────────────────────────

class SymbolMappingCreate(BaseModel):
    symbol: str = Field(..., min_length=3, max_length=20, pattern=r"^[A-Za-z0-9:.]+$")
    timeframe: str = Field(..., min_length=1, max_length=10)
    account_id: int = Field(..., ge=1)
    market_type: str = Field(..., pattern="^(futures|spot)$")


class SymbolMappingResponse(BaseModel):
    id: int
    symbol: str
    timeframe: str
    account_id: int
    market_type: str = "futures"

    class Config:
        from_attributes = True


# ── Bot Settings ─────────────────────────────────────────────────────────────

class BotSettingsUpdate(BaseModel):
    bot_active: Optional[bool] = None
    testnet_mode: Optional[bool] = None
    default_trading_size_type: Optional[str] = Field(None, pattern="^(percent|fixed)$")
    risk_per_trade: Optional[float] = Field(None, gt=0)
    leverage_override: Optional[int] = Field(None, ge=1, le=125)
    default_stoploss_percent: Optional[float] = Field(None, ge=0, le=100)
    default_trail_activation_pct: Optional[float] = Field(None, ge=0, le=50)
    default_trail_callback_pct: Optional[float] = Field(None, ge=0.1, le=10)
    default_trade_mode: Optional[str] = Field(None, pattern="^(single|multi)$")
    daily_loss_limit: Optional[float] = Field(None, ge=0)
    max_drawdown: Optional[float] = Field(None, ge=0)
    positions_refresh_interval: Optional[int] = Field(None, ge=3, le=300)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_enabled: Optional[bool] = None


class BotSettingsResponse(BaseModel):
    bot_active: bool
    testnet_mode: bool
    default_trading_size_type: str
    risk_per_trade: float
    leverage_override: int
    default_stoploss_percent: Optional[float]
    default_trail_activation_pct: Optional[float]
    default_trail_callback_pct: Optional[float]
    default_trade_mode: str
    daily_loss_limit: float
    max_drawdown: float
    positions_refresh_interval: int
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_enabled: bool = False
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Trade Record ─────────────────────────────────────────────────────────────

class TradeRecordResponse(BaseModel):
    id: int
    account_id: int
    symbol: str
    timeframe: str
    action: str
    side: str
    entry_price: Optional[float]
    quantity: Optional[float]
    usdt_value: Optional[float]
    realized_pnl: float
    leverage: Optional[int]
    status: str
    error_message: Optional[str]
    market_type: Optional[str] = None
    executed_at: datetime

    class Config:
        from_attributes = True


# ── Dashboard / WebSocket ────────────────────────────────────────────────────

class AccountBalanceSnapshot(BaseModel):
    account_id: int
    account_name: str
    market_type: str = "futures"
    wallet_balance: float
    available_margin: float
    margin_utilization: float


class DashboardData(BaseModel):
    total_balance: float
    total_available_margin: float
    avg_margin_utilization: float
    accounts: list[AccountBalanceSnapshot]
