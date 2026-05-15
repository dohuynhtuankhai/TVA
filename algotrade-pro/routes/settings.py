"""Bot settings endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import BotSettings
from schemas import BotSettingsResponse, BotSettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _get_or_create(db: AsyncSession) -> BotSettings:
    result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = BotSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.get("/", response_model=BotSettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    return await _get_or_create(db)


@router.put("/", response_model=BotSettingsResponse)
async def update_settings(
    body: BotSettingsUpdate, db: AsyncSession = Depends(get_db)
):
    settings = await _get_or_create(db)

    if body.bot_active is not None:
        settings.bot_active = body.bot_active
    if body.testnet_mode is not None:
        settings.testnet_mode = body.testnet_mode
    if body.default_trading_size_type is not None:
        settings.default_trading_size_type = body.default_trading_size_type
    if body.risk_per_trade is not None:
        settings.risk_per_trade = body.risk_per_trade
    if body.leverage_override is not None:
        settings.leverage_override = body.leverage_override
    if body.default_stoploss_percent is not None:
        settings.default_stoploss_percent = body.default_stoploss_percent
    if body.default_trail_activation_pct is not None:
        settings.default_trail_activation_pct = body.default_trail_activation_pct
    if body.default_trail_callback_pct is not None:
        settings.default_trail_callback_pct = body.default_trail_callback_pct
    if body.default_trade_mode is not None:
        settings.default_trade_mode = body.default_trade_mode
    if body.daily_loss_limit is not None:
        settings.daily_loss_limit = body.daily_loss_limit
    if body.max_drawdown is not None:
        settings.max_drawdown = body.max_drawdown
    if body.positions_refresh_interval is not None:
        settings.positions_refresh_interval = body.positions_refresh_interval
    if body.telegram_bot_token is not None:
        settings.telegram_bot_token = body.telegram_bot_token
    if body.telegram_chat_id is not None:
        settings.telegram_chat_id = body.telegram_chat_id
    if body.telegram_enabled is not None:
        settings.telegram_enabled = body.telegram_enabled

    await db.commit()
    await db.refresh(settings)
    return settings
