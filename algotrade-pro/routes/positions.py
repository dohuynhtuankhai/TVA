"""Positions and Spot holdings endpoints."""

import asyncio
import logging

from binance.enums import SIDE_BUY, SIDE_SELL, FUTURE_ORDER_TYPE_MARKET, ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot_engine import create_binance_client
from database import get_db
from encryption import decrypt_secret
from models import ExchangeAccount, TradeRecord
from websocket_manager import ws_manager

router = APIRouter(prefix="/api/positions", tags=["positions"])
logger = logging.getLogger("algotrade.positions")


def _market_type(acct: ExchangeAccount) -> str:
    return (acct.market_type or "futures").lower()


async def _fetch_account_positions(acct: ExchangeAccount) -> list[dict]:
    """Fetch Futures positions or Spot holdings for one account."""
    client = None
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        market_type = _market_type(acct)
        client = await create_binance_client(acct.api_key, secret, market_type=market_type)

        if market_type == "spot":
            return await _fetch_spot_holdings(client, acct)
        return await _fetch_futures_positions(client, acct)
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", acct.name, e)
        return []
    finally:
        if client:
            await client.close_connection()


async def _fetch_futures_positions(client, acct: ExchangeAccount) -> list[dict]:
    positions_out = []
    positions = await client.futures_position_information()

    for position in positions:
        amt = float(position.get("positionAmt", 0))
        if amt == 0:
            continue

        entry_price = float(position.get("entryPrice", 0))
        mark_price = float(position.get("markPrice", 0))
        unrealized_pnl = float(position.get("unRealizedProfit", 0))
        leverage = int(position.get("leverage", 1))
        notional = abs(float(position.get("notional", 0)))

        positions_out.append({
            "account_id": acct.id,
            "account_name": acct.name,
            "market_type": "futures",
            "symbol": position.get("symbol", ""),
            "asset": position.get("symbol", ""),
            "side": "LONG" if amt > 0 else "SHORT",
            "size": abs(amt),
            "free": abs(amt),
            "locked": 0,
            "entry_price": entry_price,
            "mark_price": mark_price,
            "notional": round(notional, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "leverage": leverage,
            "pnl_percent": round(
                (unrealized_pnl / (notional / leverage) * 100)
                if notional > 0 and leverage > 0 else 0,
                2,
            ),
        })

    return positions_out


async def _fetch_spot_holdings(client, acct: ExchangeAccount) -> list[dict]:
    holdings_out = []
    account = await client.get_account()

    for balance in account.get("balances", []):
        asset = balance["asset"]
        if asset == "USDT":
            continue
        free = float(balance.get("free", 0) or 0)
        locked = float(balance.get("locked", 0) or 0)
        amount = free + locked
        if amount <= 0:
            continue

        symbol = f"{asset}USDT"
        try:
            ticker = await client.get_symbol_ticker(symbol=symbol)
            mark_price = float(ticker["price"])
        except Exception:
            logger.debug("Skipping Spot holding without USDT ticker: %s", asset)
            continue

        notional = amount * mark_price
        holdings_out.append({
            "account_id": acct.id,
            "account_name": acct.name,
            "market_type": "spot",
            "symbol": symbol,
            "asset": asset,
            "side": "HOLD",
            "size": amount,
            "free": free,
            "locked": locked,
            "entry_price": 0,
            "mark_price": mark_price,
            "notional": round(notional, 2),
            "unrealized_pnl": 0,
            "leverage": 1,
            "pnl_percent": 0,
        })

    return holdings_out


@router.get("/")
async def get_open_positions(db: AsyncSession = Depends(get_db)):
    """Fetch Futures positions and Spot holdings across active accounts."""
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.is_active == True,  # noqa: E712
            ExchangeAccount.futures_enabled == True,
        )
    )
    accounts = result.scalars().all()

    results = await asyncio.gather(*[_fetch_account_positions(acct) for acct in accounts])
    all_positions = []
    for positions in results:
        all_positions.extend(positions)
    return all_positions


class ForceCloseRequest(BaseModel):
    account_id: int
    symbol: str


@router.post("/close")
async def force_close_position(body: ForceCloseRequest, db: AsyncSession = Depends(get_db)):
    """Close a Futures position or sell a Spot holding."""
    result = await db.execute(
        select(ExchangeAccount).where(ExchangeAccount.id == body.account_id)
    )
    acct = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(404, "Account not found")

    client = None
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        market_type = _market_type(acct)
        client = await create_binance_client(acct.api_key, secret, market_type=market_type)

        if market_type == "spot":
            return await _sell_spot_holding(db, client, acct, body.symbol)
        return await _close_futures_position(db, client, acct, body.symbol)

    except BinanceAPIException as e:
        error_msg = f"Binance error: {e.message} (code {e.code})"
        logger.error("Close failed: %s", error_msg)
        raise HTTPException(500, error_msg)
    except Exception as e:
        logger.error("Close error: %s", str(e))
        raise HTTPException(500, str(e))
    finally:
        if client:
            await client.close_connection()


async def _close_futures_position(db: AsyncSession, client, acct: ExchangeAccount, symbol: str):
    positions = await client.futures_position_information(symbol=symbol)
    closed = []

    for position in positions:
        amt = float(position.get("positionAmt", 0))
        if amt == 0:
            continue

        close_side = SIDE_SELL if amt > 0 else SIDE_BUY
        quantity = abs(amt)
        order = await client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
            reduceOnly=True,
        )

        close_price = _resolve_order_price(order)
        if close_price == 0:
            close_price = float(position.get("markPrice", 0) or position.get("entryPrice", 0))
        realized_pnl = float(position.get("unRealizedProfit", 0))
        leverage = int(position.get("leverage", 1))
        usdt_value = close_price * quantity

        record = TradeRecord(
            account_id=acct.id,
            symbol=symbol,
            timeframe="manual",
            action="EXIT",
            side=close_side,
            entry_price=close_price,
            quantity=quantity,
            usdt_value=round(usdt_value, 2),
            realized_pnl=round(realized_pnl, 2),
            leverage=leverage,
            status="FILLED",
            error_message=None,
            market_type="futures",
        )
        db.add(record)
        await db.commit()

        trade_result = {
            "symbol": symbol,
            "market_type": "futures",
            "side": "LONG" if amt > 0 else "SHORT",
            "quantity": quantity,
            "close_price": close_price,
            "realized_pnl": round(realized_pnl, 2),
        }
        closed.append(trade_result)

        await ws_manager.broadcast_trade({
            "status": "CLOSED",
            "market_type": "futures",
            "account": acct.name,
            "symbol": symbol,
            "side": close_side,
            "quantity": quantity,
            "realized_pnl": round(realized_pnl, 2),
        })

        try:
            from notifications import notify_force_close
            await notify_force_close(
                acct.name, symbol, "LONG" if amt > 0 else "SHORT", round(realized_pnl, 2)
            )
        except Exception as notif_err:
            logger.warning("Telegram notification failed: %s", str(notif_err))

    if not closed:
        return {"status": "NO_POSITION", "symbol": symbol, "account": acct.name}

    return {"status": "CLOSED", "account": acct.name, "positions": closed}


async def _sell_spot_holding(db: AsyncSession, client, acct: ExchangeAccount, symbol: str):
    symbol_info = await client.get_symbol_info(symbol)
    if not symbol_info:
        raise HTTPException(404, "Spot symbol not found")

    base_asset = symbol_info["baseAsset"]
    balance = await client.get_asset_balance(asset=base_asset)
    quantity = float((balance or {}).get("free", 0) or 0)
    quantity = _round_quantity(quantity, symbol_info)

    if quantity <= 0:
        return {"status": "NO_HOLDING", "symbol": symbol, "account": acct.name}

    order = await client.create_order(
        symbol=symbol,
        side=SIDE_SELL,
        type=ORDER_TYPE_MARKET,
        quantity=quantity,
    )

    close_price = _resolve_order_price(order)
    if close_price == 0:
        ticker = await client.get_symbol_ticker(symbol=symbol)
        close_price = float(ticker["price"])
    usdt_value = close_price * quantity

    record = TradeRecord(
        account_id=acct.id,
        symbol=symbol,
        timeframe="manual",
        action="SELL",
        side=SIDE_SELL,
        entry_price=close_price,
        quantity=quantity,
        usdt_value=round(usdt_value, 2),
        realized_pnl=0,
        leverage=1,
        status="FILLED",
        error_message=None,
        market_type="spot",
    )
    db.add(record)
    await db.commit()

    await ws_manager.broadcast_trade({
        "status": "CLOSED",
        "market_type": "spot",
        "account": acct.name,
        "symbol": symbol,
        "side": SIDE_SELL,
        "quantity": quantity,
        "realized_pnl": 0,
    })

    try:
        from notifications import notify_force_close
        await notify_force_close(acct.name, symbol, "SPOT SELL", 0)
    except Exception as notif_err:
        logger.warning("Telegram notification failed: %s", str(notif_err))

    return {
        "status": "CLOSED",
        "account": acct.name,
        "positions": [{
            "symbol": symbol,
            "market_type": "spot",
            "side": "SELL",
            "quantity": quantity,
            "close_price": close_price,
            "realized_pnl": 0,
        }],
    }


def _resolve_order_price(order: dict) -> float:
    price = float(order.get("avgPrice", 0) or 0)
    if price == 0 and order.get("fills"):
        fills = order["fills"]
        total_qty = sum(float(fill["qty"]) for fill in fills)
        if total_qty > 0:
            price = sum(float(fill["price"]) * float(fill["qty"]) for fill in fills) / total_qty
    return price


def _round_quantity(quantity: float, symbol_info: dict) -> float:
    step_size = 1.0
    for item in symbol_info.get("filters", []):
        if item["filterType"] == "LOT_SIZE":
            step_size = float(item["stepSize"])
            break
    precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
    return round(quantity - (quantity % step_size), precision)
