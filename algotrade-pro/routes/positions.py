"""Open positions endpoint – fetches live positions from Binance for each account."""

import asyncio
import logging

from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from bot_engine import create_binance_client
from database import get_db
from encryption import decrypt_secret
from models import ExchangeAccount, TradeRecord
from websocket_manager import ws_manager

router = APIRouter(prefix="/api/positions", tags=["positions"])
logger = logging.getLogger("algotrade.positions")


async def _fetch_account_positions(acct: ExchangeAccount) -> list[dict]:
    """Fetch positions for a single account (used in parallel gather)."""
    client = None
    positions_out = []
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        client = await create_binance_client(acct.api_key, secret)
        positions = await client.futures_position_information()

        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue

            entry_price = float(p.get("entryPrice", 0))
            mark_price = float(p.get("markPrice", 0))
            unrealized_pnl = float(p.get("unRealizedProfit", 0))
            leverage = int(p.get("leverage", 1))
            notional = abs(float(p.get("notional", 0)))

            positions_out.append({
                "account_id": acct.id,
                "account_name": acct.name,
                "symbol": p.get("symbol", ""),
                "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt),
                "entry_price": entry_price,
                "mark_price": mark_price,
                "notional": round(notional, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "leverage": leverage,
                "pnl_percent": round(
                    (unrealized_pnl / (notional / leverage) * 100) if notional > 0 and leverage > 0 else 0, 2
                ),
            })
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", acct.name, e)
    finally:
        if client:
            await client.close_connection()
    return positions_out


@router.get("/")
async def get_open_positions(db: AsyncSession = Depends(get_db)):
    """Fetch all open futures positions across all active accounts in parallel."""
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.is_active == True,  # noqa: E712
            ExchangeAccount.futures_enabled == True,
        )
    )
    accounts = result.scalars().all()

    # Fetch all accounts in parallel
    results = await asyncio.gather(
        *[_fetch_account_positions(acct) for acct in accounts]
    )

    # Flatten list of lists
    all_positions = []
    for positions in results:
        all_positions.extend(positions)

    return all_positions


# ── Force close ──────────────────────────────────────────────────────────────

class ForceCloseRequest(BaseModel):
    account_id: int
    symbol: str


@router.post("/close")
async def force_close_position(body: ForceCloseRequest, db: AsyncSession = Depends(get_db)):
    """Force-close an open position on Binance."""
    result = await db.execute(
        select(ExchangeAccount).where(ExchangeAccount.id == body.account_id)
    )
    acct = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(404, "Account not found")

    client = None
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        client = await create_binance_client(acct.api_key, secret)

        positions = await client.futures_position_information(symbol=body.symbol)
        closed = []

        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue

            close_side = SIDE_SELL if amt > 0 else SIDE_BUY
            quantity = abs(amt)

            order = await client.futures_create_order(
                symbol=body.symbol,
                side=close_side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=quantity,
                reduceOnly=True,
            )

            entry_price = float(p.get("entryPrice", 0))
            realized_pnl = float(p.get("unRealizedProfit", 0))
            leverage = int(p.get("leverage", 1))
            usdt_value = entry_price * quantity

            # Record to trade ledger
            record = TradeRecord(
                account_id=acct.id,
                symbol=body.symbol,
                timeframe="manual",
                action="EXIT",
                side=close_side,
                entry_price=entry_price,
                quantity=quantity,
                usdt_value=round(usdt_value, 2),
                realized_pnl=round(realized_pnl, 2),
                leverage=leverage,
                status="FILLED",
                error_message=None,
            )
            db.add(record)
            await db.commit()

            trade_result = {
                "symbol": body.symbol,
                "side": "LONG" if amt > 0 else "SHORT",
                "quantity": quantity,
                "entry_price": entry_price,
                "realized_pnl": round(realized_pnl, 2),
            }
            closed.append(trade_result)

            logger.info(
                "Force-closed %s %s (%.4f) @ %s on account %s → PnL: %.2f",
                "LONG" if amt > 0 else "SHORT", body.symbol, quantity,
                entry_price, acct.name, realized_pnl,
            )

            # Push to dashboard via WebSocket
            await ws_manager.broadcast_trade({
                "status": "CLOSED",
                "account": acct.name,
                "symbol": body.symbol,
                "side": close_side,
                "quantity": quantity,
                "realized_pnl": round(realized_pnl, 2),
            })

            # Telegram notification
            try:
                from notifications import notify_force_close
                await notify_force_close(
                    acct.name, body.symbol,
                    "LONG" if amt > 0 else "SHORT",
                    round(realized_pnl, 2),
                )
            except Exception as notif_err:
                logger.warning("Telegram notification failed: %s", str(notif_err))

        if not closed:
            return {"status": "NO_POSITION", "symbol": body.symbol, "account": acct.name}

        return {"status": "CLOSED", "account": acct.name, "positions": closed}

    except BinanceAPIException as e:
        error_msg = f"Binance error: {e.message} (code {e.code})"
        logger.error("Force close failed: %s", error_msg)
        raise HTTPException(500, error_msg)
    except Exception as e:
        logger.error("Force close error: %s", str(e))
        raise HTTPException(500, str(e))
    finally:
        if client:
            await client.close_connection()
