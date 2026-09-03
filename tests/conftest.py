"""Shared fixtures.

The config builder here produces a *minimal valid* config. Tests then mutate one
thing at a time, so a failure names the field that broke rather than leaving you
to diff two large YAML blobs.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from tradingbot.broker.protocol import (
    AccountSnapshot,
    AssetInfo,
    CalendarDay,
    ClockSnapshot,
    OrderSnapshot,
    PositionSnapshot,
)
from tradingbot.config import DataFeed, Secrets
from tradingbot.errors import BrokerError

MINIMAL_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "account": {"data_feed": "iex", "asset_class": "us_equity"},
    "universe": {"symbols": ["SPY", "QQQ"]},
    "session": {"trade_regular_hours": True, "trade_extended_hours": False},
    "strategies": [
        {
            "id": "sma_crossover",
            "enabled": True,
            "class": "tradingbot.strategies.sma_crossover:SmaCrossover",
            "symbols": ["SPY"],
            "timeframe": "5Min",
            "params": {"fast": 20, "slow": 50},
        }
    ],
    "risk": {
        "max_daily_loss_pct": 2.0,
        "max_drawdown_pct": 10.0,
        "max_gross_exposure_pct": 100.0,
        "max_net_exposure_pct": 100.0,
        "max_open_positions": 10,
        "max_position_pct": 10.0,
        "max_order_notional_pct": 5.0,
        "max_order_pct_adv": 1.0,
        "min_price": 5.00,
        "max_spread_bps": 30,
        "allow_shorts": False,
        "pdt_policy": "block_entries",
        "sizing": {
            "method": "volatility_target",
            "risk_per_trade_pct": 0.5,
            "atr_period": 14,
            "atr_multiple": 2.0,
        },
        "eod_policy": "flatten_intraday",
        "flatten_lead_minutes": 10,
        "strict_reconciliation": True,
        "kill_liquidates": True,
    },
    "observability": {
        "alerts": {
            "webhook_env": "ALERT_WEBHOOK_URL",
            "events": ["kill_switch", "risk_halt", "daily_summary"],
        }
    },
}


@pytest.fixture
def config_dict() -> dict[str, Any]:
    return copy.deepcopy(MINIMAL_CONFIG)


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[..., Path]:
    """Write a config dict to a YAML file and return its path."""

    def _write(data: dict[str, Any], name: str = "paper.yaml") -> Path:
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def paper_secrets() -> Secrets:
    return Secrets(
        _env_file=None,
        alpaca_paper_key_id="PKTESTKEYID0000000",
        alpaca_paper_secret_key="paper-secret-value-0123456789",
        alert_webhook_url="https://example.invalid/hook",
    )


# -----------------------------------------------------------------------------
# Fake broker
# -----------------------------------------------------------------------------

NOW = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)


def make_account(**overrides: Any) -> AccountSnapshot:
    defaults: dict[str, Any] = {
        "account_number": "PA3TEST",
        "equity": Decimal("100000"),
        "last_equity": Decimal("99000"),
        "cash": Decimal("50000"),
        "buying_power": Decimal("200000"),
        "daytrading_buying_power": Decimal("400000"),
        "multiplier": 4,
        "pattern_day_trader": False,
        "daytrade_count": 0,
        "shorting_enabled": True,
        "trading_blocked": False,
        "account_blocked": False,
        "transfers_blocked": False,
    }
    return AccountSnapshot(**{**defaults, **overrides})


def make_asset(symbol: str, **overrides: Any) -> AssetInfo:
    defaults: dict[str, Any] = {
        "symbol": symbol,
        "asset_class": "us_equity",
        "tradable": True,
        "status": "active",
        "fractionable": True,
        "shortable": True,
        "easy_to_borrow": True,
    }
    return AssetInfo(**{**defaults, **overrides})


class FakeBroker:
    """In-memory BrokerReader for doctor tests."""

    def __init__(
        self,
        *,
        account: AccountSnapshot | None = None,
        clock_ts: datetime = NOW,
        assets: dict[str, AssetInfo] | None = None,
        positions: list[PositionSnapshot] | None = None,
        orders: list[OrderSnapshot] | None = None,
        early_close: bool = False,
        market_closed_today: bool = False,
        raise_on_account: BrokerError | None = None,
    ) -> None:
        self._account = account or make_account()
        self._clock_ts = clock_ts
        self._assets = assets
        self._positions = positions or []
        self._orders = orders or []
        self._early_close = early_close
        self._market_closed_today = market_closed_today
        self._raise_on_account = raise_on_account

    def get_account(self) -> AccountSnapshot:
        if self._raise_on_account is not None:
            raise self._raise_on_account
        return self._account

    def get_clock(self) -> ClockSnapshot:
        return ClockSnapshot(
            timestamp=self._clock_ts,
            is_open=True,
            next_open=self._clock_ts + timedelta(hours=18),
            next_close=self._clock_ts + timedelta(hours=6),
        )

    def get_calendar(self, start: Any, end: Any) -> list[CalendarDay]:
        session_date = NOW.date()
        open_at = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
        close_at = open_at + timedelta(hours=3 if self._early_close else 6, minutes=30)
        if self._market_closed_today:
            return [
                CalendarDay(
                    session_date=session_date + timedelta(days=2),
                    open_at=open_at + timedelta(days=2),
                    close_at=close_at + timedelta(days=2),
                )
            ]
        return [
            CalendarDay(session_date=session_date, open_at=open_at, close_at=close_at)
        ]

    def get_asset(self, symbol: str) -> AssetInfo:
        if self._assets is None:
            return make_asset(symbol)
        try:
            return self._assets[symbol]
        except KeyError as exc:
            msg = f"asset {symbol} not found"
            raise BrokerError(msg) from exc

    def list_positions(self) -> list[PositionSnapshot]:
        return self._positions

    def list_open_orders(self) -> list[OrderSnapshot]:
        return self._orders


class FakeFeedProbe:
    def __init__(self, *, error: BrokerError | None = None) -> None:
        self._error = error
        self.calls: list[tuple[str, DataFeed]] = []

    def probe(self, symbol: str, feed: DataFeed) -> None:
        self.calls.append((symbol, feed))
        if self._error is not None:
            raise self._error
