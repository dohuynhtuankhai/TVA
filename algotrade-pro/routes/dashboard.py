"""Dashboard API – aggregated Futures and Spot balance data."""

import asyncio
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from encryption import decrypt_secret
from market_adapters import create_market_adapter, normalize_market_type
from models import ExchangeAccount
from schemas import AccountBalanceSnapshot, DashboardData
from websocket_manager import ws_manager

router = APIRouter(tags=["dashboard"])
logger = logging.getLogger("algopro.dashboard")


async def _fetch_account_market_balance(
    acct: ExchangeAccount, market_type: str
) -> AccountBalanceSnapshot:
    """Fetch balance for a single (account, market) pair."""
    adapter = None
    try:
        secret = decrypt_secret(acct.api_secret_encrypted)
        adapter = await create_market_adapter(
            acct.api_key, secret, market_type=market_type
        )
        wallet, available, utilization = await adapter.fetch_balance()
        return AccountBalanceSnapshot(
            account_id=acct.id,
            account_name=acct.name,
            market_type=market_type,
            wallet_balance=round(wallet, 2),
            available_margin=round(available, 2),
            margin_utilization=round(utilization, 2),
        )
    except Exception as e:
        logger.error("Failed to fetch %s balance for %s: %s", market_type, acct.name, e)
        return AccountBalanceSnapshot(
            account_id=acct.id,
            account_name=acct.name,
            market_type=market_type,
            wallet_balance=0,
            available_margin=0,
            margin_utilization=0,
        )
    finally:
        if adapter is not None:
            await adapter.close()


def _account_market_pairs(acct: ExchangeAccount) -> list[str]:
    pairs: list[str] = []
    if acct.futures_enabled:
        pairs.append("futures")
    if acct.spot_enabled:
        pairs.append("spot")
    return pairs


@router.get("/api/dashboard", response_model=DashboardData)
async def get_dashboard_data(db: AsyncSession = Depends(get_db)):
    """Fetch real-time balances from all active Binance accounts in parallel."""
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.is_active == True,  # noqa: E712
        )
    )
    accounts = result.scalars().all()

    # Fetch every (account, enabled market) pair in parallel
    tasks = [
        _fetch_account_market_balance(acct, market)
        for acct in accounts
        for market in _account_market_pairs(acct)
    ]
    snapshots = await asyncio.gather(*tasks) if tasks else []

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
    session_id = websocket.cookies.get("algopro_session")
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
