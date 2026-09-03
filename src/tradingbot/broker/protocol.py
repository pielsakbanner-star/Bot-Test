"""The broker seam (ADR 0004).

Everything above this module is broker-agnostic, which is what lets the
backtester and the live engine share one code path. Two implementations are
planned: ``AlpacaBroker`` and, from Phase 4, ``SimulatedBroker``.

Phase 0 implements :class:`BrokerReader` only. The write half is declared here
so the shape is fixed, but nothing implements it yet -- the Phase 0 exit
criterion is that the bot *cannot* place an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """The account fields the engine actually depends on.

    Money is Decimal (ADR 0008); Alpaca returns these as strings, so the
    conversion is exact.
    """

    account_number: str
    equity: Decimal
    last_equity: Decimal
    cash: Decimal
    buying_power: Decimal
    daytrading_buying_power: Decimal
    multiplier: int
    pattern_day_trader: bool
    daytrade_count: int
    shorting_enabled: bool
    trading_blocked: bool
    account_blocked: bool
    transfers_blocked: bool

    @property
    def is_cash_account(self) -> bool:
        """A multiplier of 1 means no margin, so intraday round trips risk
        good-faith violations. See docs/requirements.md section 7."""
        return self.multiplier <= 1

    @property
    def is_blocked(self) -> bool:
        return self.trading_blocked or self.account_blocked

    @property
    def is_pdt_restricted(self) -> bool:
        """Under the $25k threshold and therefore subject to the 3-day-trade cap."""
        return self.equity < Decimal("25000")


@dataclass(frozen=True, slots=True)
class ClockSnapshot:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True, slots=True)
class CalendarDay:
    session_date: date
    open_at: datetime
    close_at: datetime

    @property
    def is_early_close(self) -> bool:
        """Half days close at 13:00 US/Eastern instead of 16:00."""
        return (self.close_at - self.open_at).total_seconds() < 6 * 3600


@dataclass(frozen=True, slots=True)
class AssetInfo:
    symbol: str
    asset_class: str
    tradable: bool
    status: str
    fractionable: bool
    shortable: bool
    easy_to_borrow: bool


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal

    @property
    def is_short(self) -> bool:
        return self.qty < 0


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal | None
    filled_qty: Decimal
    order_type: str
    status: str
    submitted_at: datetime | None


@runtime_checkable
class BrokerReader(Protocol):
    """Read-only broker access. Sufficient for `doctor` and reconciliation."""

    def get_account(self) -> AccountSnapshot: ...

    def get_clock(self) -> ClockSnapshot: ...

    def get_calendar(self, start: date, end: date) -> list[CalendarDay]: ...

    def get_asset(self, symbol: str) -> AssetInfo: ...

    def list_positions(self) -> list[PositionSnapshot]: ...

    def list_open_orders(self) -> list[OrderSnapshot]: ...


@runtime_checkable
class Broker(BrokerReader, Protocol):
    """Full broker access. Implemented in Phase 3; declared here for the shape.

    Note that there is no ``modify`` -- Alpaca replaces rather than amends, and
    a replace is a cancel plus a submit as far as idempotency is concerned.
    """

    def submit(self, request: object) -> OrderSnapshot: ...

    def cancel(self, order_id: str) -> None: ...

    def cancel_all(self) -> None: ...

    def close_position(self, symbol: str, *, cancel_orders: bool = True) -> None: ...

    def close_all(self, *, cancel_orders: bool = True) -> None: ...
