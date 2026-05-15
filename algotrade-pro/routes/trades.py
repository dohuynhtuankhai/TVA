"""Trade history endpoints."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from bot_engine import create_binance_client
from database import get_db
from encryption import decrypt_secret
from models import ExchangeAccount, TradeRecord

logger = logging.getLogger("algotrade.trades")

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("/")
async def list_trades(
    account_id: int | None = None,
    symbol: str | None = None,
    side: str | None = None,
    action: str | None = None,
    status: str | None = None,
    date_from: str | None = None,  # YYYY-MM-DD
    date_to: str | None = None,    # YYYY-MM-DD
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    # Build query with join to get account name
    query = (
        select(
            TradeRecord,
            ExchangeAccount.name.label("account_name"),
        )
        .join(ExchangeAccount, TradeRecord.account_id == ExchangeAccount.id, isouter=True)
        .order_by(TradeRecord.executed_at.desc())
    )

    if account_id:
        query = query.where(TradeRecord.account_id == account_id)
    if symbol:
        query = query.where(TradeRecord.symbol == symbol.upper())
    if side:
        query = query.where(TradeRecord.side == side.upper())
    if action:
        query = query.where(TradeRecord.action == action.upper())
    if status:
        query = query.where(TradeRecord.status == status.upper())
    if date_from:
        query = query.where(TradeRecord.executed_at >= date_from)
    if date_to:
        query = query.where(TradeRecord.executed_at <= date_to + "T23:59:59")

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": row.TradeRecord.id,
            "account_id": row.TradeRecord.account_id,
            "account_name": row.account_name or "Unknown",
            "symbol": row.TradeRecord.symbol,
            "timeframe": row.TradeRecord.timeframe,
            "action": row.TradeRecord.action,
            "side": row.TradeRecord.side,
            "entry_price": row.TradeRecord.entry_price,
            "quantity": row.TradeRecord.quantity,
            "usdt_value": row.TradeRecord.usdt_value,
            "realized_pnl": row.TradeRecord.realized_pnl,
            "leverage": row.TradeRecord.leverage,
            "status": row.TradeRecord.status,
            "error_message": row.TradeRecord.error_message,
            "executed_at": row.TradeRecord.executed_at.isoformat() if row.TradeRecord.executed_at else None,
        }
        for row in rows
    ]


@router.get("/stats")
async def trade_stats(
    account_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Aggregate trade statistics, optionally filtered by account."""
    base = select(func.count(TradeRecord.id))
    pnl_base = select(func.sum(TradeRecord.realized_pnl))

    if account_id:
        base = base.where(TradeRecord.account_id == account_id)
        pnl_base = pnl_base.where(TradeRecord.account_id == account_id)

    total = await db.execute(base)
    total_pnl = await db.execute(pnl_base)

    filled_q = select(func.count(TradeRecord.id)).where(TradeRecord.status == "FILLED")
    errors_q = select(func.count(TradeRecord.id)).where(TradeRecord.status == "ERROR")
    if account_id:
        filled_q = filled_q.where(TradeRecord.account_id == account_id)
        errors_q = errors_q.where(TradeRecord.account_id == account_id)

    filled = await db.execute(filled_q)
    errors = await db.execute(errors_q)

    # Win rate
    wins_q = select(func.count(TradeRecord.id)).where(
        TradeRecord.status == "FILLED", TradeRecord.realized_pnl > 0
    )
    if account_id:
        wins_q = wins_q.where(TradeRecord.account_id == account_id)
    wins = await db.execute(wins_q)

    filled_count = filled.scalar() or 0
    win_count = wins.scalar() or 0

    return {
        "total_trades": total.scalar() or 0,
        "total_pnl": round(total_pnl.scalar() or 0.0, 2),
        "filled_count": filled_count,
        "error_count": errors.scalar() or 0,
        "win_count": win_count,
        "win_rate": round((win_count / filled_count * 100) if filled_count > 0 else 0, 1),
    }


@router.get("/accounts")
async def trade_accounts(db: AsyncSession = Depends(get_db)):
    """List accounts that have trades (for filter dropdown)."""
    result = await db.execute(
        select(ExchangeAccount.id, ExchangeAccount.name)
        .join(TradeRecord, ExchangeAccount.id == TradeRecord.account_id)
        .distinct()
        .order_by(ExchangeAccount.name)
    )
    return [{"id": r.id, "name": r.name} for r in result.all()]


# ── Binance Trade Sync ──────────────────────────────────────────────────────

async def sync_account_trades(account: ExchangeAccount, db: AsyncSession) -> int:
    """Pull trade history from Binance for one account. Returns count of new trades imported."""
    client = None
    imported = 0
    try:
        secret = decrypt_secret(account.api_secret_encrypted)
        client = await create_binance_client(account.api_key, secret)

        # Get all futures trades (Binance returns up to 1000 per call)
        trades = await client.futures_account_trades()

        for t in trades:
            # Check if we already have this trade (by matching binance trade id via a combo key)
            trade_time = datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc)
            symbol = t["symbol"]
            price = float(t["price"])
            qty = float(t["qty"])
            realized_pnl = float(t.get("realizedPnl", 0))
            side = t["side"]  # BUY or SELL
            commission = float(t.get("commission", 0))

            # Deduplicate: check if trade with same account + symbol + time + qty exists
            existing = await db.execute(
                select(TradeRecord).where(
                    TradeRecord.account_id == account.id,
                    TradeRecord.symbol == symbol,
                    TradeRecord.executed_at == trade_time,
                    TradeRecord.quantity == qty,
                )
            )
            if existing.scalar_one_or_none():
                continue

            # Determine action from side and context
            is_buyer = t.get("buyer", side == "BUY")
            action = "LONG" if side == "BUY" else "SHORT"

            # If reduceOnly or realized PnL != 0, it's likely an exit
            if t.get("reduceOnly", False) or (realized_pnl != 0):
                action = "EXIT"

            usdt_value = price * qty

            record = TradeRecord(
                account_id=account.id,
                symbol=symbol,
                timeframe="binance",
                action=action,
                side=side,
                entry_price=price,
                quantity=qty,
                usdt_value=round(usdt_value, 2),
                realized_pnl=round(realized_pnl, 2),
                leverage=account.leverage,
                status="FILLED",
                error_message=None,
                executed_at=trade_time,
            )
            db.add(record)
            imported += 1

        if imported > 0:
            await db.commit()

        logger.info("Synced %d new trades for account '%s'", imported, account.name)

    except Exception as e:
        logger.error("Sync failed for account '%s': %s", account.name, str(e))
    finally:
        if client:
            await client.close_connection()

    return imported


@router.post("/sync")
async def sync_all_trades(db: AsyncSession = Depends(get_db)):
    """Pull trade history from Binance for all active accounts."""
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.is_active == True,  # noqa: E712
            ExchangeAccount.futures_enabled == True,
        )
    )
    accounts = result.scalars().all()

    total_imported = 0
    results = []

    for acct in accounts:
        count = await sync_account_trades(acct, db)
        total_imported += count
        results.append({"account": acct.name, "imported": count})

    return {
        "status": "ok",
        "total_imported": total_imported,
        "accounts": results,
    }
