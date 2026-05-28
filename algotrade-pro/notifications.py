"""Telegram notification system for trade events."""

import logging
from datetime import datetime, timezone

import aiohttp

from config import settings

logger = logging.getLogger("algopro.notifications")


async def _get_telegram_config() -> tuple[str, str] | None:
    """Load Telegram bot token and chat ID from BotSettings."""
    from database import async_session
    from sqlalchemy import select
    from models import BotSettings

    async with async_session() as db:
        result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.scalar_one_or_none()
        if not bot_settings:
            return None

        enabled = getattr(bot_settings, "telegram_enabled", False)
        if not enabled:
            return None

        token = getattr(bot_settings, "telegram_bot_token", None) or ""
        chat_id = getattr(bot_settings, "telegram_chat_id", None) or ""

        if not token or not chat_id:
            return None

        return token.strip(), chat_id.strip()


async def send_telegram(message: str):
    """Send a message via Telegram Bot API."""
    config = await _get_telegram_config()
    if not config:
        logger.debug("Telegram not configured — skipping notification")
        return

    token, chat_id = config
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    logger.info("Telegram notification sent")
                else:
                    body = await resp.text()
                    logger.warning("Telegram send failed (%s): %s", resp.status, body)
    except Exception as e:
        logger.warning("Telegram notification error: %s", str(e))


async def notify_trade_filled(result: dict, payload_action: str, payload_symbol: str):
    """Send notification for a filled trade."""
    status = result.get("status", "")
    account = result.get("account", "Unknown")
    symbol = result.get("symbol", payload_symbol)

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    if status == "FILLED":
        action = payload_action.upper()
        qty = result.get("quantity", 0)
        price = result.get("entry_price", 0)
        value = result.get("usdt_value", 0)

        emoji = "🟢" if action in ("LONG", "BUY") else "🔴"
        msg = (
            f"{emoji} <b>TRADE FILLED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Account: <b>{account}</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Action: <b>{action}</b>\n"
            f"Qty: {qty}\n"
            f"Price: ${price:,.2f}\n"
            f"Value: ${value:,.2f}\n"
            f"Time: {now}"
        )
        await send_telegram(msg)

    elif status == "CLOSED":
        pnl = result.get("realized_pnl", 0)
        emoji = "💰" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""

        msg = (
            f"{emoji} <b>POSITION CLOSED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Account: <b>{account}</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"P&amp;L: <b>{pnl_sign}${pnl:,.2f}</b>\n"
            f"Time: {now}"
        )
        await send_telegram(msg)

    elif status == "ERROR":
        error = result.get("error", "Unknown error")
        msg = (
            f"⚠️ <b>TRADE ERROR</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Account: <b>{account}</b>\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Error: {error}\n"
            f"Time: {now}"
        )
        await send_telegram(msg)

    elif status == "BLOCKED":
        reason = result.get("reason", "Unknown")
        msg = (
            f"🚫 <b>TRADE BLOCKED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Reason: {reason}\n"
            f"Time: {now}"
        )
        await send_telegram(msg)


async def notify_order_placed(account_name: str, symbol: str, action: str, side: str, quantity: float, leverage: int):
    """Send notification when an order is placed on Binance (before fill confirmation)."""
    emoji = "📤"
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    action_label = action.upper()

    msg = (
        f"{emoji} <b>ORDER PLACED</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Account: <b>{account_name}</b>\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Action: <b>{action_label}</b>\n"
        f"Side: <b>{side}</b>\n"
        f"Qty: {quantity}\n"
        f"Leverage: {leverage}x\n"
        f"Time: {now}"
    )
    await send_telegram(msg)


async def notify_force_close(account_name: str, symbol: str, side: str, pnl: float):
    """Send notification for a manual force close."""
    emoji = "💰" if pnl >= 0 else "📉"
    pnl_sign = "+" if pnl >= 0 else ""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    msg = (
        f"{emoji} <b>FORCE CLOSED</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Account: <b>{account_name}</b>\n"
        f"Symbol: <b>{symbol}</b>\n"
        f"Side: <b>{side}</b>\n"
        f"P&amp;L: <b>{pnl_sign}${pnl:,.2f}</b>\n"
        f"Time: {now}"
    )
    await send_telegram(msg)
