"""Bot Engine – the autonomous risk manager and execution layer.

Responsibilities:
1. Risk Evaluation   – check bot_active, daily_loss_limit, max_drawdown
2. Position Sizing   – dynamic % of real-time account balance
3. Leverage Mgmt     – override exchange default before order
4. Execution         – construct & send Binance Futures orders
5. Ledger Recording  – log trade details into SQLite
"""

import asyncio
import logging
from datetime import datetime, timezone

from binance import AsyncClient
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import async_session
from encryption import decrypt_secret
from models import BotSettings, DailyPnl, ExchangeAccount, SymbolMapping, TradeRecord
from schemas import WebhookPayload

logger = logging.getLogger("algotrade.bot_engine")


async def get_testnet_mode() -> bool:
    """Read the current testnet_mode from the database."""
    async with async_session() as db:
        result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
        bot_settings = result.scalar_one_or_none()
        if bot_settings is None:
            return True  # Default to testnet for safety
        return bot_settings.testnet_mode


async def create_binance_client(api_key: str, api_secret: str, testnet: bool | None = None) -> AsyncClient:
    """Create a Binance AsyncClient pointing at testnet or live.

    If testnet is not explicitly passed, reads the setting from the database.
    """
    if testnet is None:
        testnet = await get_testnet_mode()

    client = await AsyncClient.create(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        requests_params={"timeout": 15},
    )
    if testnet:
        client.FUTURES_URL = settings.BINANCE_TESTNET_API_URL + "/fapi"
        logger.info("Connected to Binance TESTNET")
    else:
        logger.info("Connected to Binance LIVE")
    return client


class BotEngine:
    """Core trading engine that processes webhook signals."""

    # ── Public entry point ───────────────────────────────────────────────

    async def process_signal(self, payload: WebhookPayload) -> list[dict]:
        """Process a validated webhook signal end-to-end.

        Returns a list of result dicts (one per target account).
        """
        async with async_session() as db:
            # 1. Load global settings
            bot_settings = await self._get_bot_settings(db)
            if not bot_settings.bot_active:
                logger.warning("Bot is INACTIVE – ignoring signal %s", payload.symbol)
                return [{"status": "BLOCKED", "reason": "Bot is inactive"}]

            # 2. Find target account(s) for this symbol + timeframe
            accounts = await self._resolve_accounts(db, payload.symbol, payload.timeframe)
            if not accounts:
                logger.warning(
                    "No account mapped for %s @ %s", payload.symbol, payload.timeframe
                )
                return [{"status": "NO_MAPPING", "symbol": payload.symbol}]

        # 3. Execute against each mapped account. Each task owns its DB session;
        # AsyncSession instances are not safe to share across concurrent tasks.
        tasks = [
            self._execute_for_account(acct, payload, bot_settings)
            for acct in accounts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to error dicts
        final = []
        for r in results:
            if isinstance(r, Exception):
                final.append({"status": "ERROR", "error": str(r)})
            else:
                final.append(r)

        return final

    # ── Private helpers ──────────────────────────────────────────────────

    async def _get_bot_settings(self, db: AsyncSession) -> BotSettings:
        result = await db.execute(select(BotSettings).where(BotSettings.id == 1))
        settings = result.scalar_one_or_none()
        if settings is None:
            settings = BotSettings(id=1)
            db.add(settings)
            await db.commit()
            await db.refresh(settings)
        return settings

    async def _resolve_accounts(
        self, db: AsyncSession, symbol: str, timeframe: str
    ) -> list[ExchangeAccount]:
        """Return active accounts mapped to this (symbol, timeframe)."""
        result = await db.execute(
            select(ExchangeAccount)
            .join(SymbolMapping)
            .where(
                SymbolMapping.symbol == symbol.upper(),
                SymbolMapping.timeframe == timeframe,
                ExchangeAccount.is_active == True,  # noqa: E712
                ExchangeAccount.futures_enabled == True,
            )
        )
        return list(result.scalars().all())

    async def _execute_for_account(
        self,
        account: ExchangeAccount,
        payload: WebhookPayload,
        bot_settings: BotSettings,
    ) -> dict:
        """Run the full trade pipeline for a single account."""
        client = None
        async with async_session() as db:
            return await self._execute_for_account_with_session(
                db, account, payload, bot_settings, client
            )

    async def _execute_for_account_with_session(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
        payload: WebhookPayload,
        bot_settings: BotSettings,
        client: AsyncClient | None,
    ) -> dict:
        """Run the trade pipeline using a task-owned DB session."""
        try:
            warnings: list[str] = []

            # ── Risk gate ────────────────────────────────────────────
            blocked_reason = await self._check_risk_limits(
                db, account, bot_settings
            )
            if blocked_reason:
                await self._record_trade(
                    db, account, payload, status="REJECTED", error=blocked_reason
                )
                return {"status": "BLOCKED", "account": account.name, "reason": blocked_reason}

            # ── Connect to Binance ───────────────────────────────────
            secret = decrypt_secret(account.api_secret_encrypted)
            client = await create_binance_client(account.api_key, secret)

            # ── Fetch real-time balance ──────────────────────────────
            futures_account = await client.futures_account()
            wallet_balance = float(futures_account["totalWalletBalance"])

            # ── Dynamic position sizing (per-account) ────────────────
            if account.trading_size_type == "fixed":
                risk_amount = account.trading_size_value
            else:  # percent
                risk_amount = wallet_balance * (account.trading_size_value / 100.0)

            # ── Determine side ───────────────────────────────────────
            action = payload.action.upper()
            if action in ("ENTRY", "LONG"):
                side = SIDE_BUY
            elif action == "SHORT":
                side = SIDE_SELL
            elif action == "EXIT":
                return await self._handle_exit(
                    db, client, account, payload, bot_settings
                )
            else:
                return {"status": "ERROR", "error": f"Unknown action: {action}"}

            # ── Single trade mode: close existing before new entry ────
            if account.trade_mode == "single":
                await self._close_existing_position(
                    client, account, payload.symbol.upper()
                )

            # ── Set leverage (per-account) ────────────────────────────
            await client.futures_change_leverage(
                symbol=payload.symbol.upper(),
                leverage=account.leverage,
            )

            # ── Calculate quantity ────────────────────────────────────
            symbol_info = await self._get_symbol_info(client, payload.symbol.upper())
            tick_size = self._get_tick_size(symbol_info)
            quantity = await self._calculate_quantity(
                client, payload.symbol.upper(), risk_amount, symbol_info
            )

            # ── Place MARKET order ───────────────────────────────────
            # Notify order placed
            try:
                from notifications import notify_order_placed
                await notify_order_placed(
                    account.name, payload.symbol.upper(), action,
                    side, float(quantity), account.leverage,
                )
            except Exception:
                pass  # Never block execution for notifications

            order = await client.futures_create_order(
                symbol=payload.symbol.upper(),
                side=side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=quantity,
            )

            # Get price: try avgPrice → fills → webhook price
            entry_price = float(order.get("avgPrice", 0) or 0)
            if entry_price == 0 and order.get("fills"):
                fills = order["fills"]
                total_qty = sum(float(f["qty"]) for f in fills)
                if total_qty > 0:
                    entry_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
            if entry_price == 0 and payload.price:
                entry_price = payload.price
            usdt_value = entry_price * float(quantity)

            # ── Place stoploss if configured ─────────────────────────
            if account.stoploss_percent and entry_price > 0:
                sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
                if side == SIDE_BUY:
                    sl_price = self._round_price(entry_price * (1 - account.stoploss_percent / 100), tick_size)
                else:
                    sl_price = self._round_price(entry_price * (1 + account.stoploss_percent / 100), tick_size)
                try:
                    await client.futures_create_order(
                        symbol=payload.symbol.upper(),
                        side=sl_side,
                        type="STOP_MARKET",
                        stopPrice=str(sl_price),
                        closePosition=True,
                    )
                    logger.info("Stoploss placed at %s for %s", sl_price, payload.symbol)
                except Exception as e:
                    logger.warning("Stoploss order failed: %s", str(e))
                    warnings.append(f"Stoploss order failed: {str(e)}")

            # ── Place trailing stop if configured ────────────────────
            logger.info(
                "Trailing stop check: callback=%s, activation=%s, entry_price=%s",
                account.trail_callback_pct, account.trail_activation_pct, entry_price,
            )
            is_testnet = await get_testnet_mode()
            if account.trail_callback_pct and account.trail_callback_pct > 0 and entry_price > 0:
                if is_testnet:
                    warning = (
                        "Trailing stop skipped: Binance Testnet does not support "
                        "TRAILING_STOP_MARKET orders"
                    )
                    logger.warning("%s. This will work on live trading.", warning)
                    warnings.append(warning)
                else:
                    trail_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
                    try:
                        trail_params = {
                            "symbol": payload.symbol.upper(),
                            "side": trail_side,
                            "type": "TRAILING_STOP_MARKET",
                            "callbackRate": str(round(account.trail_callback_pct, 1)),
                            "quantity": str(quantity),
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        }
                        if account.trail_activation_pct and account.trail_activation_pct > 0:
                            if side == SIDE_BUY:
                                act_price = self._round_price(entry_price * (1 + account.trail_activation_pct / 100), tick_size)
                            else:
                                act_price = self._round_price(entry_price * (1 - account.trail_activation_pct / 100), tick_size)
                            trail_params["activationPrice"] = str(act_price)

                        logger.info("Placing trailing stop with params: %s", trail_params)
                        await client.futures_create_order(**trail_params)

                        logger.info(
                            "Trailing stop PLACED: callback=%.1f%%, activation=%s for %s",
                            account.trail_callback_pct,
                            trail_params.get("activationPrice", "immediate"),
                            payload.symbol,
                        )
                    except Exception as e:
                        logger.error("Trailing stop order FAILED: %s", str(e), exc_info=True)
                        warnings.append(f"Trailing stop order failed: {str(e)}")
            else:
                logger.info("Trailing stop skipped (not configured or entry_price=0)")

            # ── Record to ledger ─────────────────────────────────────
            await self._record_trade(
                db,
                account,
                payload,
                side=side,
                entry_price=entry_price,
                quantity=float(quantity),
                usdt_value=usdt_value,
                leverage=account.leverage,
                status="FILLED",
            )

            logger.info(
                "FILLED %s %s %s qty=%s @ %s for account %s",
                side, payload.symbol, action, quantity, entry_price, account.name,
            )

            result = {
                "status": "FILLED",
                "account": account.name,
                "symbol": payload.symbol,
                "side": side,
                "quantity": float(quantity),
                "entry_price": entry_price,
                "usdt_value": usdt_value,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except BinanceAPIException as e:
            error_msg = f"Binance API error: {e.message} (code {e.code})"
            logger.error(error_msg)
            await self._record_trade(
                db, account, payload, status="ERROR", error=error_msg
            )
            return {"status": "ERROR", "account": account.name, "error": error_msg}

        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await self._record_trade(
                db, account, payload, status="ERROR", error=error_msg
            )
            return {"status": "ERROR", "account": account.name, "error": error_msg}

        finally:
            if client:
                await client.close_connection()

    async def _close_existing_position(
        self,
        client: AsyncClient,
        account: ExchangeAccount,
        symbol: str,
    ):
        """Close any open position on this symbol (used by single-trade mode)."""
        try:
            positions = await client.futures_position_information(symbol=symbol)
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    close_side = SIDE_SELL if amt > 0 else SIDE_BUY
                    await client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type=FUTURE_ORDER_TYPE_MARKET,
                        quantity=abs(amt),
                        reduceOnly=True,
                    )
                    logger.info(
                        "Single-trade mode: closed existing %s position (%.4f) on %s for %s",
                        "LONG" if amt > 0 else "SHORT", abs(amt), symbol, account.name,
                    )
        except Exception as e:
            logger.warning("Failed to close existing position on %s: %s", symbol, str(e))

    async def _handle_exit(
        self,
        db: AsyncSession,
        client: AsyncClient,
        account: ExchangeAccount,
        payload: WebhookPayload,
        bot_settings: BotSettings,
    ) -> dict:
        """Close an open position for the given symbol."""
        try:
            # Get current position
            positions = await client.futures_position_information(
                symbol=payload.symbol.upper()
            )
            position = None
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    position = p
                    break

            if not position:
                return {
                    "status": "NO_POSITION",
                    "account": account.name,
                    "symbol": payload.symbol,
                }

            pos_amt = float(position["positionAmt"])
            side = SIDE_SELL if pos_amt > 0 else SIDE_BUY
            quantity = abs(pos_amt)

            order = await client.futures_create_order(
                symbol=payload.symbol.upper(),
                side=side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=quantity,
                reduceOnly=True,
            )

            realized_pnl = float(position.get("unRealizedProfit", 0))
            exit_entry_price = float(position.get("entryPrice", 0))

            # Get exit price from order fills or webhook
            exit_price = float(order.get("avgPrice", 0) or 0)
            if exit_price == 0 and order.get("fills"):
                fills = order["fills"]
                total_qty = sum(float(f["qty"]) for f in fills)
                if total_qty > 0:
                    exit_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
            if exit_price == 0 and payload.price:
                exit_price = payload.price
            if exit_price == 0:
                exit_price = float(position.get("markPrice", 0))

            usdt_value = exit_price * quantity

            await self._record_trade(
                db,
                account,
                payload,
                side=side,
                entry_price=exit_price,
                quantity=quantity,
                usdt_value=round(usdt_value, 2),
                realized_pnl=realized_pnl,
                leverage=account.leverage,
                status="FILLED",
            )

            # Update daily PnL tracker
            await self._update_daily_pnl(db, account.id, realized_pnl)

            return {
                "status": "CLOSED",
                "account": account.name,
                "symbol": payload.symbol,
                "realized_pnl": realized_pnl,
            }

        except BinanceAPIException as e:
            error_msg = f"Exit error: {e.message}"
            logger.error(error_msg)
            await self._record_trade(
                db, account, payload, status="ERROR", error=error_msg
            )
            return {"status": "ERROR", "account": account.name, "error": error_msg}

    async def _check_risk_limits(
        self, db: AsyncSession, account: ExchangeAccount, bot_settings: BotSettings
    ) -> str | None:
        """Return a reason string if the trade should be blocked, else None."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        result = await db.execute(
            select(DailyPnl).where(
                DailyPnl.account_id == account.id,
                DailyPnl.date == today,
            )
        )
        daily = result.scalar_one_or_none()

        if daily:
            if daily.realized_pnl <= -bot_settings.daily_loss_limit:
                return (
                    f"Daily loss limit reached: {daily.realized_pnl:.2f} USDT "
                    f"(limit: -{bot_settings.daily_loss_limit:.2f})"
                )

        # Check max drawdown (cumulative all-time negative PnL)
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

    async def _get_symbol_info(self, client: AsyncClient, symbol: str) -> dict:
        """Fetch and cache symbol info including tick size and step size."""
        info = await client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        raise ValueError(f"Symbol {symbol} not found on Binance Futures")

    def _get_tick_size(self, symbol_info: dict) -> float:
        """Extract tick size (price precision) from symbol filters."""
        for f in symbol_info["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                return float(f["tickSize"])
        return 0.01  # safe fallback

    def _round_price(self, price: float, tick_size: float) -> float:
        """Round a price to the nearest valid tick size."""
        if tick_size <= 0:
            return round(price, 2)
        precision = len(str(tick_size).rstrip("0").split(".")[-1]) if "." in str(tick_size) else 0
        return round(round(price / tick_size) * tick_size, precision)

    async def _calculate_quantity(
        self, client: AsyncClient, symbol: str, risk_amount: float, symbol_info: dict = None
    ) -> float:
        """Convert a USDT risk amount into a valid order quantity for the symbol."""
        # Get current price
        ticker = await client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])

        # Get symbol info for lot size / precision
        if not symbol_info:
            symbol_info = await self._get_symbol_info(client, symbol)

        # Find step size from LOT_SIZE filter
        step_size = 1.0
        for f in symbol_info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                break

        raw_qty = risk_amount / price
        # Round down to valid step size
        precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
        quantity = round(raw_qty - (raw_qty % step_size), precision)

        if quantity <= 0:
            raise ValueError(
                f"Calculated quantity is 0 for {symbol} "
                f"(risk={risk_amount:.2f}, price={price:.2f})"
            )

        return quantity

    async def _record_trade(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
        payload: WebhookPayload,
        side: str = "",
        entry_price: float = 0.0,
        quantity: float = 0.0,
        usdt_value: float = 0.0,
        realized_pnl: float = 0.0,
        leverage: int = 0,
        status: str = "FILLED",
        error: str | None = None,
    ):
        """Write a trade record to the ledger."""
        record = TradeRecord(
            account_id=account.id,
            symbol=payload.symbol.upper(),
            timeframe=payload.timeframe,
            action=payload.action.upper(),
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            usdt_value=usdt_value,
            realized_pnl=realized_pnl,
            leverage=leverage,
            status=status,
            error_message=error,
        )
        db.add(record)
        await db.commit()

    async def _update_daily_pnl(
        self, db: AsyncSession, account_id: int, pnl: float
    ):
        """Update the daily PnL tracker."""
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


# Singleton
bot_engine = BotEngine()
