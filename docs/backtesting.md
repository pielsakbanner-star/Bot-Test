# Backtesting

The backtester exists to *reject* strategies. A backtest that only ever confirms
your idea is not a test. Assume every result is optimistic until the paper soak
says otherwise.

## 1. How it works

The replay engine feeds historical bars through the same event bus, the same
strategy engine, and the same risk manager used live. Only two things are
swapped:

- `HistoricalDataSource` replaces the websocket stream.
- `SimulatedBroker` replaces `AlpacaBroker`.

If a strategy behaves differently in backtest and live, the cause is in one of
those two, not in the strategy — which is the point of the design.

## 2. Fill model

Defaults, all configurable:

| Order type | Fill assumption |
|---|---|
| Market | Next bar's open, plus slippage |
| Limit (marketable) | Next bar's open if it crosses the limit, else rest |
| Limit (resting) | Fills if the bar's range trades through the price, at the limit |
| Stop | Triggers if the bar's range crosses the stop, fills at stop plus slippage |
| Bracket legs | Evaluated at bar granularity; see the caveat below |

**Slippage:** `max(fixed_bps, spread_share * estimated_spread)` with
`fixed_bps = 2` and `spread_share = 0.5` by default. Scale it up for anything
outside large-cap liquidity.

**Commission:** Alpaca equities are commission-free, but that does not mean
costless. Model regulatory fees on sells (SEC and FINRA TAF), and for crypto
model Alpaca's spread-based pricing. Set them explicitly rather than to zero —
zero-cost backtests flatter high-turnover strategies enormously.

**The bar-granularity caveat.** With bar data you cannot know whether the stop or
the target was hit first when a bar's range spans both. The engine resolves this
**pessimistically** — the stop fills — and counts these bars in the report as
`ambiguous_bars`. If a meaningful share of your outcomes are ambiguous, the
backtest is not measuring your strategy; it is measuring an assumption. Move to
finer bars or a different exit design.

## 3. Sources of false confidence

Each of these is checked or bounded by the engine:

| Bias | Mitigation in the engine |
|---|---|
| Look-ahead | Strategies receive only sealed bars; the engine asserts on any access to a future index |
| Survivorship | Universe must come from a point-in-time membership file; using today's index constituents is flagged in the report |
| Adjustment mismatch | Adjustment policy recorded in the report and asserted against the live path |
| Overfitting | Walk-forward validation required before promotion (below) |
| Unrealistic size | `max_order_pct_adv` applies in backtest exactly as it does live |
| Ignored costs | Commission and slippage default to non-zero; zeroing them prints a warning banner in the report |
| Cherry-picked period | Report is refused for spans under 2 years or fewer than 100 trades |

## 4. Metrics

Every backtest emits:

**Returns** — total, CAGR, monthly table, best/worst month.
**Risk** — max drawdown (depth and duration), volatility, downside deviation,
95% VaR, worst 5 days.
**Risk-adjusted** — Sharpe, Sortino, Calmar.
**Trades** — count, win rate, average win/loss, profit factor, expectancy per
trade, average holding period, max consecutive losses.
**Costs** — total slippage, total fees, cost as a share of gross P&L.
**Exposure** — average gross/net, time in market, max concurrent positions.
**Hygiene** — ambiguous bars, synthetic bars, rejected-by-risk counts, warnings.

Read them in this order: **cost share**, then **max drawdown**, then
**expectancy**, then returns. A strategy whose gross edge is smaller than its
modelled costs has no edge, and no amount of Sharpe redeems it.

## 5. Walk-forward validation

Required before any strategy is promoted to a live config.

```
|--- train 12m ---|- test 3m -|
        |--- train 12m ---|- test 3m -|
                |--- train 12m ---|- test 3m -|
```

Parameters are optimized on each training window and evaluated **only** on the
following out-of-sample window. The reported result is the concatenation of the
test windows. Rules:

- Minimum 8 folds.
- Parameter stability matters as much as performance: if the optimal `fast`
  period jumps 10 → 50 → 15 across folds, the parameter is noise.
- Out-of-sample Sharpe below half the in-sample Sharpe means overfitting.
- Report the full parameter path, not just the final chosen values.

## 6. Reproducibility

Every run writes `runs/<timestamp>-<strategy>-<git_sha>/` containing config
snapshot, resolved parameters, data range and feed, adjustment policy, engine
version, RNG seed, per-trade log, equity curve, and the metrics report. Two runs
of the same directory's config must produce byte-identical metrics; a CI test
enforces this.

## 7. CLI

```bash
# Single backtest
python -m tradingbot backtest --strategy sma_crossover \
    --symbols SPY,QQQ --start 2022-01-01 --end 2024-12-31 --timeframe 5Min

# Parameter sweep (writes a grid report; does not choose for you)
python -m tradingbot sweep --strategy sma_crossover \
    --param fast=10,20,30 --param slow=50,100,200 --start 2022-01-01 --end 2024-12-31

# Walk-forward
python -m tradingbot walkforward --strategy sma_crossover \
    --train-months 12 --test-months 3 --start 2020-01-01 --end 2024-12-31

# Replay a recorded live session against the current code (regression check)
python -m tradingbot replay --session 2026-08-14
```

`replay` is the one to reach for when live results diverge from expectation: it
runs the recorded bars of an actual session through the current code and shows
whether the strategy would decide the same thing.

## 8. Promotion gate

A strategy may be added to a live config only when all of these hold:

- [ ] Backtest spans at least 2 years and includes a drawdown regime.
- [ ] At least 100 trades.
- [ ] Walk-forward out-of-sample Sharpe at least half the in-sample Sharpe.
- [ ] Costs modelled non-zero; cost share of gross P&L under 30%.
- [ ] Ambiguous-bar share under 10%.
- [ ] Max drawdown within the account's stated tolerance.
- [ ] Golden-file test committed.
- [ ] Paper soak completed per [testing.md](testing.md).
