"""Bot settings endpoints."""

import logging

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import BotSettings
from schemas import BotSettingsResponse, BotSettingsUpdate

logger = logging.getLogger("algotrade.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])
MASKED_TOKEN = "••••••••"


async def _get_or_create(db: AsyncSession) -> BotSettings:
    result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = BotSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def _to_response(settings: BotSettings) -> dict:
    """Return settings without exposing saved Telegram bot tokens."""
    return {
        "bot_active": settings.bot_active,
        "testnet_mode": settings.testnet_mode,
        "default_trading_size_type": settings.default_trading_size_type,
        "risk_per_trade": settings.risk_per_trade,
        "leverage_override": settings.leverage_override,
        "default_stoploss_percent": settings.default_stoploss_percent,
        "default_trail_activation_pct": settings.default_trail_activation_pct,
        "default_trail_callback_pct": settings.default_trail_callback_pct,
        "default_trade_mode": settings.default_trade_mode,
        "daily_loss_limit": settings.daily_loss_limit,
        "max_drawdown": settings.max_drawdown,
        "positions_refresh_interval": settings.positions_refresh_interval,
        "telegram_bot_token": MASKED_TOKEN if settings.telegram_bot_token else None,
        "telegram_chat_id": settings.telegram_chat_id,
        "telegram_enabled": settings.telegram_enabled,
        "updated_at": settings.updated_at,
    }


@router.get("/", response_model=BotSettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    settings = await _get_or_create(db)
    return _to_response(settings)


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
    if body.telegram_bot_token is not None and body.telegram_bot_token != MASKED_TOKEN:
        settings.telegram_bot_token = body.telegram_bot_token
    if body.telegram_chat_id is not None:
        settings.telegram_chat_id = body.telegram_chat_id
    if body.telegram_enabled is not None:
        settings.telegram_enabled = body.telegram_enabled

    await db.commit()
    await db.refresh(settings)
    return _to_response(settings)


@router.post("/test-telegram")
async def test_telegram(db: AsyncSession = Depends(get_db)):
    """Send a test message via Telegram using saved config (server-side)."""
    settings = await _get_or_create(db)

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(400, "Telegram bot token and chat ID must be configured first")

    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": "✅ AlgoTrade Pro — Telegram connected!",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("ok"):
                    return {"status": "ok", "message": "Test message sent"}
                else:
                    desc = data.get("description", "Unknown error")
                    logger.warning("Telegram test failed: %s", desc)
                    raise HTTPException(400, f"Telegram error: {desc}")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Telegram test error: %s", str(e))
        raise HTTPException(500, f"Failed to send: {str(e)}")
