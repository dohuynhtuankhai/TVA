"""Market adapters that hide Binance Spot vs Futures differences.

Each adapter wraps an open Binance AsyncClient and exposes a normalized
interface used by the Bot Engine and route handlers, eliminating duplicate
per-market branches scattered across the codebase.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from binance import AsyncClient
from binance.enums import (
    FUTURE_ORDER_TYPE_MARKET,
    ORDER_TYPE_MARKET,
    SIDE_BUY,
    SIDE_SELL,
)
from binance.exceptions import BinanceAPIException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import ExchangeAccount, SymbolMapping, TradeRecord

logger = logging.getLogger("algotrade.market_adapters")


# ── Helpers ────────────────────────────────────────────────────────────


def normalize_market_type(value: str | None) -> str:
    return (value or "futures").lower()


def get_tick_size(symbol_info: dict) -> float:
    for item in symbol_info.get("filters", []):
        if item["filterType"] == "PRICE_FILTER":
            return float(item["tickSize"])
    return 0.01


def get_step_size(symbol_info: dict) -> float:
    for item in symbol_info.get("filters", []):
        if item["filterType"] == "LOT_SIZE":
            return float(item["stepSize"])
    return 1.0


def round_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return round(price, 2)
    precision = (
        len(str(tick_size).rstrip("0").split(".")[-1])
        if "." in str(tick_size) else 0
    )
    return round(round(price / tick_size) * tick_size, precision)


def round_quantity(quantity: float, symbol_info: dict) -> float:
    step_size = get_step_size(symbol_info)
    precision = (
        len(str(step_size).rstrip("0").split(".")[-1])
        if "." in str(step_size) else 0
    )
    return round(quantity - (quantity % step_size), precision)


def quantity_from_price(
    risk_amount: float, price: float, symbol_info: dict, symbol: str
) -> float:
    step_size = get_step_size(symbol_info)
    raw_qty = risk_amount / price if price > 0 else 0
    precision = (
        len(str(step_size).rstrip("0").split(".")[-1])
        if "." in str(step_size) else 0
    )
    quantity = round(raw_qty - (raw_qty % step_size), precision)
    if quantity <= 0:
        raise ValueError(
            f"Calculated quantity is 0 for {symbol} "
            f"(risk={risk_amount:.2f}, price={price:.2f})"
        )
    return quantity


def resolve_order_price(order: dict, fallback_price: float | None = None) -> float:
    price = float(order.get("avgPrice", 0) or 0)
    if price == 0 and order.get("fills"):
        fills = order["fills"]
        total_qty = sum(float(fill["qty"]) for fill in fills)
        if total_qty > 0:
            price = sum(
                float(fill["price"]) * float(fill["qty"]) for fill in fills
            ) / total_qty
    if price == 0 and fallback_price:
        price = fallback_price
    return price


def get_balance_from_account_info(account_info: dict, asset: str) -> float:
    for balance in account_info.get("balances", []):
        if balance.get("asset") == asset:
            return float(balance.get("free", 0) or 0)
    return 0.0


def compute_spot_avg_cost(trades: list[dict]) -> tuple[float, float]:
    """Walk Spot trade ledger to derive (avg_buy_price, remaining_qty).

    `trades` is a list of dicts with keys `action` (BUY|SELL|EXIT),
    `entry_price`, `quantity`, ordered oldest-first. Full SELL resets
    cost basis; partial SELL preserves running avg.
    """
    running_qty = 0.0
    running_cost = 0.0
    for t in trades:
        action = (t.get("action") or "").upper()
        price = float(t.get("entry_price") or 0)
        qty = float(t.get("quantity") or 0)
        if qty <= 0 or price <= 0:
            continue
        if action == "BUY":
            running_qty += qty
            running_cost += qty * price
        elif action in ("SELL", "EXIT"):
            if qty >= running_qty - 1e-12:
                running_qty = 0.0
                running_cost = 0.0
            else:
                avg = running_cost / running_qty if running_qty > 0 else 0.0
                running_qty -= qty
                running_cost = running_qty * avg
    if running_qty <= 0:
        return 0.0, 0.0
    return running_cost / running_qty, running_qty


# ── Adapter base ───────────────────────────────────────────────────────


class MarketAdapter(ABC):
    """Per-market interface over a Binance AsyncClient."""

    market_type: str = ""

    def __init__(self, client: AsyncClient):
        self.client = client

    @abstractmethod
    async def fetch_balance(self) -> tuple[float, float, float]:
        """Return (wallet_balance, available_quote, utilization_pct)."""

    @abstractmethod
    async def fetch_sizing_balance(self) -> tuple[float, float]:
        """Return (wallet_for_sizing, available_quote) used by risk sizing."""

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> dict: ...

    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    async def place_market_entry(
        self, symbol: str, side: str, quantity: float
    ) -> dict: ...

    @abstractmethod
    async def place_stoploss(
        self,
        symbol: str,
        entry_side: str,
        quantity: float,
        stop_price: float,
    ) -> None: ...

    def supports_trailing(self, is_testnet: bool) -> tuple[bool, str | None]:
        return False, f"Trailing stop is not supported on {self.market_type}"

    async def place_trailing_stop(
        self,
        symbol: str,
        entry_side: str,
        quantity: float,
        callback_pct: float,
        activation_price: float | None,
    ) -> None:
        raise NotImplementedError(
            f"Trailing stop not implemented for {self.market_type}"
        )

    @abstractmethod
    async def precheck_single_mode(
        self,
        account: ExchangeAccount,
        symbol: str,
        symbol_info: dict,
    ) -> str | None:
        """Pre-entry hook for single mode. Returns block reason or None."""

    @abstractmethod
    async def fetch_positions(
        self,
        account: ExchangeAccount,
        db: AsyncSession | None = None,
    ) -> list[dict]:
        """Return normalized open positions / holdings.

        `db` is optional and used by Spot to enrich rows with weighted-avg
        buy price and unrealized P&L derived from the local trade ledger.
        """

    @abstractmethod
    async def close_position(
        self, symbol: str, fallback_price: float | None = None
    ) -> list[dict]:
        """Force-close open position(s) / sell entire holding for symbol."""

    @abstractmethod
    async def fetch_remote_trades(
        self, account: ExchangeAccount, db: AsyncSession
    ) -> list[dict]:
        """Return normalized trade dicts from Binance for syncing."""

    @abstractmethod
    async def verify_credentials(self, is_testnet: bool) -> bool: ...

    async def close(self) -> None:
        try:
            await self.client.close_connection()
        except Exception:
            logger.debug("close_connection raised; ignoring", exc_info=True)


# ── Futures adapter ────────────────────────────────────────────────────


class FuturesAdapter(MarketAdapter):
    market_type = "futures"

    async def fetch_balance(self) -> tuple[float, float, float]:
        futures = await self.client.futures_account()
        wallet = float(futures.get("totalWalletBalance", 0))
        available = float(futures.get("availableBalance", 0))
        utilization = ((wallet - available) / wallet * 100) if wallet > 0 else 0
        return wallet, available, utilization

    async def fetch_sizing_balance(self) -> tuple[float, float]:
        futures = await self.client.futures_account()
        wallet = float(futures["totalWalletBalance"])
        available = float(futures.get("availableBalance", 0) or 0)
        return wallet, available

    async def get_symbol_info(self, symbol: str) -> dict:
        info = await self.client.futures_exchange_info()
        for symbol_info in info["symbols"]:
            if symbol_info["symbol"] == symbol:
                return symbol_info
        raise ValueError(f"Symbol {symbol} not found on Binance Futures")

    async def get_ticker_price(self, symbol: str) -> float:
        ticker = await self.client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

    async def place_market_entry(
        self, symbol: str, side: str, quantity: float
    ) -> dict:
        return await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
        )

    async def place_stoploss(
        self,
        symbol: str,
        entry_side: str,
        quantity: float,
        stop_price: float,
    ) -> None:
        sl_side = SIDE_SELL if entry_side == SIDE_BUY else SIDE_BUY
        await self.client.futures_create_order(
            symbol=symbol,
            side=sl_side,
            type="STOP_MARKET",
            stopPrice=str(stop_price),
            closePosition=True,
        )

    def supports_trailing(self, is_testnet: bool) -> tuple[bool, str | None]:
        if is_testnet:
            return False, (
                "Trailing stop skipped: Binance Futures Testnet does not "
                "support TRAILING_STOP_MARKET orders"
            )
        return True, None

    async def place_trailing_stop(
        self,
        symbol: str,
        entry_side: str,
        quantity: float,
        callback_pct: float,
        activation_price: float | None,
    ) -> None:
        trail_side = SIDE_SELL if entry_side == SIDE_BUY else SIDE_BUY
        params = {
            "symbol": symbol,
            "side": trail_side,
            "type": "TRAILING_STOP_MARKET",
            "callbackRate": str(round(callback_pct, 1)),
            "quantity": str(quantity),
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
        }
        if activation_price is not None:
            params["activationPrice"] = str(activation_price)
        await self.client.futures_create_order(**params)

    async def precheck_single_mode(
        self,
        account: ExchangeAccount,
        symbol: str,
        symbol_info: dict,
    ) -> str | None:
        try:
            positions = await self.client.futures_position_information(symbol=symbol)
            for position in positions:
                amt = float(position.get("positionAmt", 0))
                if amt != 0:
                    close_side = SIDE_SELL if amt > 0 else SIDE_BUY
                    await self.client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type=FUTURE_ORDER_TYPE_MARKET,
                        quantity=abs(amt),
                        reduceOnly=True,
                    )
                    logger.info(
                        "Single-trade mode: closed existing Futures %s (%.4f) on %s for %s",
                        "LONG" if amt > 0 else "SHORT",
                        abs(amt),
                        symbol,
                        account.name,
                    )
        except Exception as e:
            logger.warning(
                "Failed to close existing Futures position on %s: %s", symbol, str(e)
            )
        return None

    async def fetch_positions(
        self,
        account: ExchangeAccount,
        db: AsyncSession | None = None,
    ) -> list[dict]:
        positions = await self.client.futures_position_information()
        rows: list[dict] = []
        for position in positions:
            amt = float(position.get("positionAmt", 0))
            if amt == 0:
                continue
            entry_price = float(position.get("entryPrice", 0))
            mark_price = float(position.get("markPrice", 0))
            unrealized_pnl = float(position.get("unRealizedProfit", 0))
            leverage = int(position.get("leverage", 1))
            notional = abs(float(position.get("notional", 0)))
            rows.append({
                "account_id": account.id,
                "account_name": account.name,
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
        return rows

    async def close_position(
        self, symbol: str, fallback_price: float | None = None
    ) -> list[dict]:
        positions = await self.client.futures_position_information(symbol=symbol)
        closed: list[dict] = []
        for position in positions:
            amt = float(position.get("positionAmt", 0))
            if amt == 0:
                continue
            close_side = SIDE_SELL if amt > 0 else SIDE_BUY
            quantity = abs(amt)
            order = await self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=quantity,
                reduceOnly=True,
            )
            close_price = resolve_order_price(order, fallback_price)
            if close_price == 0:
                close_price = float(
                    position.get("markPrice", 0) or position.get("entryPrice", 0)
                )
            realized_pnl = float(position.get("unRealizedProfit", 0))
            leverage = int(position.get("leverage", 1))
            closed.append({
                "market_type": "futures",
                "side_label": "LONG" if amt > 0 else "SHORT",
                "close_side": close_side,
                "action": "EXIT",
                "quantity": quantity,
                "close_price": close_price,
                "realized_pnl": round(realized_pnl, 2),
                "leverage": leverage,
            })
        return closed

    async def fetch_remote_trades(
        self, account: ExchangeAccount, db: AsyncSession
    ) -> list[dict]:
        raw = await self.client.futures_account_trades()
        normalized: list[dict] = []
        for t in raw:
            side = t["side"]
            realized_pnl = float(t.get("realizedPnl", 0))
            action = "LONG" if side == "BUY" else "SHORT"
            if t.get("reduceOnly", False) or realized_pnl != 0:
                action = "EXIT"
            normalized.append({
                "symbol": t["symbol"],
                "time": datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc),
                "price": float(t["price"]),
                "quantity": float(t["qty"]),
                "side": side,
                "action": action,
                "realized_pnl": round(realized_pnl, 2),
                "leverage": account.leverage,
            })
        return normalized

    async def verify_credentials(self, is_testnet: bool) -> bool:
        if is_testnet:
            await self.client.futures_account()
            logger.info("Testnet keys verified via Futures account")
            return True
        try:
            perms = await self.client.get_account_api_permissions()
        except BinanceAPIException as e:
            logger.warning("Futures permission check failed: %s", e.message)
            return False
        return bool(perms.get("enableFutures", False))


# ── Spot adapter ───────────────────────────────────────────────────────


class SpotAdapter(MarketAdapter):
    market_type = "spot"

    async def fetch_balance(self) -> tuple[float, float, float]:
        account = await self.client.get_account()
        wallet = 0.0
        available = 0.0
        for balance in account.get("balances", []):
            asset = balance["asset"]
            free = float(balance.get("free", 0) or 0)
            locked = float(balance.get("locked", 0) or 0)
            total = free + locked
            if total <= 0:
                continue
            if asset == "USDT":
                available += free
                wallet += total
                continue
            try:
                ticker = await self.client.get_symbol_ticker(symbol=f"{asset}USDT")
                wallet += total * float(ticker["price"])
            except Exception:
                logger.debug("Skipping spot valuation for non-USDT asset %s", asset)
        utilization = ((wallet - available) / wallet * 100) if wallet > 0 else 0
        return wallet, available, utilization

    async def fetch_sizing_balance(self) -> tuple[float, float]:
        account = await self.client.get_account()
        wallet = await self.estimate_equity(self.client, account)
        available_usdt = get_balance_from_account_info(account, "USDT")
        return wallet, available_usdt

    @staticmethod
    async def estimate_equity(client: AsyncClient, account_info: dict) -> float:
        total = 0.0
        for balance in account_info.get("balances", []):
            asset = balance["asset"]
            amount = (
                float(balance.get("free", 0) or 0)
                + float(balance.get("locked", 0) or 0)
            )
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

    async def get_symbol_info(self, symbol: str) -> dict:
        info = await self.client.get_exchange_info()
        for symbol_info in info["symbols"]:
            if symbol_info["symbol"] == symbol:
                return symbol_info
        raise ValueError(f"Symbol {symbol} not found on Binance Spot")

    async def get_ticker_price(self, symbol: str) -> float:
        ticker = await self.client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        # Spot is always 1x; no-op.
        return None

    async def place_market_entry(
        self, symbol: str, side: str, quantity: float
    ) -> dict:
        return await self.client.create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=quantity,
        )

    async def place_stoploss(
        self,
        symbol: str,
        entry_side: str,
        quantity: float,
        stop_price: float,
    ) -> None:
        # Spot STOP_LOSS_LIMIT — only sell direction is meaningful (we entered BUY).
        await self.client.create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type="STOP_LOSS_LIMIT",
            quantity=quantity,
            stopPrice=str(stop_price),
            price=str(stop_price),
            timeInForce="GTC",
        )

    async def precheck_single_mode(
        self,
        account: ExchangeAccount,
        symbol: str,
        symbol_info: dict,
    ) -> str | None:
        base_asset = symbol_info.get("baseAsset")
        existing_qty = await self._free_balance(base_asset)
        if existing_qty > 0:
            return (
                f"Spot single mode blocked duplicate BUY: existing "
                f"{base_asset} balance is {existing_qty}"
            )
        return None

    async def _free_balance(self, asset: str | None) -> float:
        if not asset:
            return 0.0
        balance = await self.client.get_asset_balance(asset=asset)
        if not balance:
            return 0.0
        return float(balance.get("free", 0) or 0)

    async def fetch_positions(
        self,
        account: ExchangeAccount,
        db: AsyncSession | None = None,
    ) -> list[dict]:
        spot_account = await self.client.get_account()
        rows: list[dict] = []
        for balance in spot_account.get("balances", []):
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
                ticker = await self.client.get_symbol_ticker(symbol=symbol)
                mark_price = float(ticker["price"])
            except Exception:
                logger.debug("Skipping Spot holding without USDT ticker: %s", asset)
                continue
            notional = amount * mark_price

            avg_buy = 0.0
            unrealized_pnl = 0.0
            pnl_percent = 0.0
            if db is not None:
                avg_buy, _ledger_qty = await self._compute_avg_buy(db, account.id, symbol)
                if avg_buy > 0:
                    unrealized_pnl = (mark_price - avg_buy) * amount
                    pnl_percent = (mark_price - avg_buy) / avg_buy * 100

            rows.append({
                "account_id": account.id,
                "account_name": account.name,
                "market_type": "spot",
                "symbol": symbol,
                "asset": asset,
                "side": "HOLD",
                "size": amount,
                "free": free,
                "locked": locked,
                "entry_price": round(avg_buy, 8) if avg_buy > 0 else 0,
                "mark_price": mark_price,
                "notional": round(notional, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "leverage": 1,
                "pnl_percent": round(pnl_percent, 2),
            })
        return rows

    @staticmethod
    async def _compute_avg_buy(
        db: AsyncSession, account_id: int, symbol: str
    ) -> tuple[float, float]:
        result = await db.execute(
            select(TradeRecord.action, TradeRecord.entry_price, TradeRecord.quantity)
            .where(
                TradeRecord.account_id == account_id,
                TradeRecord.symbol == symbol,
                TradeRecord.market_type == "spot",
                TradeRecord.status == "FILLED",
            )
            .order_by(TradeRecord.executed_at.asc())
        )
        trades = [
            {"action": row.action, "entry_price": row.entry_price, "quantity": row.quantity}
            for row in result.all()
        ]
        return compute_spot_avg_cost(trades)

    async def close_position(
        self, symbol: str, fallback_price: float | None = None
    ) -> list[dict]:
        symbol_info = await self.client.get_symbol_info(symbol)
        if not symbol_info:
            raise ValueError(f"Spot symbol {symbol} not found")
        base_asset = symbol_info["baseAsset"]
        quantity = await self._free_balance(base_asset)
        quantity = round_quantity(quantity, symbol_info)
        if quantity <= 0:
            return []
        order = await self.client.create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=quantity,
        )
        close_price = resolve_order_price(order, fallback_price)
        if close_price == 0:
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            close_price = float(ticker["price"])
        return [{
            "market_type": "spot",
            "side_label": "SELL",
            "close_side": SIDE_SELL,
            "action": "SELL",
            "quantity": quantity,
            "close_price": close_price,
            "realized_pnl": 0,
            "leverage": 1,
        }]

    async def fetch_remote_trades(
        self, account: ExchangeAccount, db: AsyncSession
    ) -> list[dict]:
        history_symbols = await db.execute(
            select(TradeRecord.symbol)
            .where(TradeRecord.account_id == account.id)
            .distinct()
        )
        symbols = set(history_symbols.scalars().all())
        configured = await db.execute(
            select(SymbolMapping.symbol).where(
                SymbolMapping.account_id == account.id
            )
        )
        symbols.update(configured.scalars().all())

        normalized: list[dict] = []
        for symbol in symbols:
            try:
                trades = await self.client.get_my_trades(symbol=symbol)
            except Exception as e:
                logger.warning(
                    "Spot sync skipped %s for '%s': %s", symbol, account.name, e
                )
                continue
            for t in trades:
                side = "BUY" if t.get("isBuyer") else "SELL"
                action = "BUY" if side == "BUY" else "SELL"
                normalized.append({
                    "symbol": t["symbol"],
                    "time": datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc),
                    "price": float(t["price"]),
                    "quantity": float(t["qty"]),
                    "side": side,
                    "action": action,
                    "realized_pnl": 0.0,
                    "leverage": 1,
                })
        return normalized

    async def verify_credentials(self, is_testnet: bool) -> bool:
        if is_testnet:
            await self.client.get_account()
            logger.info("Testnet keys verified via Spot account")
            return True
        try:
            perms = await self.client.get_account_api_permissions()
            if perms.get("enableSpotAndMarginTrading", False):
                return True
        except BinanceAPIException as e:
            logger.debug("Spot permission probe failed: %s", e.message)
        try:
            await self.client.get_account()
            return True
        except BinanceAPIException as e:
            logger.warning("Spot account probe failed: %s", e.message)
            return False


# ── Factory ────────────────────────────────────────────────────────────


_ADAPTERS: dict[str, type[MarketAdapter]] = {
    "futures": FuturesAdapter,
    "spot": SpotAdapter,
}


def adapter_class(market_type: str | None) -> type[MarketAdapter]:
    market = normalize_market_type(market_type)
    try:
        return _ADAPTERS[market]
    except KeyError as exc:
        raise ValueError(f"Unknown market_type: {market}") from exc


async def create_market_adapter(
    api_key: str,
    api_secret: str,
    market_type: str | None,
    testnet: bool | None = None,
) -> MarketAdapter:
    """Create an AsyncClient wired to the right Binance market and wrap it."""
    from bot_engine import create_binance_client  # local import to avoid cycle

    market = normalize_market_type(market_type)
    client = await create_binance_client(
        api_key, api_secret, testnet=testnet, market_type=market
    )
    return adapter_class(market)(client)


def adapter_for_account_with_client(
    account: ExchangeAccount, client: AsyncClient
) -> MarketAdapter:
    return adapter_class(account.market_type)(client)
