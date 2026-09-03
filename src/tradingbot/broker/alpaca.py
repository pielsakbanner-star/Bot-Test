"""Alpaca implementation of the broker seam.

Phase 0 implements :class:`~tradingbot.broker.protocol.BrokerReader` only. The
order-submitting half of the protocol deliberately does not exist yet -- the
Phase 0 exit criterion is that the bot *cannot* place an order.

Two things this module owns that nothing above it should know about:

* **Error translation.** SDK and HTTP failures become the small set in
  :mod:`tradingbot.errors` (docs/alpaca-integration.md section 9).
* **Rate limiting.** A client-side token bucket keeps us inside the per-account
  request budget (requirement N-4).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from datetime import time as clock_time
from decimal import Decimal, InvalidOperation
from typing import Any, Final, TypeVar
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Asset, Clock, Order, Position
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest

from tradingbot.broker.protocol import (
    AccountSnapshot,
    AssetInfo,
    CalendarDay,
    ClockSnapshot,
    OrderSnapshot,
    PositionSnapshot,
)
from tradingbot.config import DataFeed, Mode, Secrets
from tradingbot.errors import (
    AuthError,
    BrokerError,
    InsufficientBuyingPowerError,
    NotTradableError,
    PermanentOrderRejectError,
    RateLimitedError,
    TransientBrokerError,
    WashTradeBlockedError,
)

MARKET_TZ: Final = ZoneInfo("America/New_York")

# The basic plan allows roughly 200 requests/minute per account. We stay well
# under it: the steady state is websocket-driven, and REST calls are rare.
DEFAULT_RATE_LIMIT_PER_MINUTE: Final = 150

T = TypeVar("T")

# HTTP statuses we branch on.
HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR_MIN: Final = 500
HTTP_SERVER_ERROR_MAX: Final = 600


# -----------------------------------------------------------------------------
# Conversion helpers
# -----------------------------------------------------------------------------


def to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Convert an SDK value to Decimal exactly.

    Alpaca returns money as strings, so this is lossless. A float would only
    appear if the SDK changed under us; routing it through ``str`` keeps
    ADR 0008 intact either way.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        msg = f"cannot interpret {value!r} as a decimal"
        raise BrokerError(msg) from exc


def _as_market_datetime(value: Any, session_date: date) -> datetime:
    """Normalise an SDK session time to an aware datetime in market time.

    Tolerates the three shapes the SDK has plausibly used: a naive datetime (what
    it produces today), an already-aware datetime, and a bare ``time``.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=MARKET_TZ)
        return value
    if isinstance(value, clock_time):
        return datetime.combine(session_date, value, tzinfo=MARKET_TZ)
    msg = f"cannot interpret {value!r} as a session time"
    raise BrokerError(msg)


def _enum_value(value: Any) -> str:
    """SDK enums render as ``AssetClass.US_EQUITY``; we want ``us_equity``."""
    inner = getattr(value, "value", value)
    return str(inner)


def _expect[M](value: Any, model: type[M]) -> M:
    """Narrow an SDK response to its model type.

    Every alpaca-py getter is typed as ``Model | dict[str, Any]`` because the
    client can be put into raw-data mode. We never do that, so a dict here means
    the SDK changed under us -- which should surface as a clear broker error, not
    an AttributeError three frames away.
    """
    if not isinstance(value, model):
        msg = (
            f"expected {model.__name__} from the Alpaca SDK, got {type(value).__name__}"
        )
        raise BrokerError(msg)
    return value


def _expect_each[M](values: Any, model: type[M]) -> list[M]:
    """Narrow a list of SDK responses to their model type."""
    if not isinstance(values, list):
        msg = f"expected a list from the Alpaca SDK, got {type(values).__name__}"
        raise BrokerError(msg)
    return [_expect(v, model) for v in values]


# -----------------------------------------------------------------------------
# Rate limiting
# -----------------------------------------------------------------------------


class TokenBucket:
    """Simple thread-safe token bucket.

    Priority calls (cancel, flatten) are never queued behind bulk reads, so they
    bypass the bucket entirely -- see docs/alpaca-integration.md section 7.
    """

    def __init__(self, per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE) -> None:
        self._capacity = float(per_minute)
        self._tokens = float(per_minute)
        self._refill_per_second = per_minute / 60.0
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._updated) * self._refill_per_second,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self._refill_per_second
                time.sleep(deficit)


# -----------------------------------------------------------------------------
# Error translation
# -----------------------------------------------------------------------------


def translate(exc: APIError) -> BrokerError:  # noqa: PLR0911
    """Map an SDK error onto our hierarchy.

    Many returns by design: this is a translation table, and flattening it into
    a dict or a chain of elifs would make the ordering (status first, then
    message heuristics) harder to follow, not easier.
    """
    status = getattr(exc, "status_code", None)
    message = str(exc)
    lowered = message.lower()

    if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        # 403 is overloaded: auth, entitlement, and buying power all use it.
        if "buying power" in lowered or "insufficient" in lowered:
            return InsufficientBuyingPowerError(message)
        if "subscription" in lowered or "not permitted" in lowered:
            return PermanentOrderRejectError(message)
        return AuthError(message)
    if status == HTTP_TOO_MANY_REQUESTS:
        return RateLimitedError(message)
    if status == HTTP_NOT_FOUND:
        return NotTradableError(message)
    if (
        status is not None
        and HTTP_SERVER_ERROR_MIN <= int(status) < HTTP_SERVER_ERROR_MAX
    ):
        return TransientBrokerError(message)
    if "wash trade" in lowered or "self-match" in lowered:
        return WashTradeBlockedError(message)
    if "not tradable" in lowered or "inactive" in lowered:
        return NotTradableError(message)
    if "insufficient" in lowered or "buying power" in lowered:
        return InsufficientBuyingPowerError(message)
    return PermanentOrderRejectError(message)


# -----------------------------------------------------------------------------
# Reader
# -----------------------------------------------------------------------------


class AlpacaBroker:
    """Read-only Alpaca access. Satisfies ``BrokerReader``."""

    def __init__(
        self, client: TradingClient, *, limiter: TokenBucket | None = None
    ) -> None:
        self._client = client
        self._limiter = limiter or TokenBucket()

    @classmethod
    def from_secrets(cls, secrets: Secrets, mode: Mode) -> AlpacaBroker:
        key, secret = secrets.credentials_for(mode)
        client = TradingClient(
            api_key=key.get_secret_value(),
            secret_key=secret.get_secret_value(),
            paper=mode is Mode.PAPER,
        )
        return cls(client)

    def _call(self, fn: Callable[[], T]) -> T:
        self._limiter.acquire()
        try:
            return fn()
        except APIError as exc:
            raise translate(exc) from exc

    # --- BrokerReader ---------------------------------------------------------

    def get_account(self) -> AccountSnapshot:
        account = self._call(self._client.get_account)
        return AccountSnapshot(
            account_number=str(getattr(account, "account_number", "") or ""),
            equity=to_decimal(getattr(account, "equity", None)),
            last_equity=to_decimal(getattr(account, "last_equity", None)),
            cash=to_decimal(getattr(account, "cash", None)),
            buying_power=to_decimal(getattr(account, "buying_power", None)),
            daytrading_buying_power=to_decimal(
                getattr(account, "daytrading_buying_power", None)
            ),
            multiplier=int(
                to_decimal(getattr(account, "multiplier", None), Decimal("1"))
            ),
            pattern_day_trader=bool(getattr(account, "pattern_day_trader", False)),
            daytrade_count=int(getattr(account, "daytrade_count", 0) or 0),
            shorting_enabled=bool(getattr(account, "shorting_enabled", False)),
            trading_blocked=bool(getattr(account, "trading_blocked", False)),
            account_blocked=bool(getattr(account, "account_blocked", False)),
            transfers_blocked=bool(getattr(account, "transfers_blocked", False)),
        )

    def get_clock(self) -> ClockSnapshot:
        clock = _expect(self._call(self._client.get_clock), Clock)
        return ClockSnapshot(
            timestamp=clock.timestamp,
            is_open=bool(clock.is_open),
            next_open=clock.next_open,
            next_close=clock.next_close,
        )

    def get_calendar(self, start: date, end: date) -> list[CalendarDay]:
        request = GetCalendarRequest(start=start, end=end)
        days = self._call(lambda: self._client.get_calendar(request))
        if not isinstance(days, list):
            msg = f"expected a calendar list, got {type(days).__name__}"
            raise BrokerError(msg)
        return [self._to_calendar_day(day) for day in days]

    @staticmethod
    def _to_calendar_day(day: Any) -> CalendarDay:
        """Attach the market timezone to Alpaca's session times.

        The SDK builds ``Calendar.open``/``.close`` by ``strptime``-ing the
        API's "09:30" against the session date, which yields a *naive* datetime
        whose wall clock is US/Eastern. Localising here rather than at the call
        site is what makes half-day handling automatic downstream -- the flatten
        schedule then reads a real instant instead of a wall-clock assumption.
        """
        session_date: date = day.date
        return CalendarDay(
            session_date=session_date,
            open_at=_as_market_datetime(day.open, session_date),
            close_at=_as_market_datetime(day.close, session_date),
        )

    def get_asset(self, symbol: str) -> AssetInfo:
        asset = _expect(self._call(lambda: self._client.get_asset(symbol)), Asset)
        return AssetInfo(
            symbol=str(asset.symbol),
            asset_class=_enum_value(getattr(asset, "asset_class", "")),
            tradable=bool(getattr(asset, "tradable", False)),
            status=_enum_value(getattr(asset, "status", "")),
            fractionable=bool(getattr(asset, "fractionable", False)),
            shortable=bool(getattr(asset, "shortable", False)),
            easy_to_borrow=bool(getattr(asset, "easy_to_borrow", False)),
        )

    def list_positions(self) -> list[PositionSnapshot]:
        positions = _expect_each(self._call(self._client.get_all_positions), Position)
        return [
            PositionSnapshot(
                symbol=str(p.symbol),
                qty=to_decimal(p.qty),
                avg_entry_price=to_decimal(p.avg_entry_price),
                market_value=to_decimal(getattr(p, "market_value", None)),
                unrealized_pl=to_decimal(getattr(p, "unrealized_pl", None)),
            )
            for p in positions
        ]

    def list_open_orders(self) -> list[OrderSnapshot]:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = _expect_each(
            self._call(lambda: self._client.get_orders(request)), Order
        )
        return [
            OrderSnapshot(
                order_id=str(o.id),
                client_order_id=str(getattr(o, "client_order_id", "") or ""),
                symbol=str(o.symbol),
                side=_enum_value(getattr(o, "side", "")),
                qty=to_decimal(o.qty) if getattr(o, "qty", None) is not None else None,
                filled_qty=to_decimal(getattr(o, "filled_qty", None)),
                order_type=_enum_value(
                    getattr(o, "order_type", None) or getattr(o, "type", "")
                ),
                status=_enum_value(getattr(o, "status", "")),
                submitted_at=getattr(o, "submitted_at", None),
            )
            for o in orders
        ]


# -----------------------------------------------------------------------------
# Feed probe
# -----------------------------------------------------------------------------

_FEED_NAMES: Final = {DataFeed.IEX: "iex", DataFeed.SIP: "sip"}


class AlpacaFeedProbe:
    """Confirms the configured data feed is actually entitled.

    A silent downgrade to a thinner feed is the kind of thing you discover from
    unexplained backtest/live divergence weeks later, so `doctor` asks directly.
    """

    def __init__(self, client: StockHistoricalDataClient) -> None:
        self._client = client

    @classmethod
    def from_secrets(cls, secrets: Secrets, mode: Mode) -> AlpacaFeedProbe:
        key, secret = secrets.credentials_for(mode)
        return cls(
            StockHistoricalDataClient(
                api_key=key.get_secret_value(),
                secret_key=secret.get_secret_value(),
            )
        )

    def probe(self, symbol: str, feed: DataFeed) -> None:
        request = StockLatestQuoteRequest(
            symbol_or_symbols=symbol, feed=_FEED_NAMES[feed]
        )
        try:
            self._client.get_stock_latest_quote(request)
        except APIError as exc:
            raise translate(exc) from exc
