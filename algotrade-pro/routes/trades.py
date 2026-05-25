"""Trade history endpoints."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session, get_db
from encryption import decrypt_secret
from market_adapters import create_market_adapter, normalize_market_type
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
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date_from must be YYYY-MM-DD format")
        query = query.where(TradeRecord.executed_at >= date_from)
    if date_to:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date_to must be YYYY-MM-DD format")
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
            "market_type": row.TradeRecord.market_type or getattr(row, "market_type", None) or "futures",
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

async def _sync_one_market(
    account: ExchangeAccount, market_type: str, db: AsyncSession
) -> int:
    """Pull trade history from Binance for one (account, market) pair."""
    adapter = None
    imported = 0
    market_type = normalize_market_type(market_type)
    try:
        secret = decrypt_secret(account.api_secret_encrypted)
        adapter = await create_market_adapter(
            account.api_key, secret, market_type=market_type
        )
        trades = await adapter.fetch_remote_trades(account, db)

        for t in trades:
            existing = await db.execute(
                select(TradeRecord).where(
                    TradeRecord.account_id == account.id,
                    TradeRecord.symbol == t["symbol"],
                    TradeRecord.executed_at == t["time"],
                    TradeRecord.quantity == t["quantity"],
                )
            )
            if existing.scalar_one_or_none():
                continue

            usdt_value = t["price"] * t["quantity"]
            record = TradeRecord(
                account_id=account.id,
                symbol=t["symbol"],
                timeframe="binance",
                action=t["action"],
                side=t["side"],
                entry_price=t["price"],
                quantity=t["quantity"],
                usdt_value=round(usdt_value, 2),
                realized_pnl=round(t["realized_pnl"], 2),
                leverage=t["leverage"],
                status="FILLED",
                error_message=None,
                market_type=market_type,
                executed_at=t["time"],
            )
            db.add(record)
            imported += 1

        if imported > 0:
            await db.commit()

        logger.info(
            "Synced %d new %s trades for account '%s'",
            imported, market_type, account.name,
        )

    except Exception as e:
        logger.error(
            "%s sync failed for account '%s': %s",
            market_type, account.name, str(e),
        )
    finally:
        if adapter is not None:
            await adapter.close()

    return imported


async def sync_account_trades(account: ExchangeAccount, db: AsyncSession) -> int:
    """Pull trade history for every enabled market on the account."""
    total = 0
    if account.futures_enabled:
        total += await _sync_one_market(account, "futures", db)
    if account.spot_enabled:
        total += await _sync_one_market(account, "spot", db)
    return total


@router.post("/sync")
async def sync_all_trades(db: AsyncSession = Depends(get_db)):
    """Pull trade history from Binance for all active accounts."""
    result = await db.execute(
        select(ExchangeAccount.id).where(
            ExchangeAccount.is_active == True,  # noqa: E712
        )
    )
    account_ids = result.scalars().all()

    # Sync accounts in parallel. Each worker uses its own DB session because
    # AsyncSession cannot be shared safely across concurrent tasks.
    async def _sync_one(account_id: int):
        async with async_session() as worker_db:
            acct_result = await worker_db.execute(
                select(ExchangeAccount).where(ExchangeAccount.id == account_id)
            )
            acct = acct_result.scalar_one_or_none()
            if not acct:
                return {"account": f"#{account_id}", "imported": 0}
            count = await sync_account_trades(acct, worker_db)
        return {"account": acct.name, "imported": count}

    results = await asyncio.gather(*[_sync_one(account_id) for account_id in account_ids])
    total_imported = sum(r["imported"] for r in results)

    return {
        "status": "ok",
        "total_imported": total_imported,
        "accounts": list(results),
    }
