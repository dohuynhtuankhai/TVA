"""Unit tests derived from the AlgoTrade Pro PRD (Futures + Spot Edition V1).

Each test cites the PRD section it covers. Tests are pure-logic / mocked —
no real Binance, no real DB.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from bot_engine import BotEngine, _market_type
from routes.webhook import _clean_symbol, normalize_timeframe
from schemas import AccountCreate, WebhookPayload


# ──────────────────────────────────────────────────────────────────────────
# PRD 2.2 — Webhook JSON format & valid actions
# ──────────────────────────────────────────────────────────────────────────

class TestWebhookPayloadSchema:
    @pytest.mark.parametrize(
        "action",
        ["LONG", "SHORT", "EXIT", "BUY", "SELL", "ENTRY"],
    )
    def test_accepts_all_prd_actions(self, action):
        p = WebhookPayload(symbol="BTCUSDT", action=action, timeframe="5m", price=65000.0)
        assert p.action == action

    def test_rejects_unknown_action(self):
        with pytest.raises(ValidationError):
            WebhookPayload(symbol="BTCUSDT", action="HOLD", timeframe="5m")

    def test_price_optional(self):
        p = WebhookPayload(symbol="BTCUSDT", action="LONG", timeframe="5m")
        assert p.price is None

    def test_requires_symbol_action_timeframe(self):
        with pytest.raises(ValidationError):
            WebhookPayload(symbol="BTCUSDT", action="LONG")  # missing timeframe


# ──────────────────────────────────────────────────────────────────────────
# PRD 2.3 — Symbol Normalization (strip exchange prefix, .P, PERP)
# ──────────────────────────────────────────────────────────────────────────

class TestCleanSymbol:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("BTCUSDT", "BTCUSDT"),
            ("BINANCE:BTCUSDT", "BTCUSDT"),
            ("BYBIT:ETHUSDT", "ETHUSDT"),
            ("BTCUSDT.P", "BTCUSDT"),
            ("BINANCE:BTCUSDT.P", "BTCUSDT"),
            ("BTCUSDTPERP", "BTCUSDT"),
            ("btcusdt", "BTCUSDT"),
        ],
    )
    def test_strips_and_uppercases(self, raw, expected):
        assert _clean_symbol(raw) == expected


# ──────────────────────────────────────────────────────────────────────────
# PRD 2.3 — Timeframe Normalization
# ──────────────────────────────────────────────────────────────────────────

class TestNormalizeTimeframe:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("5m", "5"),
            ("1h", "60"),
            ("4h", "240"),
            ("1D", "D"),
            ("1d", "D"),
            ("15m", "15"),
            ("1w", "W"),
            ("60", "60"),       # already TV format passes through
            ("D", "D"),
            ("M", "M"),         # capital M = month
        ],
    )
    def test_user_friendly_to_tradingview(self, raw, expected):
        assert normalize_timeframe(raw) == expected

    def test_unknown_returned_as_is(self):
        assert normalize_timeframe("xyz") == "xyz"


# ──────────────────────────────────────────────────────────────────────────
# PRD 2.1 / 2.5 — market_type helper & backward compatibility
# ──────────────────────────────────────────────────────────────────────────

class TestMarketTypeHelper:
    def test_defaults_to_futures_when_missing(self):
        """PRD: Existing accounts default to Futures for backward compatibility."""
        acct = SimpleNamespace(market_type=None)
        assert _market_type(acct) == "futures"

    def test_defaults_to_futures_when_attr_absent(self):
        acct = SimpleNamespace()
        assert _market_type(acct) == "futures"

    def test_returns_spot_lowercased(self):
        acct = SimpleNamespace(market_type="Spot")
        assert _market_type(acct) == "spot"

    def test_returns_futures_lowercased(self):
        acct = SimpleNamespace(market_type="FUTURES")
        assert _market_type(acct) == "futures"


# ──────────────────────────────────────────────────────────────────────────
# Schemas — AccountCreate market_type pattern (PRD 2.1)
# ──────────────────────────────────────────────────────────────────────────

class TestAccountCreateMarketType:
    def test_accepts_futures(self):
        a = AccountCreate(
            name="acc", api_key="abcdefghij", api_secret="abcdefghij",
            market_type="futures",
        )
        assert a.market_type == "futures"

    def test_accepts_spot(self):
        a = AccountCreate(
            name="acc", api_key="abcdefghij", api_secret="abcdefghij",
            market_type="spot",
        )
        assert a.market_type == "spot"

    def test_rejects_unknown_market_type(self):
        with pytest.raises(ValidationError):
            AccountCreate(
                name="acc", api_key="abcdefghij", api_secret="abcdefghij",
                market_type="margin",
            )

    def test_market_type_default_futures(self):
        """PRD: Existing accounts default to Futures."""
        a = AccountCreate(
            name="acc", api_key="abcdefghij", api_secret="abcdefghij",
        )
        assert a.market_type == "futures"


# ──────────────────────────────────────────────────────────────────────────
# Bot engine pure helpers — quantity / price rounding (PRD 2.4)
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    return BotEngine()


def _symbol_info(tick="0.01", step="0.001", base="BTC"):
    return {
        "symbol": "BTCUSDT",
        "baseAsset": base,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "LOT_SIZE", "stepSize": step},
        ],
    }


class TestRoundingHelpers:
    def test_get_tick_size(self, engine):
        assert engine._get_tick_size(_symbol_info(tick="0.5")) == 0.5

    def test_get_tick_size_default_when_missing(self, engine):
        assert engine._get_tick_size({"filters": []}) == 0.01

    def test_get_step_size(self, engine):
        assert engine._get_step_size(_symbol_info(step="0.01")) == 0.01

    def test_get_step_size_default_when_missing(self, engine):
        assert engine._get_step_size({"filters": []}) == 1.0

    def test_round_price_to_tick(self, engine):
        # tick=0.5 → 100.3 rounds to 100.5
        assert engine._round_price(100.3, 0.5) == 100.5

    def test_round_price_zero_tick(self, engine):
        assert engine._round_price(100.123, 0) == 100.12

    def test_round_quantity_truncates_to_step(self, engine):
        info = _symbol_info(step="0.001")
        # 0.12345 → 0.123 (truncated to 3dp)
        assert engine._round_quantity(0.12345, info) == 0.123


class TestQuantityFromPrice:
    def test_basic(self, engine):
        info = _symbol_info(step="0.001")
        # risk 100 USDT, price 50000 → 0.002 BTC
        q = engine._quantity_from_price(100.0, 50000.0, info, "BTCUSDT")
        assert q == 0.002

    def test_raises_when_quantity_rounds_to_zero(self, engine):
        info = _symbol_info(step="1")
        # risk 10 USDT @ 50000 → raw 0.0002 → step 1 → 0
        with pytest.raises(ValueError, match="quantity is 0"):
            engine._quantity_from_price(10.0, 50000.0, info, "BTCUSDT")

    def test_raises_when_price_zero(self, engine):
        info = _symbol_info()
        with pytest.raises(ValueError):
            engine._quantity_from_price(100.0, 0.0, info, "BTCUSDT")


# ──────────────────────────────────────────────────────────────────────────
# Bot engine — order price resolution (PRD 2.4 ledger correctness)
# ──────────────────────────────────────────────────────────────────────────

class TestResolveOrderPrice:
    def test_uses_avg_price_when_present(self, engine):
        assert engine._resolve_order_price({"avgPrice": "65000"}) == 65000.0

    def test_uses_vwap_of_fills_when_avg_zero(self, engine):
        # Two fills: 1@100 + 1@200 → vwap 150
        order = {
            "avgPrice": 0,
            "fills": [
                {"price": "100", "qty": "1"},
                {"price": "200", "qty": "1"},
            ],
        }
        assert engine._resolve_order_price(order) == 150.0

    def test_falls_back_to_payload_price(self, engine):
        assert engine._resolve_order_price({}, fallback_price=42.0) == 42.0

    def test_returns_zero_when_no_data(self, engine):
        assert engine._resolve_order_price({}) == 0


# ──────────────────────────────────────────────────────────────────────────
# Bot engine — spot balance helpers (PRD 2.4)
# ──────────────────────────────────────────────────────────────────────────

class TestSpotBalanceHelpers:
    def test_balance_found(self, engine):
        info = {"balances": [
            {"asset": "USDT", "free": "1234.5", "locked": "0"},
            {"asset": "BTC", "free": "0.1", "locked": "0"},
        ]}
        assert engine._get_balance_from_account_info(info, "USDT") == 1234.5

    def test_balance_missing_returns_zero(self, engine):
        info = {"balances": [{"asset": "BTC", "free": "0.1"}]}
        assert engine._get_balance_from_account_info(info, "USDT") == 0.0

    def test_balance_empty_balances(self, engine):
        assert engine._get_balance_from_account_info({}, "USDT") == 0.0


class TestEstimateSpotEquity:
    async def test_sums_usdt_and_priced_assets(self, engine):
        """PRD 2.1 / 2.4 — Spot equity = USDT + non-USDT priced via ticker."""
        info = {"balances": [
            {"asset": "USDT", "free": "100", "locked": "50"},   # 150 USDT
            {"asset": "BTC", "free": "0.1", "locked": "0"},     # 0.1 * 50_000 = 5_000
            {"asset": "ETH", "free": "0", "locked": "0"},       # skipped (0)
        ]}
        client = MagicMock()
        client.get_symbol_ticker = AsyncMock(return_value={"price": "50000"})
        total = await engine._estimate_spot_equity(client, info)
        assert total == pytest.approx(150 + 5000)
        client.get_symbol_ticker.assert_awaited_with(symbol="BTCUSDT")

    async def test_skips_assets_without_ticker(self, engine):
        info = {"balances": [
            {"asset": "USDT", "free": "10", "locked": "0"},
            {"asset": "WEIRDCOIN", "free": "5", "locked": "0"},
        ]}
        client = MagicMock()
        client.get_symbol_ticker = AsyncMock(side_effect=Exception("no symbol"))
        total = await engine._estimate_spot_equity(client, info)
        assert total == 10  # only USDT counted


# ──────────────────────────────────────────────────────────────────────────
# Bot engine — market-aware dispatch (PRD 2.1, 2.4)
# ──────────────────────────────────────────────────────────────────────────

class TestExecuteForAccountDispatch:
    async def test_routes_spot_account_to_spot_executor(self, engine, monkeypatch):
        """PRD 2.4: Spot Execution uses Binance Spot endpoints, Futures uses Futures."""
        calls = []

        async def fake_spot(db, account, payload, bot_settings):
            calls.append("spot")
            return {"status": "FILLED", "market_type": "spot"}

        async def fake_futures(db, account, payload, bot_settings):
            calls.append("futures")
            return {"status": "FILLED", "market_type": "futures"}

        monkeypatch.setattr(engine, "_execute_spot_for_account", fake_spot)
        monkeypatch.setattr(engine, "_execute_futures_for_account", fake_futures)

        # Avoid touching the real DB
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def fake_session():
            yield MagicMock()
        monkeypatch.setattr("bot_engine.async_session", fake_session)

        spot_acct = SimpleNamespace(market_type="spot", name="spot-acc")
        fut_acct = SimpleNamespace(market_type="futures", name="fut-acc")
        legacy_acct = SimpleNamespace(market_type=None, name="legacy")  # PRD backcompat

        payload = WebhookPayload(symbol="BTCUSDT", action="LONG", timeframe="5m")
        bot_settings = SimpleNamespace()

        await engine._execute_for_account(spot_acct, payload, bot_settings)
        await engine._execute_for_account(fut_acct, payload, bot_settings)
        await engine._execute_for_account(legacy_acct, payload, bot_settings)

        assert calls == ["spot", "futures", "futures"]
