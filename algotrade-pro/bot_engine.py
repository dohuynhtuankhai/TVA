"""Bot Engine - autonomous risk management and exchange execution.

Supports Binance Futures and Binance Spot side by side. Existing accounts
default to Futures; Spot is opt-in per account.
"""

import asyncio
import logging
from datetime import datetime, timezone

from binance import AsyncClient
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_MARKET
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


class BotEngine:
    """Core trading engine that processes webhook signals."""

    async def process_signal(self, payload: WebhookPayload) -> list[dict]:
        """Process a validated webhook signal end-to-end."""
        async with async_session() as db:
            bot_settings = await self._get_bot_settings(db)
            if not bot_settings.bot_active:
                logger.warning("Bot is INACTIVE - ignoring signal %s", payload.symbol)
                return [{"status": "BLOCKED", "reason": "Bot is inactive"}]

            accounts = await self._resolve_accounts(db, payload.symbol, payload.timeframe)
            if not accounts:
                logger.warning("No account mapped for %s @ %s", payload.symbol, payload.timeframe)
                return [{"status": "NO_MAPPING", "symbol": payload.symbol}]

        tasks = [self._execute_for_account(acct, payload, bot_settings) for acct in accounts]
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
    ) -> list[ExchangeAccount]:
        """Return active, enabled accounts mapped to this symbol/timeframe."""
        result = await db.execute(
            select(ExchangeAccount)
            .join(SymbolMapping)
            .where(
                SymbolMapping.symbol == symbol.upper(),
                SymbolMapping.timeframe == timeframe,
                SymbolMapping.market_type == ExchangeAccount.market_type,
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
        async with async_session() as db:
            if _market_type(account) == "spot":
                return await self._execute_spot_for_account(db, account, payload, bot_settings)
            return await self._execute_futures_for_account(db, account, payload, bot_settings)

    # ── Futures execution ─────────────────────────────────────────────

    async def _execute_futures_for_account(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
        payload: WebhookPayload,
        bot_settings: BotSettings,
    ) -> dict:
        client = None
        try:
            warnings: list[str] = []
            blocked_reason = await self._check_risk_limits(db, account, bot_settings)
            if blocked_reason:
                await self._record_trade(
                    db, account, payload, status="REJECTED", error=blocked_reason
                )
                return {"status": "BLOCKED", "account": account.name, "reason": blocked_reason}

            secret = decrypt_secret(account.api_secret_encrypted)
            client = await create_binance_client(
                account.api_key, secret, market_type="futures"
            )

            futures_account = await client.futures_account()
            wallet_balance = float(futures_account["totalWalletBalance"])

            if account.trading_size_type == "fixed":
                risk_amount = account.trading_size_value
            else:
                risk_amount = wallet_balance * (account.trading_size_value / 100.0)

            action = payload.action.upper()
            if action in ("ENTRY", "LONG", "BUY"):
                side = SIDE_BUY
                record_action = "LONG"
            elif action == "SHORT":
                side = SIDE_SELL
                record_action = "SHORT"
            elif action in ("EXIT", "SELL"):
                return await self._handle_futures_exit(db, client, account, payload)
            else:
                return {"status": "ERROR", "error": f"Unknown action: {action}"}

            if account.trade_mode == "single":
                await self._close_existing_futures_position(
                    client, account, payload.symbol.upper()
                )

            await client.futures_change_leverage(
                symbol=payload.symbol.upper(),
                leverage=account.leverage,
            )

            symbol_info = await self._get_futures_symbol_info(client, payload.symbol.upper())
            tick_size = self._get_tick_size(symbol_info)
            quantity = await self._calculate_futures_quantity(
                client, payload.symbol.upper(), risk_amount, symbol_info
            )

            try:
                from notifications import notify_order_placed

                await notify_order_placed(
                    account.name, payload.symbol.upper(), record_action,
                    side, float(quantity), account.leverage,
                )
            except Exception:
                pass

            order = await client.futures_create_order(
                symbol=payload.symbol.upper(),
                side=side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=quantity,
            )

            entry_price = self._resolve_order_price(order, payload.price)
            usdt_value = entry_price * float(quantity)

            if account.stoploss_percent and entry_price > 0:
                sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
                if side == SIDE_BUY:
                    sl_price = self._round_price(
                        entry_price * (1 - account.stoploss_percent / 100), tick_size
                    )
                else:
                    sl_price = self._round_price(
                        entry_price * (1 + account.stoploss_percent / 100), tick_size
                    )
                try:
                    await client.futures_create_order(
                        symbol=payload.symbol.upper(),
                        side=sl_side,
                        type="STOP_MARKET",
                        stopPrice=str(sl_price),
                        closePosition=True,
                    )
                    logger.info("Futures stoploss placed at %s for %s", sl_price, payload.symbol)
                except Exception as e:
                    logger.warning("Futures stoploss order failed: %s", str(e))
                    warnings.append(f"Stoploss order failed: {str(e)}")

            is_testnet = await get_testnet_mode()
            if account.trail_callback_pct and account.trail_callback_pct > 0 and entry_price > 0:
                if is_testnet:
                    warning = (
                        "Trailing stop skipped: Binance Futures Testnet does not support "
                        "TRAILING_STOP_MARKET orders"
                    )
                    logger.warning("%s. This will work on live Futures trading.", warning)
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
                                act_price = self._round_price(
                                    entry_price * (1 + account.trail_activation_pct / 100),
                                    tick_size,
                                )
                            else:
                                act_price = self._round_price(
                                    entry_price * (1 - account.trail_activation_pct / 100),
                                    tick_size,
                                )
                            trail_params["activationPrice"] = str(act_price)

                        await client.futures_create_order(**trail_params)
                        logger.info("Futures trailing stop placed for %s", payload.symbol)
                    except Exception as e:
                        logger.error("Futures trailing stop order failed: %s", str(e), exc_info=True)
                        warnings.append(f"Trailing stop order failed: {str(e)}")

            await self._record_trade(
                db,
                account,
                payload,
                action=record_action,
                side=side,
                entry_price=entry_price,
                quantity=float(quantity),
                usdt_value=usdt_value,
                leverage=account.leverage,
                status="FILLED",
            )

            result = {
                "status": "FILLED",
                "market_type": "futures",
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

        except BinanceAPIException as e:
            error_msg = f"Binance Futures API error: {e.message} (code {e.code})"
            logger.error(error_msg)
            await self._record_trade(db, account, payload, status="ERROR", error=error_msg)
            return {"status": "ERROR", "market_type": "futures", "account": account.name, "error": error_msg}
        except Exception as e:
            error_msg = f"Unexpected Futures error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await self._record_trade(db, account, payload, status="ERROR", error=error_msg)
            return {"status": "ERROR", "market_type": "futures", "account": account.name, "error": error_msg}
        finally:
            if client:
                await client.close_connection()

    async def _close_existing_futures_position(
        self,
        client: AsyncClient,
        account: ExchangeAccount,
        symbol: str,
    ):
        try:
            positions = await client.futures_position_information(symbol=symbol)
            for position in positions:
                amt = float(position.get("positionAmt", 0))
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
                        "Single-trade mode: closed existing Futures %s position (%.4f) on %s for %s",
                        "LONG" if amt > 0 else "SHORT", abs(amt), symbol, account.name,
                    )
        except Exception as e:
            logger.warning("Failed to close existing Futures position on %s: %s", symbol, str(e))

    async def _handle_futures_exit(
        self,
        db: AsyncSession,
        client: AsyncClient,
        account: ExchangeAccount,
        payload: WebhookPayload,
    ) -> dict:
        try:
            positions = await client.futures_position_information(symbol=payload.symbol.upper())
            position = None
            for candidate in positions:
                if float(candidate.get("positionAmt", 0)) != 0:
                    position = candidate
                    break

            if not position:
                return {
                    "status": "NO_POSITION",
                    "market_type": "futures",
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
            exit_price = self._resolve_order_price(order, payload.price)
            if exit_price == 0:
                exit_price = float(position.get("markPrice", 0))
            usdt_value = exit_price * quantity

            await self._record_trade(
                db,
                account,
                payload,
                action="EXIT",
                side=side,
                entry_price=exit_price,
                quantity=quantity,
                usdt_value=round(usdt_value, 2),
                realized_pnl=realized_pnl,
                leverage=account.leverage,
                status="FILLED",
            )
            await self._update_daily_pnl(db, account.id, realized_pnl)

            return {
                "status": "CLOSED",
                "market_type": "futures",
                "account": account.name,
                "symbol": payload.symbol,
                "realized_pnl": realized_pnl,
            }

        except BinanceAPIException as e:
            error_msg = f"Futures exit error: {e.message}"
            logger.error(error_msg)
            await self._record_trade(db, account, payload, status="ERROR", error=error_msg)
            return {"status": "ERROR", "market_type": "futures", "account": account.name, "error": error_msg}

    # ── Spot execution ────────────────────────────────────────────────

    async def _execute_spot_for_account(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
        payload: WebhookPayload,
        bot_settings: BotSettings,
    ) -> dict:
        client = None
        try:
            warnings: list[str] = []
            blocked_reason = await self._check_risk_limits(db, account, bot_settings)
            if blocked_reason:
                await self._record_trade(
                    db, account, payload, status="REJECTED", error=blocked_reason
                )
                return {"status": "BLOCKED", "account": account.name, "reason": blocked_reason}

            secret = decrypt_secret(account.api_secret_encrypted)
            client = await create_binance_client(account.api_key, secret, market_type="spot")

            spot_account = await client.get_account()
            wallet_balance = await self._estimate_spot_equity(client, spot_account)
            available_usdt = self._get_balance_from_account_info(spot_account, "USDT")

            if account.trading_size_type == "fixed":
                risk_amount = account.trading_size_value
            else:
                risk_amount = wallet_balance * (account.trading_size_value / 100.0)
            risk_amount = min(risk_amount, available_usdt)

            action = payload.action.upper()
            if action in ("ENTRY", "LONG", "BUY"):
                side = SIDE_BUY
            elif action in ("SHORT", "EXIT", "SELL"):
                return await self._handle_spot_sell(db, client, account, payload)
            else:
                return {"status": "ERROR", "error": f"Unknown action: {action}"}

            symbol_info = await self._get_spot_symbol_info(client, payload.symbol.upper())
            tick_size = self._get_tick_size(symbol_info)
            quantity = await self._calculate_spot_quantity(
                client, payload.symbol.upper(), risk_amount, symbol_info
            )

            if account.trade_mode == "single":
                base_asset = symbol_info.get("baseAsset")
                existing_qty = await self._get_free_asset_balance(client, base_asset)
                if existing_qty > 0:
                    message = (
                        f"Spot single mode blocked duplicate BUY: existing "
                        f"{base_asset} balance is {existing_qty}"
                    )
                    await self._record_trade(
                        db, account, payload, action="BUY", status="REJECTED", error=message
                    )
                    return {"status": "BLOCKED", "market_type": "spot", "account": account.name, "reason": message}

            try:
                from notifications import notify_order_placed

                await notify_order_placed(
                    account.name, payload.symbol.upper(), "BUY", side, float(quantity), 1
                )
            except Exception:
                pass

            order = await client.create_order(
                symbol=payload.symbol.upper(),
                side=side,
                type=ORDER_TYPE_MARKET,
                quantity=quantity,
            )

            entry_price = self._resolve_order_price(order, payload.price)
            usdt_value = entry_price * float(quantity)

            if account.stoploss_percent and entry_price > 0:
                sl_price = self._round_price(
                    entry_price * (1 - account.stoploss_percent / 100), tick_size
                )
                try:
                    await client.create_order(
                        symbol=payload.symbol.upper(),
                        side=SIDE_SELL,
                        type="STOP_LOSS_LIMIT",
                        quantity=quantity,
                        stopPrice=str(sl_price),
                        price=str(sl_price),
                        timeInForce="GTC",
                    )
                    logger.info("Spot stoploss placed at %s for %s", sl_price, payload.symbol)
                except Exception as e:
                    logger.warning("Spot stoploss order failed: %s", str(e))
                    warnings.append(f"Spot stoploss order failed: {str(e)}")

            if account.trail_callback_pct:
                warnings.append("Trailing stop is Futures-only and ignored for Spot orders")

            await self._record_trade(
                db,
                account,
                payload,
                action="BUY",
                side=side,
                entry_price=entry_price,
                quantity=float(quantity),
                usdt_value=usdt_value,
                leverage=1,
                status="FILLED",
            )

            result = {
                "status": "FILLED",
                "market_type": "spot",
                "account": account.name,
                "symbol": payload.symbol,
                "side": side,
                "action": "BUY",
                "quantity": float(quantity),
                "entry_price": entry_price,
                "usdt_value": usdt_value,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except BinanceAPIException as e:
            error_msg = f"Binance Spot API error: {e.message} (code {e.code})"
            logger.error(error_msg)
            await self._record_trade(db, account, payload, status="ERROR", error=error_msg)
            return {"status": "ERROR", "market_type": "spot", "account": account.name, "error": error_msg}
        except Exception as e:
            error_msg = f"Unexpected Spot error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await self._record_trade(db, account, payload, status="ERROR", error=error_msg)
            return {"status": "ERROR", "market_type": "spot", "account": account.name, "error": error_msg}
        finally:
            if client:
                await client.close_connection()

    async def _handle_spot_sell(
        self,
        db: AsyncSession,
        client: AsyncClient,
        account: ExchangeAccount,
        payload: WebhookPayload,
    ) -> dict:
        try:
            symbol_info = await self._get_spot_symbol_info(client, payload.symbol.upper())
            base_asset = symbol_info.get("baseAsset")
            quantity = await self._get_free_asset_balance(client, base_asset)
            quantity = self._round_quantity(quantity, symbol_info)

            if quantity <= 0:
                return {
                    "status": "NO_HOLDING",
                    "market_type": "spot",
                    "account": account.name,
                    "symbol": payload.symbol,
                }

            order = await client.create_order(
                symbol=payload.symbol.upper(),
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=quantity,
            )

            exit_price = self._resolve_order_price(order, payload.price)
            if exit_price == 0:
                ticker = await client.get_symbol_ticker(symbol=payload.symbol.upper())
                exit_price = float(ticker["price"])
            usdt_value = exit_price * quantity

            await self._record_trade(
                db,
                account,
                payload,
                action="SELL" if payload.action.upper() != "EXIT" else "EXIT",
                side=SIDE_SELL,
                entry_price=exit_price,
                quantity=quantity,
                usdt_value=round(usdt_value, 2),
                realized_pnl=0,
                leverage=1,
                status="FILLED",
            )

            return {
                "status": "CLOSED",
                "market_type": "spot",
                "account": account.name,
                "symbol": payload.symbol,
                "realized_pnl": 0,
            }

        except BinanceAPIException as e:
            error_msg = f"Spot sell error: {e.message}"
            logger.error(error_msg)
            await self._record_trade(db, account, payload, status="ERROR", error=error_msg)
            return {"status": "ERROR", "market_type": "spot", "account": account.name, "error": error_msg}

    # ── Shared helpers ────────────────────────────────────────────────

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

    async def _get_futures_symbol_info(self, client: AsyncClient, symbol: str) -> dict:
        info = await client.futures_exchange_info()
        for symbol_info in info["symbols"]:
            if symbol_info["symbol"] == symbol:
                return symbol_info
        raise ValueError(f"Symbol {symbol} not found on Binance Futures")

    async def _get_spot_symbol_info(self, client: AsyncClient, symbol: str) -> dict:
        info = await client.get_exchange_info()
        for symbol_info in info["symbols"]:
            if symbol_info["symbol"] == symbol:
                return symbol_info
        raise ValueError(f"Symbol {symbol} not found on Binance Spot")

    def _get_tick_size(self, symbol_info: dict) -> float:
        for item in symbol_info["filters"]:
            if item["filterType"] == "PRICE_FILTER":
                return float(item["tickSize"])
        return 0.01

    def _round_price(self, price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return round(price, 2)
        precision = len(str(tick_size).rstrip("0").split(".")[-1]) if "." in str(tick_size) else 0
        return round(round(price / tick_size) * tick_size, precision)

    def _round_quantity(self, quantity: float, symbol_info: dict) -> float:
        step_size = self._get_step_size(symbol_info)
        precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
        return round(quantity - (quantity % step_size), precision)

    def _get_step_size(self, symbol_info: dict) -> float:
        for item in symbol_info["filters"]:
            if item["filterType"] == "LOT_SIZE":
                return float(item["stepSize"])
        return 1.0

    async def _calculate_futures_quantity(
        self, client: AsyncClient, symbol: str, risk_amount: float, symbol_info: dict
    ) -> float:
        ticker = await client.futures_symbol_ticker(symbol=symbol)
        return self._quantity_from_price(risk_amount, float(ticker["price"]), symbol_info, symbol)

    async def _calculate_spot_quantity(
        self, client: AsyncClient, symbol: str, risk_amount: float, symbol_info: dict
    ) -> float:
        ticker = await client.get_symbol_ticker(symbol=symbol)
        return self._quantity_from_price(risk_amount, float(ticker["price"]), symbol_info, symbol)

    def _quantity_from_price(
        self, risk_amount: float, price: float, symbol_info: dict, symbol: str
    ) -> float:
        step_size = self._get_step_size(symbol_info)
        raw_qty = risk_amount / price if price > 0 else 0
        precision = len(str(step_size).rstrip("0").split(".")[-1]) if "." in str(step_size) else 0
        quantity = round(raw_qty - (raw_qty % step_size), precision)

        if quantity <= 0:
            raise ValueError(
                f"Calculated quantity is 0 for {symbol} "
                f"(risk={risk_amount:.2f}, price={price:.2f})"
            )
        return quantity

    def _resolve_order_price(self, order: dict, fallback_price: float | None = None) -> float:
        price = float(order.get("avgPrice", 0) or 0)
        if price == 0 and order.get("fills"):
            fills = order["fills"]
            total_qty = sum(float(fill["qty"]) for fill in fills)
            if total_qty > 0:
                price = sum(float(fill["price"]) * float(fill["qty"]) for fill in fills) / total_qty
        if price == 0 and fallback_price:
            price = fallback_price
        return price

    async def _get_free_asset_balance(self, client: AsyncClient, asset: str | None) -> float:
        if not asset:
            return 0.0
        balance = await client.get_asset_balance(asset=asset)
        if not balance:
            return 0.0
        return float(balance.get("free", 0) or 0)

    def _get_balance_from_account_info(self, account_info: dict, asset: str) -> float:
        for balance in account_info.get("balances", []):
            if balance.get("asset") == asset:
                return float(balance.get("free", 0) or 0)
        return 0.0

    async def _estimate_spot_equity(self, client: AsyncClient, account_info: dict) -> float:
        total = 0.0
        for balance in account_info.get("balances", []):
            asset = balance["asset"]
            amount = float(balance.get("free", 0) or 0) + float(balance.get("locked", 0) or 0)
            if amount <= 0:
                continue
            if asset == "USDT":
                total += amount
                continue
            try:
                ticker = await client.get_symbol_ticker(symbol=f"{asset}USDT")
                total += amount * float(ticker["price"])
            except Exception:
                logger.debug("Skipping non-USDT spot valuation for asset %s", asset)
        return total

    async def _record_trade(
        self,
        db: AsyncSession,
        account: ExchangeAccount,
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
            market_type=_market_type(account),
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


bot_engine = BotEngine()
