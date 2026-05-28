"""Unit tests derived from the AlgoPro PRD (Futures + Spot Edition V1).

Each test cites the PRD section it covers. Tests are pure-logic / mocked —
no real Binance, no real DB.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from bot_engine import BotEngine, _market_type
from market_adapters import compute_spot_avg_cost
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

class TestAccountCreateNoMarketType:
    """V2: accounts cover both Futures and Spot. Market is chosen per mapping."""

    def test_schema_has_no_market_type_field(self):
        assert "market_type" not in AccountCreate.model_fields

    def test_create_without_market_type(self):
        a = AccountCreate(
            name="acc", api_key="abcdefghij", api_secret="abcdefghij",
        )
        assert a.name == "acc"

    def test_extra_market_type_ignored(self):
        # Pydantic default: extra ignored
        a = AccountCreate(
            name="acc", api_key="abcdefghij", api_secret="abcdefghij",
            market_type="margin",  # ignored, not raised
        )
        assert not hasattr(a, "market_type")


class TestSymbolMappingCreateMarketType:
    """Mapping carries its own market_type (account no longer pins one)."""

    def test_accepts_futures(self):
        from schemas import SymbolMappingCreate
        m = SymbolMappingCreate(
            symbol="BTCUSDT", timeframe="5m", account_id=1, market_type="futures",
        )
        assert m.market_type == "futures"

    def test_accepts_spot(self):
        from schemas import SymbolMappingCreate
        m = SymbolMappingCreate(
            symbol="BTCUSDT", timeframe="5m", account_id=1, market_type="spot",
        )
        assert m.market_type == "spot"

    def test_market_type_required(self):
        from schemas import SymbolMappingCreate
        with pytest.raises(ValidationError):
            SymbolMappingCreate(symbol="BTCUSDT", timeframe="5m", account_id=1)

    def test_rejects_unknown(self):
        from schemas import SymbolMappingCreate
        with pytest.raises(ValidationError):
            SymbolMappingCreate(
                symbol="BTCUSDT", timeframe="5m", account_id=1, market_type="margin",
            )


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
    async def test_routes_mapping_market_to_correct_adapter(self, engine, monkeypatch):
        """V2: mapping.market_type drives which adapter is selected; the same account
        can be invoked for either market."""
        from market_adapters import FuturesAdapter, SpotAdapter

        seen_markets: list[str] = []

        async def fake_create_market_adapter(api_key, api_secret, market_type, testnet=None):
            seen_markets.append(market_type)
            adapter_cls = SpotAdapter if market_type == "spot" else FuturesAdapter
            client = MagicMock()
            client.close_connection = AsyncMock()
            return adapter_cls(client)

        async def fake_handle_entry(db, adapter, account, payload, action):
            return {"status": "FILLED", "market_type": adapter.market_type}

        async def fake_handle_exit(db, adapter, account, payload):
            return {"status": "CLOSED", "market_type": adapter.market_type}

        async def fake_check_risk(db, account, bot_settings):
            return None

        monkeypatch.setattr("bot_engine.create_market_adapter", fake_create_market_adapter)
        monkeypatch.setattr("bot_engine.decrypt_secret", lambda _: "secret")
        monkeypatch.setattr(engine, "_handle_entry", fake_handle_entry)
        monkeypatch.setattr(engine, "_handle_exit", fake_handle_exit)
        monkeypatch.setattr(engine, "_check_risk_limits", fake_check_risk)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session():
            yield MagicMock()

        monkeypatch.setattr("bot_engine.async_session", fake_session)

        # One dual-enabled account, called twice with different mapping markets.
        acct = SimpleNamespace(
            name="dual", futures_enabled=True, spot_enabled=True,
            api_key="k", api_secret_encrypted="x",
        )

        payload = WebhookPayload(symbol="BTCUSDT", action="LONG", timeframe="5m")
        bot_settings = SimpleNamespace()

        r_spot = await engine._execute_for_account(acct, "spot", payload, bot_settings)
        r_fut = await engine._execute_for_account(acct, "futures", payload, bot_settings)
        r_legacy = await engine._execute_for_account(acct, None, payload, bot_settings)

        assert seen_markets == ["spot", "futures", "futures"]
        assert r_spot["market_type"] == "spot"
        assert r_fut["market_type"] == "futures"
        assert r_legacy["market_type"] == "futures"


# ──────────────────────────────────────────────────────────────────────────
# Spot cost-basis — weighted-avg buy price from local trade ledger
# ──────────────────────────────────────────────────────────────────────────

class TestComputeSpotAvgCost:
    def test_empty_returns_zero(self):
        assert compute_spot_avg_cost([]) == (0.0, 0.0)

    def test_single_buy(self):
        trades = [{"action": "BUY", "entry_price": 50000, "quantity": 0.1}]
        avg, qty = compute_spot_avg_cost(trades)
        assert avg == 50000
        assert qty == 0.1

    def test_two_buys_weighted_avg(self):
        # 0.1 @ 50000 + 0.1 @ 60000 → avg 55000, qty 0.2
        trades = [
            {"action": "BUY", "entry_price": 50000, "quantity": 0.1},
            {"action": "BUY", "entry_price": 60000, "quantity": 0.1},
        ]
        avg, qty = compute_spot_avg_cost(trades)
        assert avg == pytest.approx(55000)
        assert qty == pytest.approx(0.2)

    def test_partial_sell_preserves_avg(self):
        # buy 0.2 @ 55000, sell 0.05 → 0.15 left at avg 55000
        trades = [
            {"action": "BUY", "entry_price": 50000, "quantity": 0.1},
            {"action": "BUY", "entry_price": 60000, "quantity": 0.1},
            {"action": "SELL", "entry_price": 58000, "quantity": 0.05},
        ]
        avg, qty = compute_spot_avg_cost(trades)
        assert avg == pytest.approx(55000)
        assert qty == pytest.approx(0.15)

    def test_full_sell_resets_cost_basis(self):
        # buy 0.1 @ 50000, full sell, then buy 0.05 @ 70000 → avg 70000
        trades = [
            {"action": "BUY", "entry_price": 50000, "quantity": 0.1},
            {"action": "SELL", "entry_price": 60000, "quantity": 0.1},
            {"action": "BUY", "entry_price": 70000, "quantity": 0.05},
        ]
        avg, qty = compute_spot_avg_cost(trades)
        assert avg == pytest.approx(70000)
        assert qty == pytest.approx(0.05)

    def test_exit_treated_as_sell(self):
        # EXIT alias of SELL — full liquidation
        trades = [
            {"action": "BUY", "entry_price": 50000, "quantity": 0.1},
            {"action": "EXIT", "entry_price": 55000, "quantity": 0.1},
        ]
        assert compute_spot_avg_cost(trades) == (0.0, 0.0)

    def test_skips_invalid_rows(self):
        trades = [
            {"action": "BUY", "entry_price": 0, "quantity": 1},      # zero price
            {"action": "BUY", "entry_price": 100, "quantity": 0},    # zero qty
            {"action": "BUY", "entry_price": 100, "quantity": 1},    # valid
        ]
        avg, qty = compute_spot_avg_cost(trades)
        assert avg == 100
        assert qty == 1


# ──────────────────────────────────────────────────────────────────────────
# SpotAdapter.fetch_remote_trades — seed symbols from current holdings
# ──────────────────────────────────────────────────────────────────────────

class TestSpotFetchRemoteTradesSeed:
    async def test_seeds_symbols_from_balances(self, monkeypatch):
        """On a brand-new account (no mappings, no ledger) the Spot sync still
        queries get_my_trades for every asset currently held, so BUY history
        is recovered for coins the user deposited or bought outside the bot."""
        from market_adapters import SpotAdapter

        client = MagicMock()
        client.get_account = AsyncMock(return_value={
            "balances": [
                {"asset": "USDT", "free": "100", "locked": "0"},  # skipped
                {"asset": "BTC", "free": "0.1", "locked": "0"},   # seed BTCUSDT
                {"asset": "ETH", "free": "0", "locked": "2"},     # seed ETHUSDT
                {"asset": "DOGE", "free": "0", "locked": "0"},    # skipped (zero)
            ]
        })
        queried_symbols: list[str] = []

        async def fake_get_my_trades(symbol):
            queried_symbols.append(symbol)
            return []  # no trades, just record the symbol queried

        client.get_my_trades = AsyncMock(side_effect=fake_get_my_trades)

        adapter = SpotAdapter(client)
        account = SimpleNamespace(id=1, name="new-acc")

        # Fake DB that returns no existing history/mapping symbols
        from contextlib import asynccontextmanager

        class FakeResult:
            def scalars(self):
                class S:
                    def all(self_inner):
                        return []
                return S()

        async def fake_execute(*args, **kwargs):
            return FakeResult()

        db = MagicMock()
        db.execute = fake_execute

        trades = await adapter.fetch_remote_trades(account, db)
        assert trades == []
        # get_my_trades called once per seeded symbol — kwargs not positional
        assert set(queried_symbols) == {"BTCUSDT", "ETHUSDT"}


# ──────────────────────────────────────────────────────────────────────────
# Utility watcher — TradingView alert expiry notification decision
# ──────────────────────────────────────────────────────────────────────────

class TestTVAlertWatcherDecision:
    """Pure-logic checks on the _is_due_for_notification predicate."""

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def _row(self, expires_offset_hours, notified_offset_hours=None):
        from datetime import timedelta
        now = self._now()
        row = SimpleNamespace(
            expires_at=now + timedelta(hours=expires_offset_hours),
            notified_at=(
                now - timedelta(hours=notified_offset_hours)
                if notified_offset_hours is not None else None
            ),
        )
        return row

    def test_far_future_not_due(self):
        from datetime import timedelta
        from utility_watcher import _is_due_for_notification
        row = self._row(expires_offset_hours=72)  # 3 days away, warn=24h
        assert not _is_due_for_notification(
            row, self._now(), timedelta(hours=24), timedelta(hours=12),
        )

    def test_inside_warn_window_first_time(self):
        from datetime import timedelta
        from utility_watcher import _is_due_for_notification
        row = self._row(expires_offset_hours=6)
        assert _is_due_for_notification(
            row, self._now(), timedelta(hours=24), timedelta(hours=12),
        )

    def test_recently_notified_skipped(self):
        from datetime import timedelta
        from utility_watcher import _is_due_for_notification
        row = self._row(expires_offset_hours=6, notified_offset_hours=1)
        assert not _is_due_for_notification(
            row, self._now(), timedelta(hours=24), timedelta(hours=12),
        )

    def test_old_notification_renotifies(self):
        from datetime import timedelta
        from utility_watcher import _is_due_for_notification
        row = self._row(expires_offset_hours=2, notified_offset_hours=13)
        assert _is_due_for_notification(
            row, self._now(), timedelta(hours=24), timedelta(hours=12),
        )

    def test_already_expired_still_notifies(self):
        from datetime import timedelta
        from utility_watcher import _is_due_for_notification
        row = self._row(expires_offset_hours=-3)  # expired 3h ago
        assert _is_due_for_notification(
            row, self._now(), timedelta(hours=24), timedelta(hours=12),
        )


class TestTVAlertCreateSchema:
    def test_requires_expires_at(self):
        from schemas import TVAlertCreate
        with pytest.raises(ValidationError):
            TVAlertCreate(symbol="BTCUSDT", timeframe="5m")  # missing expires_at

    def test_accepts_isoformat(self):
        from schemas import TVAlertCreate
        a = TVAlertCreate(
            symbol="BTCUSDT", timeframe="5m",
            expires_at="2099-01-01T00:00:00+00:00",
        )
        assert a.symbol == "BTCUSDT"
