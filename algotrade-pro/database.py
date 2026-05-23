"""Async SQLAlchemy engine and session factory."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings

logger = logging.getLogger("algotrade.database")

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """FastAPI dependency – yields a DB session per request."""
    async with async_session() as session:
        yield session


async def init_db():
    """Create all tables on startup and add any missing columns."""
    from models import Base  # noqa: F811

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Auto-migrate: add missing columns to existing tables (SQLite safe)
    await _add_missing_columns()


async def _add_missing_columns():
    """Add columns that may be missing from older DB versions."""
    migrations = [
        ("bot_settings", "telegram_bot_token", "VARCHAR(256)"),
        ("bot_settings", "telegram_chat_id", "VARCHAR(64)"),
        ("bot_settings", "telegram_enabled", "BOOLEAN DEFAULT 0"),
        ("exchange_accounts", "market_type", "VARCHAR(10) DEFAULT 'futures'"),
        ("symbol_mappings", "market_type", "VARCHAR(10) DEFAULT 'futures'"),
        ("trade_records", "market_type", "VARCHAR(10) DEFAULT 'futures'"),
        ("webhook_logs", "market_type", "VARCHAR(10)"),
    ]

    async with engine.begin() as conn:
        for table, column, col_type in migrations:
            try:
                await conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    )
                )
            except Exception as e:
                # "duplicate column" is expected for already-migrated DBs
                logger.debug("Migration skip %s.%s: %s", table, column, e)
