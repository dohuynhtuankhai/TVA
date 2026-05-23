"""SQLAlchemy ORM models for AlgoTrade Pro."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ExchangeAccount(Base):
    """A Binance exchange account with encrypted credentials."""

    __tablename__ = "exchange_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    api_key = Column(String(256), nullable=False)
    api_secret_encrypted = Column(Text, nullable=False)  # Fernet ciphertext
    is_active = Column(Boolean, default=True)
    futures_enabled = Column(Boolean, default=False)
    market_type = Column(String(10), default="futures")  # "futures" or "spot"

    # ── Per-account trading settings ─────────────────────────────────
    trading_size_type = Column(String(10), default="percent")  # "percent" or "fixed"
    trading_size_value = Column(Float, default=5.0)  # % of balance or fixed USDT
    leverage = Column(Integer, default=10)
    stoploss_percent = Column(Float, nullable=True, default=None)  # optional, e.g. 2.0 = 2%
    trail_activation_pct = Column(Float, nullable=True, default=None)  # e.g. 2.0 = activate after 2% profit
    trail_callback_pct = Column(Float, nullable=True, default=None)  # e.g. 1.0 = trail by 1%
    trade_mode = Column(String(10), default="single")  # "single" or "multi"

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    symbol_mappings = relationship(
        "SymbolMapping", back_populates="account", cascade="all, delete-orphan"
    )
    trades = relationship(
        "TradeRecord", back_populates="account", cascade="all, delete-orphan"
    )


class SymbolMapping(Base):
    """Maps a (symbol, timeframe) pair to a specific exchange account.

    This determines which account executes a signal for BTCUSDT on 5m, etc.
    """

    __tablename__ = "symbol_mappings"
    __table_args__ = (
        Index("ix_mapping_symbol_tf", "symbol", "timeframe"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)  # e.g. BTCUSDT
    timeframe = Column(String(10), nullable=False)  # e.g. 5m, 15m, 1h
    account_id = Column(Integer, ForeignKey("exchange_accounts.id"), nullable=False)
    market_type = Column(String(10), default="futures")  # denormalized for clear routing/UI

    account = relationship("ExchangeAccount", back_populates="symbol_mappings")


class BotSettings(Base):
    """Global bot configuration – single-row table.

    risk_per_trade, leverage_override, and default_stoploss serve as
    DEFAULT values when creating new accounts.  Changing them here does
    NOT retroactively change existing account settings.
    """

    __tablename__ = "bot_settings"

    id = Column(Integer, primary_key=True, default=1)
    bot_active = Column(Boolean, default=True)
    testnet_mode = Column(Boolean, default=True)  # True = fake money, False = real

    # ── Defaults for new accounts ────────────────────────────────────
    default_trading_size_type = Column(String(10), default="percent")  # "percent" or "fixed"
    risk_per_trade = Column(Float, default=5.0)  # Default trading size value
    leverage_override = Column(Integer, default=10)  # Default leverage
    default_stoploss_percent = Column(Float, nullable=True, default=None)  # Default SL
    default_trail_activation_pct = Column(Float, nullable=True, default=2.0)  # Default trail activation
    default_trail_callback_pct = Column(Float, nullable=True, default=1.0)  # Default trail callback
    default_trade_mode = Column(String(10), default="single")  # "single" or "multi"

    # ── Global risk limits ───────────────────────────────────────────
    daily_loss_limit = Column(Float, default=500.0)  # USDT
    max_drawdown = Column(Float, default=2000.0)  # USDT

    # ── Notifications ───────────────────────────────────────────────
    telegram_bot_token = Column(String(256), nullable=True, default=None)
    telegram_chat_id = Column(String(64), nullable=True, default=None)
    telegram_enabled = Column(Boolean, default=False)

    # ── UI settings ──────────────────────────────────────────────────
    positions_refresh_interval = Column(Integer, default=10)  # seconds
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class TradeRecord(Base):
    """Ledger of every trade executed by the Bot Engine."""

    __tablename__ = "trade_records"
    __table_args__ = (
        Index("ix_trade_account_symbol", "account_id", "symbol"),
        Index("ix_trade_executed_at", "executed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("exchange_accounts.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    action = Column(String(10), nullable=False)  # BUY, SELL, EXIT, LONG, SHORT
    side = Column(String(10), nullable=False)  # BUY, SELL
    entry_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=True)
    usdt_value = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0)
    leverage = Column(Integer, nullable=True)
    status = Column(String(20), default="FILLED")  # FILLED, REJECTED, ERROR
    error_message = Column(Text, nullable=True)
    market_type = Column(String(10), default="futures")
    executed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    account = relationship("ExchangeAccount", back_populates="trades")


class WebhookLog(Base):
    """Log of every webhook received and its execution result."""

    __tablename__ = "webhook_logs"
    __table_args__ = (
        Index("ix_webhook_received_at", "received_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    action = Column(String(10), nullable=False)  # BUY, SELL, EXIT, LONG, SHORT
    price = Column(Float, nullable=True)
    status = Column(String(20), nullable=False)  # SUCCESS, PARTIAL, FAILED, NO_MAPPING
    accounts_targeted = Column(Integer, default=0)
    accounts_filled = Column(Integer, default=0)
    accounts_errored = Column(Integer, default=0)
    details = Column(Text, nullable=True)  # JSON string of results
    execution_ms = Column(Float, nullable=True)
    market_type = Column(String(10), nullable=True)
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DailyPnl(Base):
    """Tracks daily profit/loss per account for risk limit checks."""

    __tablename__ = "daily_pnl"
    __table_args__ = (
        Index("ix_daily_pnl_account_date", "account_id", "date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("exchange_accounts.id"), nullable=False)
    date = Column(String(10), nullable=False)  # YYYY-MM-DD
    realized_pnl = Column(Float, default=0.0)
    trade_count = Column(Integer, default=0)
