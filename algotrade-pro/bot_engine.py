"""Bot Engine - autonomous risk management and exchange execution.

Supports Binance Futures and Binance Spot side by side. Existing accounts
default to Futures; Spot is opt-in per account. Market-specific behavior
lives in market_adapters.MarketAdapter implementations.
"""

import asyncio
import logging
from datetime import datetime, timezone

from binance import AsyncClient
from binance.enums import SIDE_BUY, SIDE_SELL
from binance.exceptions import BinanceAPIException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import async_session
from encryption import decrypt_secret
from models import BotSettings, DailyPnl, ExchangeAccount, SymbolMapping, TradeRecord
from schemas import WebhookPayload

logger = logging.getLogger("algotrade.bot_engine")


def _market_type(account: ExchangeAccount) -> str:
    return (getattr(account, "market_type", None) or "futures").lower()


async def get_testnet_mode() -> bool:
    """Read the current testnet_mode from the database."""
    async with async_session() as db:
        result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.scalar_one_or_none()
        if bot_settings is None:
            return True
        return bot_settings.testnet_mode


async def create_binance_client(
    api_key: str,
    api_secret: str,
    testnet: bool | None = None,
    market_type: str = "futures",
) -> AsyncClient:
    """Create a Binance AsyncClient for either Futures or Spot."""
    if testnet is None:
        testnet = await get_testnet_mode()

    market = (market_type or "futures").lower()
    client = await AsyncClient.create(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        requests_params={"timeout": 15},
    )

    if market == "spot":
        client.API_URL = (
            settings.BINANCE_SPOT_TESTNET_API_URL
            if testnet
            else settings.BINANCE_SPOT_LIVE_API_URL
        ) + "/api"
        logger.info("Connected to Binance Spot %s", "TESTNET" if testnet else "LIVE")
    else:
        client.FUTURES_URL = (
            settings.BINANCE_FUTURES_TESTNET_API_URL
            if testnet
            else settings.BINANCE_FUTURES_LIVE_API_URL
        ) + "/fapi"
        logger.info("Connected to Binance Futures %s", "TESTNET" if testnet else "LIVE")

    return client


from market_adapters import (  # noqa: E402  (after create_binance_client to avoid cycle)
    MarketAdapter,
    SpotAdapter,
    create_market_adapter,
    get_balance_from_account_info,
    get_step_size,
    get_tick_size,
    quantity_from_price,
    resolve_order_price,
    round_price,
    round_quantity,
)


class BotEngine:
    """Core trading engine that processes webhook signals."""

    async def process_signal(self, payload: WebhookPayload) -> list[dict]:
        """Process a validated webhook signal end-to-end."""
        async with async_session() as db:
            bot_settings = await self._get_bot_settings(db)
            if not bot_settings.bot_active:
                logger.warning("Bot is INACTIVE - ignoring signal %s", payload.symbol)
                return [{"status": "BLOCKED", "reason": "Bot is inactive"}]

            pairs = await self._resolve_accounts(db, payload.symbol, payload.timeframe)
            if not pairs:
                logger.warning("No account mapped for %s @ %s", payload.symbol, payload.timeframe)
                return [{"status": "NO_MAPPING", "symbol": payload.symbol}]

        tasks = [
            self._execute_for_account(acct, market, payload, bot_settings)
            for (acct, market) in pairs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final = []
        for result in results:
            if isinstance(result, Exception):
                final.append({"status": "ERROR", "error": str(result)})
            else:
                final.append(result)
        return final

    async def _get_bot_settings(self, db: AsyncSession) -> BotSettings:
        result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.scalar_one_or_none()
        if bot_settings is None:
            bot_settings = BotSettings(id=1)
            db.add(bot_settings)
            await db.commit()
            await db.refresh(bot_settings)
        return bot_settings

    async def _resolve_accounts(
        self, db: AsyncSession, symbol: str, timeframe: str
    ) -> list[tuple[ExchangeAccount, str]]:
        """Return (account, market_type) pairs mapped to this symbol/timeframe.

        Mapping.market_type is the source of truth; account must have that
        market verified (futures_enabled or spot_enabled).
        """
        from sqlalchemy import and_, or_

        result = await db.execute(
            select(ExchangeAccount, SymbolMapping.market_type)
            .join(SymbolMapping, SymbolMapping.account_id == ExchangeAccount.id)
            .where(
                SymbolMapping.symbol == symbol.upper(),
                SymbolMapping.timeframe == timeframe,
                ExchangeAccount.is_active == True,  # noqa: E712
                or_(
                    and_(
                        SymbolMapping.market_type == "futures",
                        ExchangeAccount.futures_enabled == True,  # noqa: E712
                    ),
                    and_(
                        SymbolMapping.market_type == "spot",
                        ExchangeAccount.spot_enabled == True,  # noqa: E712
                    ),
                ),
            )
        )
        return [(row.ExchangeAccount, row.market_type) for row in result.all()]

    async def _execute_for_account(
        self,
        account: ExchangeAccount,
        market: str,
        payload: WebhookPayload,
        bot_settings: BotSettings,
    ) -> dict:
        async with async_session() as db:
            return await self._execute(db, account, market, payload, bot_settings)

    async def _execute(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
        market: str,
        payload: WebhookPayload,
        bot_settings: BotSettings,
    ) -> dict:
        market = (market or "futures").lower()
        blocked = await self._check_risk_limits(db, account, bot_settings)
        if blocked:
            await self._record_trade(db, account, market, payload, status="REJECTED", error=blocked)
            return {"status": "BLOCKED", "account": account.name, "reason": blocked}

        secret = decrypt_secret(account.api_secret_encrypted)
        adapter: MarketAdapter | None = None
        try:
            adapter = await create_market_adapter(
                account.api_key, secret, market_type=market
            )
            action = payload.action.upper()
            if self._is_exit_action(market, action):
                return await self._handle_exit(db, adapter, account, payload)
            return await self._handle_entry(db, adapter, account, payload, action)
        except BinanceAPIException as e:
            error_msg = f"Binance {market.title()} API error: {e.message} (code {e.code})"
            logger.error(error_msg)
            await self._record_trade(db, account, market, payload, status="ERROR", error=error_msg)
            return {
                "status": "ERROR",
                "market_type": market,
                "account": account.name,
                "error": error_msg,
            }
        except Exception as e:
            error_msg = f"Unexpected {market.title()} error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await self._record_trade(db, account, market, payload, status="ERROR", error=error_msg)
            return {
                "status": "ERROR",
                "market_type": market,
                "account": account.name,
                "error": error_msg,
            }
        finally:
            if adapter is not None:
                await adapter.close()

    @staticmethod
    def _is_exit_action(market: str, action: str) -> bool:
        if market == "spot":
            return action in ("SHORT", "EXIT", "SELL")
        return action in ("EXIT", "SELL")

    # ── Entry / Exit (market-agnostic via adapter) ────────────────────

    async def _handle_entry(
        self,
        db: AsyncSession,
        adapter: MarketAdapter,
        account: ExchangeAccount,
        payload: WebhookPayload,
        action: str,
    ) -> dict:
        warnings: list[str] = []
        wallet, available_quote = await adapter.fetch_sizing_balance()

        if account.trading_size_type == "fixed":
            risk_amount = account.trading_size_value
        else:
            risk_amount = wallet * (account.trading_size_value / 100.0)
        if adapter.market_type == "spot":
            risk_amount = min(risk_amount, available_quote)

        if action == "SHORT":
            side = SIDE_SELL
            record_action = "SHORT"
        else:
            side = SIDE_BUY
            record_action = "BUY" if adapter.market_type == "spot" else "LONG"

        symbol = payload.symbol.upper()
        symbol_info = await adapter.get_symbol_info(symbol)
        tick_size = get_tick_size(symbol_info)
        price = await adapter.get_ticker_price(symbol)
        quantity = quantity_from_price(risk_amount, price, symbol_info, symbol)

        if account.trade_mode == "single":
            block_reason = await adapter.precheck_single_mode(account, symbol, symbol_info)
            if block_reason:
                await self._record_trade(
                    db, account, adapter.market_type, payload,
                    action=record_action, status="REJECTED", error=block_reason,
                )
                return {
                    "status": "BLOCKED",
                    "market_type": adapter.market_type,
                    "account": account.name,
                    "reason": block_reason,
                }

        leverage = account.leverage if adapter.market_type == "futures" else 1
        if adapter.market_type == "futures":
            await adapter.set_leverage(symbol, leverage)

        try:
            from notifications import notify_order_placed
            await notify_order_placed(
                account.name, symbol, record_action, side, float(quantity), leverage,
            )
        except Exception:
            pass

        order = await adapter.place_market_entry(symbol, side, quantity)
        entry_price = resolve_order_price(order, payload.price)
        usdt_value = entry_price * float(quantity)

        if account.stoploss_percent and entry_price > 0:
            if side == SIDE_BUY:
                sl_price = round_price(
                    entry_price * (1 - account.stoploss_percent / 100), tick_size
                )
            else:
                sl_price = round_price(
                    entry_price * (1 + account.stoploss_percent / 100), tick_size
                )
            try:
                await adapter.place_stoploss(symbol, side, quantity, sl_price)
                logger.info(
                    "%s stoploss placed at %s for %s",
                    adapter.market_type.title(), sl_price, symbol,
                )
            except Exception as e:
                logger.warning(
                    "%s stoploss order failed: %s", adapter.market_type.title(), str(e)
                )
                warnings.append(f"Stoploss order failed: {str(e)}")

        if account.trail_callback_pct and account.trail_callback_pct > 0:
            is_testnet = await get_testnet_mode()
            supported, warning = adapter.supports_trailing(is_testnet)
            if not supported:
                if warning:
                    logger.warning("%s", warning)
                    warnings.append(warning)
            elif entry_price > 0:
                activation_price: float | None = None
                if account.trail_activation_pct and account.trail_activation_pct > 0:
                    if side == SIDE_BUY:
                        activation_price = round_price(
                            entry_price * (1 + account.trail_activation_pct / 100),
                            tick_size,
                        )
                    else:
                        activation_price = round_price(
                            entry_price * (1 - account.trail_activation_pct / 100),
                            tick_size,
                        )
                try:
                    await adapter.place_trailing_stop(
                        symbol, side, quantity,
                        account.trail_callback_pct, activation_price,
                    )
                    logger.info(
                        "%s trailing stop placed for %s",
                        adapter.market_type.title(), symbol,
                    )
                except Exception as e:
                    logger.error(
                        "%s trailing stop order failed: %s",
                        adapter.market_type.title(), str(e), exc_info=True,
                    )
                    warnings.append(f"Trailing stop order failed: {str(e)}")

        await self._record_trade(
            db,
            account,
            adapter.market_type,
            payload,
            action=record_action,
            side=side,
            entry_price=entry_price,
            quantity=float(quantity),
            usdt_value=usdt_value,
            leverage=leverage,
            status="FILLED",
        )

        result = {
            "status": "FILLED",
            "market_type": adapter.market_type,
            "account": account.name,
            "symbol": payload.symbol,
            "side": side,
            "action": record_action,
            "quantity": float(quantity),
            "entry_price": entry_price,
            "usdt_value": usdt_value,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    async def _handle_exit(
        self,
        db: AsyncSession,
        adapter: MarketAdapter,
        account: ExchangeAccount,
        payload: WebhookPayload,
    ) -> dict:
        symbol = payload.symbol.upper()
        closed = await adapter.close_position(symbol, payload.price)
        if not closed:
            no_status = "NO_POSITION" if adapter.market_type == "futures" else "NO_HOLDING"
            return {
                "status": no_status,
                "market_type": adapter.market_type,
                "account": account.name,
                "symbol": payload.symbol,
            }

        primary = closed[0]
        total_qty = sum(c["quantity"] for c in closed)
        realized_pnl_total = sum(c["realized_pnl"] for c in closed)
        exit_price = primary["close_price"]
        usdt_value = exit_price * total_qty
        side = primary["close_side"]
        leverage = primary["leverage"]
        action = primary["action"]
        if adapter.market_type == "spot" and payload.action.upper() == "EXIT":
            action = "EXIT"

        await self._record_trade(
            db,
            account,
            adapter.market_type,
            payload,
            action=action,
            side=side,
            entry_price=exit_price,
            quantity=total_qty,
            usdt_value=round(usdt_value, 2),
            realized_pnl=realized_pnl_total,
            leverage=leverage,
            status="FILLED",
        )

        if adapter.market_type == "futures" and realized_pnl_total != 0:
            await self._update_daily_pnl(db, account.id, realized_pnl_total)

        return {
            "status": "CLOSED",
            "market_type": adapter.market_type,
            "account": account.name,
            "symbol": payload.symbol,
            "realized_pnl": realized_pnl_total,
        }

    # ── Risk limits & ledger ──────────────────────────────────────────

    async def _check_risk_limits(
        self, db: AsyncSession, account: ExchangeAccount, bot_settings: BotSettings
    ) -> str | None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await db.execute(
            select(DailyPnl).where(
                DailyPnl.account_id == account.id,
                DailyPnl.date == today,
            )
        )
        daily = result.scalar_one_or_none()
        if daily and daily.realized_pnl <= -bot_settings.daily_loss_limit:
            return (
                f"Daily loss limit reached: {daily.realized_pnl:.2f} USDT "
                f"(limit: -{bot_settings.daily_loss_limit:.2f})"
            )

        result = await db.execute(
            select(func.sum(TradeRecord.realized_pnl)).where(
                TradeRecord.account_id == account.id
            )
        )
        total_pnl = result.scalar() or 0.0
        if total_pnl <= -bot_settings.max_drawdown:
            return (
                f"Max drawdown reached: {total_pnl:.2f} USDT "
                f"(limit: -{bot_settings.max_drawdown:.2f})"
            )
        return None

    async def _record_trade(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
        market: str,
        payload: WebhookPayload,
        action: str | None = None,
        side: str = "",
        entry_price: float = 0.0,
        quantity: float = 0.0,
        usdt_value: float = 0.0,
        realized_pnl: float = 0.0,
        leverage: int = 0,
        status: str = "FILLED",
        error: str | None = None,
    ):
        record = TradeRecord(
            account_id=account.id,
            symbol=payload.symbol.upper(),
            timeframe=payload.timeframe,
            action=action or payload.action.upper(),
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            usdt_value=usdt_value,
            realized_pnl=realized_pnl,
            leverage=leverage,
            status=status,
            error_message=error,
            market_type=(market or "futures").lower(),
        )
        db.add(record)
        await db.commit()

    async def _update_daily_pnl(self, db: AsyncSession, account_id: int, pnl: float):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await db.execute(
            select(DailyPnl).where(
                DailyPnl.account_id == account_id,
                DailyPnl.date == today,
            )
        )
        daily = result.scalar_one_or_none()
        if daily:
            daily.realized_pnl += pnl
            daily.trade_count += 1
        else:
            daily = DailyPnl(
                account_id=account_id, date=today, realized_pnl=pnl, trade_count=1
            )
            db.add(daily)
        await db.commit()

    # ── Thin proxies preserved for tests & legacy callers ─────────────

    def _get_tick_size(self, symbol_info: dict) -> float:
        return get_tick_size(symbol_info)

    def _get_step_size(self, symbol_info: dict) -> float:
        return get_step_size(symbol_info)

    def _round_price(self, price: float, tick_size: float) -> float:
        return round_price(price, tick_size)

    def _round_quantity(self, quantity: float, symbol_info: dict) -> float:
        return round_quantity(quantity, symbol_info)

    def _quantity_from_price(
        self, risk_amount: float, price: float, symbol_info: dict, symbol: str
    ) -> float:
        return quantity_from_price(risk_amount, price, symbol_info, symbol)

    def _resolve_order_price(
        self, order: dict, fallback_price: float | None = None
    ) -> float:
        return resolve_order_price(order, fallback_price)

    def _get_balance_from_account_info(self, account_info: dict, asset: str) -> float:
        return get_balance_from_account_info(account_info, asset)

    async def _estimate_spot_equity(
        self, client: AsyncClient, account_info: dict
    ) -> float:
        return await SpotAdapter.estimate_equity(client, account_info)


bot_engine = BotEngine()
