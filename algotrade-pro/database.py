"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings

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
    ]

    async with engine.begin() as conn:
        for table, column, col_type in migrations:
            try:
                await conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    )
                )
            except Exception:
                pass  # Column already exists
