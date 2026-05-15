"""Exchange account CRUD endpoints."""

import logging

from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot_engine import create_binance_client
from database import get_db
from encryption import decrypt_secret, encrypt_secret
from models import BotSettings, ExchangeAccount, SymbolMapping
from routes.webhook import normalize_timeframe
from websocket_manager import ws_manager
from schemas import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    SymbolMappingCreate,
    SymbolMappingResponse,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
logger = logging.getLogger("algotrade.accounts")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _to_response(acct: ExchangeAccount) -> AccountResponse:
    return AccountResponse(
        id=acct.id,
        name=acct.name,
        api_key_preview="…" + acct.api_key[-4:],
        is_active=acct.is_active,
        futures_enabled=acct.futures_enabled,
        trading_size_type=acct.trading_size_type,
        trading_size_value=acct.trading_size_value,
        leverage=acct.leverage,
        stoploss_percent=acct.stoploss_percent,
        trail_activation_pct=acct.trail_activation_pct,
        trail_callback_pct=acct.trail_callback_pct,
        trade_mode=acct.trade_mode,
        created_at=acct.created_at,
        updated_at=acct.updated_at,
    )


async def _get_defaults(db: AsyncSession) -> BotSettings:
    """Load global BotSettings to use as defaults for new accounts."""
    result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = BotSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


async def _verify_binance_keys(api_key: str, api_secret: str) -> bool:
    """Ping Binance to check if Futures trading is enabled.

    On testnet, the permissions endpoint doesn't exist and many endpoints
    behave differently. Testnet accounts always have futures enabled,
    so we just verify the keys are valid by making any authenticated call.
    """
    from bot_engine import get_testnet_mode

    client = None
    is_testnet = await get_testnet_mode()

    try:
        client = await create_binance_client(api_key, api_secret, testnet=is_testnet)

        if is_testnet:
            # Testnet: just check if keys work at all — futures is always on
            try:
                await client.futures_account_balance()
                logger.info("Testnet keys verified via futures_account_balance")
                return True
            except Exception:
                try:
                    await client.futures_account()
                    logger.info("Testnet keys verified via futures_account")
                    return True
                except Exception:
                    await client.ping()
                    logger.info("Testnet keys verified via ping (assuming futures)")
                    return True
        else:
            perms = await client.get_account_api_permissions()
            return perms.get("enableFutures", False)

    except BinanceAPIException as e:
        logger.warning("Binance key verification failed: %s", e.message)
        return False
    except Exception as e:
        logger.warning("Binance key verification error: %s", str(e))
        return False
    finally:
        if client:
            await client.close_connection()


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ExchangeAccount).order_by(ExchangeAccount.id))
    return [_to_response(a) for a in result.scalars().all()]


@router.post("/", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    # Load defaults from BotSettings
    defaults = await _get_defaults(db)

    # Verify keys against Binance
    futures_ok = await _verify_binance_keys(body.api_key, body.api_secret)

    acct = ExchangeAccount(
        name=body.name,
        api_key=body.api_key,
        api_secret_encrypted=encrypt_secret(body.api_secret),
        futures_enabled=futures_ok,
        # Use provided values or fall back to BotSettings defaults
        trading_size_type=body.trading_size_type or defaults.default_trading_size_type,
        trading_size_value=body.trading_size_value if body.trading_size_value is not None else defaults.risk_per_trade,
        leverage=body.leverage if body.leverage is not None else defaults.leverage_override,
        stoploss_percent=body.stoploss_percent if body.stoploss_percent is not None else defaults.default_stoploss_percent,
        trail_activation_pct=body.trail_activation_pct if body.trail_activation_pct is not None else defaults.default_trail_activation_pct,
        trail_callback_pct=body.trail_callback_pct if body.trail_callback_pct is not None else defaults.default_trail_callback_pct,
        trade_mode=body.trade_mode or defaults.default_trade_mode,
    )
    db.add(acct)
    await db.commit()
    await db.refresh(acct)

    logger.info(
        "Account '%s' created (futures=%s, size=%s %s, lev=%sx, sl=%s%%)",
        acct.name, acct.futures_enabled,
        acct.trading_size_value, acct.trading_size_type,
        acct.leverage, acct.stoploss_percent,
    )

    # Auto-sync trade history from Binance in background
    if futures_ok:
        bg.add_task(_auto_sync_account, acct.id)

    await ws_manager.broadcast("account_added", {"account_id": acct.id, "name": acct.name})

    return _to_response(acct)


async def _auto_sync_account(account_id: int):
    """Background task: sync trade history for a newly added account."""
    from database import async_session
    from routes.trades import sync_account_trades

    try:
        async with async_session() as db:
            result = await db.execute(
                select(ExchangeAccount).where(ExchangeAccount.id == account_id)
            )
            acct = result.scalar_one_or_none()
            if acct:
                count = await sync_account_trades(acct, db)
                logger.info("Auto-synced %d trades for new account '%s'", count, acct.name)
    except Exception as e:
        logger.warning("Auto-sync for new account failed: %s", str(e))


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ExchangeAccount).where(ExchangeAccount.id == account_id)
    )
    acct = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(404, "Account not found")
    return _to_response(acct)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: int, body: AccountUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ExchangeAccount).where(ExchangeAccount.id == account_id)
    )
    acct = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(404, "Account not found")

    if body.name is not None:
        acct.name = body.name
    if body.api_key is not None:
        acct.api_key = body.api_key
    if body.api_secret is not None:
        acct.api_secret_encrypted = encrypt_secret(body.api_secret)
        secret = body.api_secret
        key = body.api_key or acct.api_key
        acct.futures_enabled = await _verify_binance_keys(key, secret)
    if body.is_active is not None:
        acct.is_active = body.is_active
    if body.trading_size_type is not None:
        acct.trading_size_type = body.trading_size_type
    if body.trading_size_value is not None:
        acct.trading_size_value = body.trading_size_value
    if body.leverage is not None:
        acct.leverage = body.leverage
    if body.stoploss_percent is not None:
        acct.stoploss_percent = body.stoploss_percent
    if body.trail_activation_pct is not None:
        acct.trail_activation_pct = body.trail_activation_pct
    if body.trail_callback_pct is not None:
        acct.trail_callback_pct = body.trail_callback_pct
    if body.trade_mode is not None:
        acct.trade_mode = body.trade_mode

    await db.commit()
    await db.refresh(acct)
    return _to_response(acct)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ExchangeAccount).where(ExchangeAccount.id == account_id)
    )
    acct = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(404, "Account not found")
    await db.delete(acct)
    await db.commit()


# ── Symbol Mappings ──────────────────────────────────────────────────────────

@router.get("/{account_id}/mappings", response_model=list[SymbolMappingResponse])
async def list_mappings(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SymbolMapping).where(SymbolMapping.account_id == account_id)
    )
    return list(result.scalars().all())


@router.post("/mappings", response_model=SymbolMappingResponse, status_code=201)
async def create_mapping(
    body: SymbolMappingCreate, db: AsyncSession = Depends(get_db)
):
    symbol = body.symbol.strip().upper()

    # Validate symbol exists on Binance Futures
    try:
        import aiohttp
        from bot_engine import get_testnet_mode
        is_testnet = await get_testnet_mode()
        base_url = "https://testnet.binancefuture.com" if is_testnet else "https://fapi.binance.com"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    info = await resp.json()
                    valid_symbols = {s["symbol"] for s in info.get("symbols", [])}
                    if symbol not in valid_symbols:
                        raise HTTPException(
                            400,
                            f"Symbol '{symbol}' not found on Binance Futures. "
                            f"Check spelling (e.g. BTCUSDT, ETHUSDT)."
                        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Symbol validation skipped (could not connect): %s", str(e))

    # Normalize timeframe: 5m → 5, 1h → 60, etc.
    normalized_tf = normalize_timeframe(body.timeframe)

    mapping = SymbolMapping(
        symbol=symbol,
        timeframe=normalized_tf,
        account_id=body.account_id,
    )
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)
    return mapping


@router.delete("/mappings/{mapping_id}", status_code=204)
async def delete_mapping(mapping_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SymbolMapping).where(SymbolMapping.id == mapping_id)
    )
    mapping = result.scalar_one_or_none()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    await db.delete(mapping)
    await db.commit()
