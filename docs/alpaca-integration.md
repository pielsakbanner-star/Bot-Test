# Alpaca Integration

Everything specific to the broker lives here so the rest of the codebase stays
generic. Verify details against the current
[Alpaca docs](https://docs.alpaca.markets) before implementing — API surfaces and
plan entitlements change.

## 1. SDK and endpoints

Use the official Python SDK, `alpaca-py`:

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
```

| Purpose | Base URL |
|---|---|
| Paper trading | `https://paper-api.alpaca.markets` |
| Live trading | `https://api.alpaca.markets` |
| Market data | `https://data.alpaca.markets` |

REST auth uses the `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY` headers; the SDK
sets these for you. **Paper and live keys are different keys.** Using a live key
against the paper URL fails — treat that as the safety property it is, and never
build a code path that "falls back" from one to the other.

`TradingClient(key, secret, paper=True)` selects the paper endpoint. The
`paper` flag is derived from validated config, never from a default.

## 2. Account fields the bot depends on

From `TradingClient.get_account()`:

- `equity`, `last_equity` — for daily P&L and drawdown limits.
- `buying_power`, `regt_buying_power`, `daytrading_buying_power`, `cash`.
- `pattern_day_trader` (bool), `daytrade_count` (int) — PDT enforcement.
- `trading_blocked`, `account_blocked`, `transfers_blocked`,
  `shorting_enabled` — checked at startup; any block halts the bot.
- `multiplier` — `1` cash/non-margin, `2` Reg-T margin, `4` PDT margin.

`multiplier == 1` means a cash account: the bot must refuse to run intraday
strategies to avoid good-faith violations.

## 3. Market data feeds

| Feed | Coverage | Plan |
|---|---|---|
| `iex` | IEX exchange only, ~2–3% of consolidated volume | Free |
| `sip` | Full consolidated tape | Paid (Algo Trader Plus) |

The free plan's historical SIP data is delayed (recent minutes withheld), and
IEX real-time bars can differ meaningfully from consolidated bars for thin
names. Consequence for this project: **develop and backtest on whatever feed you
have, but do not go live on a strategy whose edge is smaller than the IEX/SIP
difference.** Record the feed used in every backtest report.

Crypto data is a separate stream (`CryptoDataStream`) with no feed tiers.

## 4. Orders

### Types and time-in-force

- `market`, `limit`, `stop`, `stop_limit`, `trailing_stop`.
- TIF: `day`, `gtc`, `opg` (at the open), `cls` (at the close), `ioc`, `fok`.
- Order classes: `simple`, `bracket` (entry + take-profit + stop-loss),
  `oco`, `oto`.

Prefer **marketable limit orders** over pure market orders for anything but the
most liquid symbols: a market order in a thin book is how you discover slippage
the expensive way. Configure the aggressiveness in ticks/bps.

### Constraints that bite

| Constraint | Detail |
|---|---|
| Extended hours | Only `limit` + `time_in_force=day`, with `extended_hours=True`. Anything else is rejected. |
| Fractional / notional | Only `market` or `limit` + `day`. Not short-able. Bracket orders do not support fractional quantities. |
| Bracket orders | Not supported for extended hours or fractional quantities. |
| Shorting | Requires `shorting_enabled`; hard-to-borrow names may reject. Handle rejection as a normal outcome, not an error. |
| Wash trades | Alpaca rejects self-crossing orders (opposing sides same symbol resting simultaneously). Cancel before reversing. |
| Position reversal | Alpaca does not flip a position in one order. Close, wait for the fill, then open the other side. |
| Order quantity | Cannot exceed available buying power; check before submitting rather than letting the broker reject. |

### Idempotency

Every order carries a `client_order_id` the bot generates:

```
{strategy_id}-{symbol}-{bar_timestamp_epoch}-{intent_hash[:8]}
```

Deterministic from the inputs, so a retry after a timeout reuses the same id and
Alpaca rejects the duplicate rather than double-filling. On startup the router
queries recent orders and treats any id it would have generated as already done.

## 5. Trade updates stream

`TradingStream` delivers order lifecycle events. Handle at minimum:

`new`, `partial_fill`, `fill`, `canceled`, `expired`, `rejected`, `done_for_day`,
`replaced`, plus the stop/limit variants (`stop_price_updated` etc.).

Two rules:

1. **`fill` is the only event that changes a position.** `new` and `accepted`
   change nothing but working-order state.
2. **The stream can drop events across a reconnect.** Always reconcile after a
   reconnect rather than assuming continuity.

## 6. Clock and calendar

- `get_clock()` → `is_open`, `next_open`, `next_close`. Use it instead of local
  time zones; it also exposes broker-side clock skew.
- `get_calendar(start, end)` → session dates and hours, including early closes
  (day after Thanksgiving, Christmas Eve). Half days are a classic source of
  end-of-day-flatten bugs — schedule the flatten relative to `next_close`, never
  a hardcoded 15:55.

Crypto trades 24/7 and ignores the equity calendar entirely.

## 7. Rate limits and throttling

The basic plan allows on the order of **200 requests/minute per account** for the
trading API; data limits depend on the plan. Design so the steady state is
websocket-driven and REST calls are rare:

- Never poll for positions on a timer faster than the reconciliation interval.
- Never poll order status — use the trade-updates stream.
- Client-side token bucket in the broker adapter, with a **priority lane** so
  `cancel_all` and `close_all` are never queued behind bulk reads.
- On HTTP 429, honor backoff and alert if it happens more than twice a session —
  it means the call pattern is wrong.

Only **one websocket connection per account** is permitted for market data.
A second connection disconnects the first; this is the concrete reason two bot
instances on one account are unsupported.

## 8. Environment variables

```
ALPACA_PAPER_KEY_ID
ALPACA_PAPER_SECRET_KEY
ALPACA_LIVE_KEY_ID          # only present on the live host
ALPACA_LIVE_SECRET_KEY
ALPACA_DATA_FEED            # iex | sip
```

See [configuration.md](configuration.md) for how these are loaded and validated.

## 9. Error translation

The adapter maps SDK/HTTP errors into a small set the rest of the code handles:

| Bot error | Cause | Handling |
|---|---|---|
| `AuthError` | 401/403 | Fatal, halt |
| `RateLimited` | 429 | Backoff and retry |
| `InsufficientBuyingPower` | 403 with buying-power message | Reject the intent, log, continue |
| `NotTradable` | Asset inactive/non-tradable | Drop symbol for the session |
| `WashTradeBlocked` | Self-cross rejection | Cancel opposing order, retry once |
| `TransientBrokerError` | 5xx, timeouts | Retry with backoff, max 3 |
| `PermanentOrderReject` | Anything else | Reject, alert, do not retry |
