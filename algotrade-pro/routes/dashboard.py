"""Dashboard API – aggregated Futures and Spot balance data."""

import asyncio
import logging

from binance import AsyncClient
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot_engine import create_binance_client
from database import get_db
from encryption import decrypt_secret
from models import ExchangeAccount
from schemas import AccountBalanceSnapshot, DashboardData
from websocket_manager import ws_manager

router = APIRouter(tags=["dashboard"])
logger = logging.getLogger("algotrade.dashboard")


async def _fetch_account_balance(acct: ExchangeAccount) -> AccountBalanceSnapshot:
    """Fetch balance for a single account (used in parallel gather)."""
    client = None
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        market_type = acct.market_type or "futures"
        client = await create_binance_client(acct.api_key, secret, market_type=market_type)

        if market_type == "spot":
            account = await client.get_account()
            available = 0.0
            wallet = 0.0

            for balance in account.get("balances", []):
                asset = balance["asset"]
                free = float(balance.get("free", 0) or 0)
                asset_locked = float(balance.get("locked", 0) or 0)
                total = free + asset_locked
                if total <= 0:
                    continue
                if asset == "USDT":
                    available += free
                    wallet += total
                    continue
                try:
                    ticker = await client.get_symbol_ticker(symbol=f"{asset}USDT")
                    wallet += total * float(ticker["price"])
                except Exception:
                    logger.debug("Skipping spot valuation for non-USDT asset %s", asset)

            utilization = ((wallet - available) / wallet * 100) if wallet > 0 else 0
        else:
            futures = await client.futures_account()
            wallet = float(futures.get("totalWalletBalance", 0))
            available = float(futures.get("availableBalance", 0))
            utilization = ((wallet - available) / wallet * 100) if wallet > 0 else 0

        return AccountBalanceSnapshot(
            account_id=acct.id,
            account_name=acct.name,
            market_type=market_type,
            wallet_balance=round(wallet, 2),
            available_margin=round(available, 2),
            margin_utilization=round(utilization, 2),
        )
    except Exception as e:
        logger.error("Failed to fetch balance for %s: %s", acct.name, e)
        return AccountBalanceSnapshot(
            account_id=acct.id,
            account_name=acct.name,
            market_type=acct.market_type or "futures",
            wallet_balance=0,
            available_margin=0,
            margin_utilization=0,
        )
    finally:
        if client:
            await client.close_connection()


@router.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard_data(db: AsyncSession = Depends(get_db)):
    """Fetch real-time balances from all active Binance accounts in parallel."""
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.is_active == True,  # noqa: E712
            ExchangeAccount.futures_enabled == True,
        )
    )
    accounts = result.scalars().all()

    # Fetch all account balances in parallel
    snapshots = await asyncio.gather(
        *[_fetch_account_balance(acct) for acct in accounts]
    )

    total_bal = sum(s.wallet_balance for s in snapshots)
    total_avail = sum(s.available_margin for s in snapshots)
    avg_util = (
        sum(s.margin_utilization for s in snapshots) / len(snapshots)
        if snapshots
        else 0
    )

    return DashboardData(
        total_balance=round(total_bal, 2),
        total_available_margin=round(total_avail, 2),
        avg_margin_utilization=round(avg_util, 2),
        accounts=snapshots,
    )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time dashboard updates."""
    # Must accept FIRST — calling close() before accept() causes HTTP 403
    await websocket.accept()

    # Now check auth via session cookie
    from auth import validate_session
    session_id = websocket.cookies.get("algotrade_session")
    if not validate_session(session_id):
        await websocket.close(code=4001, reason="Not authenticated")
        return

    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; client can send pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
