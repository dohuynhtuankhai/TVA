"""Webhook endpoint – ingests TradingView signals."""

import json
import logging
import re
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot_engine import bot_engine
from database import get_db, async_session
from models import WebhookLog
from schemas import WebhookPayload
from websocket_manager import ws_manager

router = APIRouter(prefix="/api", tags=["webhook"])
logger = logging.getLogger("algotrade.webhook")


# ── Timeframe mapping ─────────────────────────────────────────────────────
# TradingView sends timeframe.period as: "5" for 5m, "60" for 1h, "240" for 4h, "D" for 1D
# Users input: "5m", "1h", "4h", "1D"
# We normalize user-friendly formats to TradingView's format for matching.

TIMEFRAME_MAP = {
    # Minutes
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    # Hours (TradingView sends minutes)
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "8h": "480", "12h": "720",
    # Days/Weeks/Months
    "1d": "D", "1w": "W", "1M": "M",
    # Also accept raw TradingView values as-is
    "1": "1", "3": "3", "5": "5", "15": "15", "30": "30",
    "60": "60", "120": "120", "240": "240", "360": "360", "480": "480", "720": "720",
    "D": "D", "W": "W", "M": "M",
}


def normalize_timeframe(raw: str) -> str:
    """Convert user-friendly timeframe (5m, 1h) to TradingView format (5, 60).

    If the input is already in TradingView format or unrecognized, return as-is.
    """
    cleaned = raw.strip()
    # Try exact match (case-insensitive for most, case-sensitive for M vs m)
    if cleaned in TIMEFRAME_MAP:
        return TIMEFRAME_MAP[cleaned]
    # Try lowercase
    lower = cleaned.lower()
    if lower in TIMEFRAME_MAP:
        return TIMEFRAME_MAP[lower]
    # Return as-is if not recognized
    return cleaned


def _clean_symbol(raw: str) -> str:
    """Normalize TradingView ticker formats to plain symbol.

    TradingView sends tickers like:
      BINANCE:BTCUSDT, BTCUSDT.P, BINANCE:BTCUSDT.P, BTCUSDTPERP
    We need just: BTCUSDT
    """
    # Remove exchange prefix (BINANCE:, BYBIT:, etc.)
    symbol = raw.split(":")[-1]
    # Remove .P suffix (perpetual marker)
    symbol = re.sub(r"\.[A-Z]+$", "", symbol)
    # Remove PERP suffix
    symbol = re.sub(r"PERP$", "", symbol)
    return symbol.upper()


async def _process_and_broadcast(payload: WebhookPayload):
    """Background task: run the bot engine, sync from Binance, push via WebSocket."""
    start = time.monotonic()
    results = await bot_engine.process_signal(payload)
    elapsed_ms = (time.monotonic() - start) * 1000

    logger.info(
        "Signal %s %s@%s processed in %.0f ms → %s",
        payload.action, payload.symbol, payload.timeframe, elapsed_ms, results,
    )

    # ── Log webhook result to DB ────────────────────────────────────
    try:
        filled = sum(1 for r in results if r.get("status") == "FILLED" or r.get("status") == "CLOSED")
        errored = sum(1 for r in results if r.get("status") == "ERROR")
        no_mapping = any(r.get("status") == "NO_MAPPING" for r in results)

        if no_mapping:
            log_status = "NO_MAPPING"
        elif errored == len(results):
            log_status = "FAILED"
        elif errored > 0:
            log_status = "PARTIAL"
        else:
            log_status = "SUCCESS"

        async with async_session() as db:
            log_entry = WebhookLog(
                symbol=payload.symbol,
                timeframe=payload.timeframe,
                action=payload.action.upper(),
                price=payload.price,
                status=log_status,
                accounts_targeted=len(results),
                accounts_filled=filled,
                accounts_errored=errored,
                details=json.dumps(results, default=str),
                execution_ms=round(elapsed_ms, 1),
            )
            db.add(log_entry)
            await db.commit()
    except Exception as e:
        logger.warning("Failed to log webhook: %s", str(e))

    for r in results:
        await ws_manager.broadcast_trade(r)

    # Send Telegram notifications
    try:
        from notifications import notify_trade_filled
        for r in results:
            await notify_trade_filled(r, payload.action, payload.symbol)
    except Exception as e:
        logger.warning("Telegram notification failed: %s", str(e))

    # Auto-sync trade history from Binance after execution
    try:
        from routes.trades import sync_account_trades
        from sqlalchemy import select as sa_select
        from models import ExchangeAccount, SymbolMapping

        async with async_session() as db:
            acct_result = await db.execute(
                sa_select(ExchangeAccount)
                .join(SymbolMapping)
                .where(
                    SymbolMapping.symbol == payload.symbol.upper(),
                    SymbolMapping.timeframe == payload.timeframe,
                    ExchangeAccount.is_active == True,
                    ExchangeAccount.futures_enabled == True,
                )
            )
            accounts = acct_result.scalars().all()
            for acct in accounts:
                count = await sync_account_trades(acct, db)
                if count > 0:
                    logger.info("Auto-synced %d trades for %s after webhook", count, acct.name)
    except Exception as e:
        logger.warning("Auto-sync after webhook failed: %s", str(e))


@router.post("/webhook", status_code=200)
async def receive_webhook(payload: WebhookPayload, bg: BackgroundTasks):
    """Receive a TradingView JSON webhook.

    Returns HTTP 200 immediately and processes the signal in the background
    to meet the <500ms latency requirement.
    """
    # Clean the symbol from TradingView format
    payload.symbol = _clean_symbol(payload.symbol)

    logger.info("Webhook received: %s", payload.model_dump())

    if payload.action.upper() not in ("ENTRY", "EXIT", "LONG", "SHORT"):
        raise HTTPException(status_code=400, detail="Invalid action")

    bg.add_task(_process_and_broadcast, payload)

    return {"status": "accepted", "symbol": payload.symbol, "action": payload.action}


# ── Webhook Logs ──────────────────────────────────────────────────────────

@router.get("/webhook-logs")
async def list_webhook_logs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List recent webhook logs."""
    result = await db.execute(
        select(WebhookLog)
        .order_by(WebhookLog.received_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()
    return [
        {
            "id": log.id,
            "symbol": log.symbol,
            "timeframe": log.timeframe,
            "action": log.action,
            "price": log.price,
            "status": log.status,
            "accounts_targeted": log.accounts_targeted,
            "accounts_filled": log.accounts_filled,
            "accounts_errored": log.accounts_errored,
            "details": log.details,
            "execution_ms": log.execution_ms,
            "received_at": log.received_at.isoformat() if log.received_at else None,
        }
        for log in logs
    ]
