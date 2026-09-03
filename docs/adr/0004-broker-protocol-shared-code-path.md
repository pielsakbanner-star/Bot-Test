# ADR 0004 — Broker protocol so backtest and live share one code path

**Status:** Accepted

## Context

Most hobby trading bots have two implementations: a backtest script and a live
runner. They drift, and the drift is discovered in production with real money.

## Decision

Define a `Broker` protocol (`submit`, `cancel`, `cancel_all`, `get_positions`,
`get_orders`, `get_account`, `close_position`, `close_all`) with two
implementations — `AlpacaBroker` and `SimulatedBroker`. Everything above that
layer is identical in both modes. The backtester swaps the broker and the data
source, and nothing else.

## Consequences

**Good.** What was backtested is what runs. Risk manager, order router,
portfolio accounting, and strategy code are exercised by every backtest, so the
integration test suite is effectively free. A live divergence can be replayed
against recorded bars to isolate whether the cause is the strategy or the
execution environment.

**Bad.** The protocol must be the intersection of what a simulator can model and
what Alpaca offers, so Alpaca-specific features are only usable through explicit
extension points. `SimulatedBroker` must model fills, partials, slippage, and
rejections faithfully enough to be worth trusting — that is real work, and a
sloppy simulator produces confident nonsense.

**Consequence for the fill model.** Because the simulator's assumptions now
determine backtest validity, they are documented and reported explicitly, and
ambiguous bar outcomes are resolved pessimistically and counted. See
[backtesting.md](../backtesting.md).
