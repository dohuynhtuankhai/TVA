"""Positions and Spot holdings endpoints."""

import asyncio
import logging

from binance.exceptions import BinanceAPIException
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session, get_db
from encryption import decrypt_secret
from market_adapters import create_market_adapter
from models import ExchangeAccount, TradeRecord
from websocket_manager import ws_manager

router = APIRouter(prefix="/api/positions", tags=["positions"])
logger = logging.getLogger("algotrade.positions")


async def _fetch_account_market_positions(
    acct: ExchangeAccount, market_type: str
) -> list[dict]:
    """Fetch positions/holdings for one (account, market) pair.

    Uses its own DB session because AsyncSession cannot be shared safely
    across concurrent gather tasks.
    """
    adapter = None
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        adapter = await create_market_adapter(
            acct.api_key, secret, market_type=market_type
        )
        async with async_session() as worker_db:
            return await adapter.fetch_positions(acct, worker_db)
    except Exception as e:
        logger.error(
            "Failed to fetch %s positions for %s: %s", market_type, acct.name, e
        )
        return []
    finally:
        if adapter is not None:
            await adapter.close()


def _enabled_markets(acct: ExchangeAccount) -> list[str]:
    pairs: list[str] = []
    if acct.futures_enabled:
        pairs.append("futures")
    if acct.spot_enabled:
        pairs.append("spot")
    return pairs


@router.get("/")
async def get_open_positions(db: AsyncSession = Depends(get_db)):
    """Fetch Futures positions and Spot holdings across active accounts."""
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.is_active == True,  # noqa: E712
        )
    )
    accounts = result.scalars().all()

    tasks = [
        _fetch_account_market_positions(acct, market)
        for acct in accounts
        for market in _enabled_markets(acct)
    ]
    results = await asyncio.gather(*tasks) if tasks else []
    all_positions = []
    for positions in results:
        all_positions.extend(positions)
    return all_positions


class ForceCloseRequest(BaseModel):
    account_id: int
    symbol: str
    market_type: str = "futures"


@router.post("/close")
async def force_close_position(body: ForceCloseRequest, db: AsyncSession = Depends(get_db)):
    """Close a Futures position or sell a Spot holding."""
    result = await db.execute(
        select(ExchangeAccount).where(ExchangeAccount.id == body.account_id)
    )
    acct = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(404, "Account not found")

    market_type = (body.market_type or "futures").lower()
    if market_type == "spot" and not acct.spot_enabled:
        raise HTTPException(400, "Account not verified for Spot.")
    if market_type == "futures" and not acct.futures_enabled:
        raise HTTPException(400, "Account not verified for Futures.")

    adapter = None
    symbol = body.symbol
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        adapter = await create_market_adapter(
            acct.api_key, secret, market_type=market_type
        )
        closed = await adapter.close_position(symbol)
    except BinanceAPIException as e:
        error_msg = f"Binance error: {e.message} (code {e.code})"
        logger.error("Close failed: %s", error_msg)
        raise HTTPException(500, error_msg)
    except Exception as e:
        logger.error("Close error: %s", str(e))
        raise HTTPException(500, str(e))
    finally:
        if adapter is not None:
            await adapter.close()

    if not closed:
        empty_status = (
            "NO_HOLDING" if market_type == "spot"
            else "NO_POSITION"
        )
        return {"status": empty_status, "symbol": symbol, "account": acct.name}

    positions_out: list[dict] = []
    for c in closed:
        usdt_value = c["close_price"] * c["quantity"]
        record = TradeRecord(
            account_id=acct.id,
            symbol=symbol,
            timeframe="manual",
            action=c["action"],
            side=c["close_side"],
            entry_price=c["close_price"],
            quantity=c["quantity"],
            usdt_value=round(usdt_value, 2),
            realized_pnl=c["realized_pnl"],
            leverage=c["leverage"],
            status="FILLED",
            error_message=None,
            market_type=c["market_type"],
        )
        db.add(record)
        positions_out.append({
            "symbol": symbol,
            "market_type": c["market_type"],
            "side": c["side_label"],
            "quantity": c["quantity"],
            "close_price": c["close_price"],
            "realized_pnl": c["realized_pnl"],
        })

        await ws_manager.broadcast_trade({
            "status": "CLOSED",
            "market_type": c["market_type"],
            "account": acct.name,
            "symbol": symbol,
            "side": c["close_side"],
            "quantity": c["quantity"],
            "realized_pnl": c["realized_pnl"],
        })

        try:
            from notifications import notify_force_close
            label = c["side_label"] if c["market_type"] == "futures" else "SPOT SELL"
            await notify_force_close(acct.name, symbol, label, c["realized_pnl"])
        except Exception as notif_err:
            logger.warning("Telegram notification failed: %s", str(notif_err))

    await db.commit()

    return {"status": "CLOSED", "account": acct.name, "positions": positions_out}
