# Requirements

## 1. Purpose

Build a self-hosted trading bot that executes systematic strategies through
Alpaca on the operator's behalf, without manual intervention during the trading
session, while enforcing risk limits the operator sets in advance.

## 2. Scope

### In scope (v1)

- US equities and ETFs, long and short, via Alpaca Trading API.
- Crypto (`BTC/USD`, `ETH/USD`, …) as an optional second asset class — same
  engine, different calendar and no PDT constraints.
- Bar-driven strategies on 1-minute and above timeframes.
- Market, limit, stop, stop-limit, trailing-stop, and bracket orders.
- Backtesting on historical Alpaca bars with an explicit cost model.
- Paper trading as a first-class, default mode.
- Structured logging, metrics, and alerting to a channel the operator watches.

### Out of scope (v1)

- Options, futures, and forex.
- Sub-second / HFT strategies and order-book microstructure logic.
- Multi-broker routing or smart order routing across venues.
- A hosted web UI. Observability is via logs, metrics, and alerts.
- Managing capital for anyone other than the operator.
- Machine-learning model training in-process (a model may be *loaded* and used
  for inference; training happens offline).

## 3. Users

There is one user role: **the operator** — the account owner, who configures
strategies and limits, starts and stops the bot, and responds to alerts.

## 4. Functional requirements

| ID | Requirement |
|---|---|
| F-1 | Authenticate to Alpaca using key/secret pairs supplied via environment, never committed to the repo. |
| F-2 | Discover account state on startup: equity, buying power, cash, `pattern_day_trader`, `daytrade_count`, blocked flags. |
| F-3 | Reconcile local positions and open orders against the broker on startup and every N minutes thereafter. |
| F-4 | Subscribe to real-time bars, quotes and trades for the configured symbol universe. |
| F-5 | Maintain a rolling in-memory window of bars per symbol, warm-started from historical data so indicators are valid at the first live bar. |
| F-6 | Evaluate each enabled strategy on bar close and collect emitted intents. |
| F-7 | Pass every intent through the risk manager, which may approve, resize, or reject it with a logged reason. |
| F-8 | Translate approved intents into Alpaca orders with a deterministic `client_order_id`. |
| F-9 | Consume the trade-updates stream and update portfolio state on `new`, `fill`, `partial_fill`, `canceled`, `rejected`, `expired`. |
| F-10 | Enforce a configurable end-of-day policy: flatten all, flatten intraday only, or hold overnight. |
| F-11 | Expose a kill switch that cancels all open orders and optionally liquidates all positions, callable from CLI and triggered automatically by breach conditions. |
| F-12 | Persist every signal, decision, order, and fill to a durable store for post-hoc analysis. |
| F-13 | Run the same strategy code in backtest and live, differing only in the data source and broker implementation. |
| F-14 | Respect the market calendar: no equity orders outside session hours unless extended-hours trading is explicitly enabled. |
| F-15 | Provide a `doctor` command that validates config, credentials, clock skew, data entitlements, and symbol tradability without placing orders. |

## 5. Non-functional requirements

| ID | Requirement |
|---|---|
| N-1 | **Correctness over latency.** Target end-to-end bar-close → order-submit under 500 ms; correctness is never traded for speed. |
| N-2 | **Idempotency.** No code path may produce a duplicate order on retry. |
| N-3 | **Crash recovery.** A restart mid-session recovers full state from broker + local store without duplicating or orphaning positions. |
| N-4 | **Rate-limit safety.** Stay within Alpaca's per-account request budget with client-side throttling and backoff. |
| N-5 | **Observability.** Every order is traceable from the bar that triggered it to the fill, via a correlation id. |
| N-6 | **Secret hygiene.** Keys live only in the environment or an OS keystore; logs are scrubbed of key material. |
| N-7 | **Deterministic backtests.** Same inputs and seed produce identical results. |
| N-8 | **Test coverage.** Risk manager and order router require ≥90% line coverage; strategies require a golden-file backtest test. |

## 6. Assumptions

- Single operator, single Alpaca account, single running instance. Concurrent
  instances against one account are explicitly unsupported and guarded against
  by a lock file plus a `client_order_id` namespace check.
- The host has reliable power and network during market hours; degraded network
  is treated as an incident, not a normal condition.
- Market data comes from Alpaca. The free IEX feed is acceptable for
  development; SIP entitlement is assumed before live trading (see
  [alpaca-integration.md](alpaca-integration.md#3-market-data-feeds)).

## 7. Constraints

- **PDT rule.** Accounts under $25,000 equity are limited to 3 day trades per
  rolling 5 business days. The risk manager must track and enforce this, since a
  violation can restrict the account for 90 days.
- **Settlement.** Cash accounts are subject to good-faith violations; the bot
  assumes a margin account and refuses to run day-trading strategies on a cash
  account.
- **Extended hours.** Alpaca accepts only `limit` orders with `time_in_force=day`
  outside regular hours.
- **Fractional shares.** Supported only for market and day-limit orders, and not
  short-able. Sizing logic must round to whole shares when fractional is not
  permitted for that order type.

## 8. Acceptance criteria for v1

1. `doctor` passes against a live paper account.
2. A reference strategy backtests over ≥2 years with a full metrics report.
3. The same strategy runs unattended on paper for **20 consecutive trading days**
   with zero unhandled exceptions and zero reconciliation mismatches.
4. Kill switch verified: flattens a multi-position portfolio within 5 seconds.
5. A forced process kill mid-session, followed by restart, produces a state that
   exactly matches the broker.
