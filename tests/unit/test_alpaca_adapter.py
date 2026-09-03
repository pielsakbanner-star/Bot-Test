"""Broker adapter: error translation, decimal conversion, calendar, throttling.

No network. The SDK's ``APIError`` is constructed directly so every branch of
the translation table is exercised.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from datetime import time as clock_time
from decimal import Decimal
from typing import Any, cast

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Calendar

from tradingbot.broker.alpaca import (
    MARKET_TZ,
    AlpacaBroker,
    TokenBucket,
    to_decimal,
    translate,
)
from tradingbot.broker.protocol import BrokerReader
from tradingbot.errors import (
    AuthError,
    InsufficientBuyingPowerError,
    NotTradableError,
    PermanentOrderRejectError,
    RateLimitedError,
    TransientBrokerError,
    WashTradeBlockedError,
)


class _StatusError(APIError):
    """APIError with a settable status.

    The SDK derives ``status_code`` from an attached HTTP error, so it cannot be
    assigned directly. Overriding the property in a subclass keeps these tests
    working regardless of how the SDK stores it.
    """

    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)  # type: ignore[no-untyped-call]
        self._status = status

    @property
    def status_code(self) -> int | None:
        return self._status


def api_error(message: str, status: int | None = None) -> APIError:
    return _StatusError(message, status)


# --- error translation --------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "status", "expected"),
    [
        ("unauthorized", 401, AuthError),
        ("forbidden", 403, AuthError),
        ("insufficient buying power", 403, InsufficientBuyingPowerError),
        ("your subscription does not permit sip", 403, PermanentOrderRejectError),
        ("too many requests", 429, RateLimitedError),
        ("asset not found", 404, NotTradableError),
        ("internal server error", 500, TransientBrokerError),
        ("bad gateway", 502, TransientBrokerError),
        ("potential wash trade detected", 422, WashTradeBlockedError),
        ("asset is not tradable", 422, NotTradableError),
        ("insufficient qty available", 422, InsufficientBuyingPowerError),
        ("something else entirely", 422, PermanentOrderRejectError),
        ("no status at all", None, PermanentOrderRejectError),
    ],
)
def test_translation_table(message: str, status: int | None, expected: type) -> None:
    assert isinstance(translate(api_error(message, status)), expected)


def test_buying_power_403_is_not_an_auth_error() -> None:
    """403 is overloaded at Alpaca; misclassifying this one would halt the bot
    for what is actually a routine, per-order rejection."""
    assert isinstance(
        translate(api_error("insufficient buying power", 403)),
        InsufficientBuyingPowerError,
    )


# --- decimals (ADR 0008) ------------------------------------------------------


def test_string_money_is_exact() -> None:
    assert to_decimal("100000.55") == Decimal("100000.55")


def test_float_money_avoids_binary_artifacts() -> None:
    assert str(to_decimal(0.1)) == "0.1"


def test_none_uses_the_default() -> None:
    assert to_decimal(None) == Decimal("0")
    assert to_decimal(None, Decimal("1")) == Decimal("1")


def test_unparseable_value_raises() -> None:
    with pytest.raises(Exception, match="decimal"):
        to_decimal("not-a-number")


# --- calendar -----------------------------------------------------------------


SESSION = date(2026, 11, 27)


class _Day:
    """Mirrors what alpaca-py actually produces: naive datetimes whose wall
    clock is US/Eastern, built by strptime-ing the API's "09:30" against the
    session date."""

    def __init__(self, open_h: int, open_m: int, close_h: int, close_m: int) -> None:
        self.date = SESSION
        self.open = datetime.combine(SESSION, clock_time(open_h, open_m))
        self.close = datetime.combine(SESSION, clock_time(close_h, close_m))


def test_calendar_uses_the_real_sdk_shape() -> None:
    """Guard against the SDK changing Calendar.open back to a bare time."""
    day = Calendar(date="2026-11-27", open="09:30", close="13:00")
    assert isinstance(day.open, datetime)
    assert day.open.tzinfo is None
    converted = AlpacaBroker._to_calendar_day(day)
    assert converted.is_early_close


def test_calendar_times_are_localised_to_market_time() -> None:
    day = AlpacaBroker._to_calendar_day(_Day(9, 30, 16, 0))
    assert day.open_at.tzinfo is not None
    assert day.open_at.astimezone(MARKET_TZ).hour == 9
    assert day.close_at.astimezone(MARKET_TZ).hour == 16
    assert not day.is_early_close


def test_half_day_is_detected() -> None:
    """The day after Thanksgiving closes at 13:00 -- the flatten window has to
    move with it, so this flag has to be right."""
    day = AlpacaBroker._to_calendar_day(_Day(9, 30, 13, 0))
    assert day.is_early_close


def test_aware_session_times_are_left_alone() -> None:
    naive = _Day(9, 30, 16, 0)
    naive.open = naive.open.replace(tzinfo=MARKET_TZ)
    naive.close = naive.close.replace(tzinfo=MARKET_TZ)
    day = AlpacaBroker._to_calendar_day(naive)
    assert day.open_at.astimezone(MARKET_TZ).hour == 9


def test_bare_times_are_still_accepted() -> None:
    """Tolerated for forward-compatibility, so assert it works."""

    class _TimeDay:
        date = SESSION
        open = clock_time(9, 30)
        close = clock_time(16, 0)

    day = AlpacaBroker._to_calendar_day(_TimeDay())
    assert day.open_at.astimezone(MARKET_TZ).hour == 9


# --- throttling ---------------------------------------------------------------


def test_token_bucket_allows_a_burst_up_to_capacity() -> None:
    bucket = TokenBucket(per_minute=60)
    started = time.monotonic()
    for _ in range(60):
        bucket.acquire()
    assert time.monotonic() - started < 0.5


def test_token_bucket_throttles_beyond_capacity() -> None:
    bucket = TokenBucket(per_minute=60)  # one token per second
    for _ in range(60):
        bucket.acquire()
    started = time.monotonic()
    bucket.acquire()
    assert time.monotonic() - started >= 0.5


# --- protocol conformance -----------------------------------------------------


def test_adapter_satisfies_the_reader_protocol() -> None:
    assert isinstance(AlpacaBroker(client=_stub_client()), BrokerReader)


def test_phase_0_adapter_cannot_place_orders() -> None:
    """The Phase 0 exit criterion, asserted rather than assumed."""
    broker: Any = AlpacaBroker(client=_stub_client())
    for method in ("submit", "cancel", "cancel_all", "close_position", "close_all"):
        assert not hasattr(broker, method), f"{method} must not exist before Phase 3"


class _StubClient:
    """Minimal stand-in; no method on it is called by these tests."""


def _stub_client() -> TradingClient:
    """The stub only needs to satisfy the constructor, never be called."""
    return cast(TradingClient, _StubClient())
