# Data Pipeline

Bad data produces confident, wrong trades. This layer's job is to make data
problems *visible and blocking* rather than silent.

## 1. Sources

| Source | Use |
|---|---|
| `StockHistoricalDataClient.get_stock_bars` | Warm-up window, backtests |
| `StockDataStream` (websocket) | Live bars, quotes, trades |
| `CryptoHistoricalDataClient` / `CryptoDataStream` | Crypto equivalents |
| `TradingClient.get_calendar` / `get_clock` | Session boundaries |
| `TradingClient.get_all_assets` | Tradability, fractionability, shortability |

## 2. Warm-up

At startup, for every `(symbol, timeframe)` a strategy subscribes to, fetch
`max(warmup_bars) * 1.5` bars of history (the margin covers holidays and halts).
Strategies are not evaluated until the window is full.

The warm-up bars and the live bars must be **the same shape** — same timeframe
alignment, same adjustment, same feed. Mixing an adjusted historical series with
an unadjusted live series puts a phantom jump in the middle of your indicator
window on every dividend and split.

Fetch with:

- `adjustment="all"` for backtests (split- and dividend-adjusted),
- `adjustment="split"` (or `raw` with explicit handling) for live warm-up, since
  live bars are unadjusted — and document the choice in the backtest report.

## 3. Bar aggregation

Subscribe once per symbol at 1-minute resolution and build higher timeframes
locally. This keeps the subscription count and bandwidth low, and gives control
over bar-close timing.

Rules:

- Bars are aligned to the session start, not to the wall clock, so a 5-minute
  bar for equities runs 09:30–09:35, not 09:32–09:37.
- A bar is **sealed** when the first tick of the next interval arrives, or when
  the timer for `interval_end + grace` (default 2 s) expires — whichever comes
  first. Never seal on a timer alone if data may be late; never wait forever.
- A sealed bar with zero constituent 1-minute bars is emitted as a
  **synthetic flat bar** (`open=high=low=close=prev_close`, `volume=0`) and
  flagged `synthetic=True`. Strategies can ignore or skip them, but indicators
  do not get a hole.
- The final bar of the session is sealed at the close, even if short.

## 4. Data quality gates

Every incoming bar passes validation before publication:

| Check | Action on failure |
|---|---|
| `high >= max(open, close)` and `low <= min(open, close)` | Drop bar, log `ERROR`, mark symbol suspect |
| Timestamp not older than the last sealed bar | Drop (out-of-order duplicate) |
| Price move vs previous close > `max_bar_move_pct` (default 20%) | Emit but flag `suspect=True`; risk manager blocks new entries in that symbol |
| Zero or negative price | Drop, mark symbol suspect |
| Volume negative | Drop |

Three suspect bars in a session disable the symbol for the day.

## 5. Staleness watchdog

For every subscribed symbol the pipeline tracks `last_bar_at`. If a symbol goes
`stale_threshold_seconds` (default 90 during regular hours) past its expected
bar:

1. Mark the symbol stale.
2. The risk manager blocks new entries in it; exits remain allowed.
3. Alert if more than 25% of the universe is stale simultaneously — that is a
   connection problem, not a symbol problem.

Thin symbols legitimately print no trades for minutes. Set the threshold per
liquidity tier, or accept that low-volume names will flap and exclude them from
the universe. A halted symbol looks identical to a broken feed from the client
side; `get_all_assets` status and the absence of quotes for the whole universe
are the discriminators.

## 6. Reconnection

Websocket disconnects are routine, not exceptional.

```
attempt:  1    2    3    4    5    6+
delay:    1s   2s   4s   8s   16s  30s (capped, with jitter)
```

On reconnect:

1. Re-subscribe to the full universe.
2. **Backfill the gap** from the historical API for the interval between the
   last sealed bar and now, so indicator windows have no hole.
3. Clear stale flags only after a fresh bar arrives per symbol.
4. Reconcile positions and orders — trade updates may have been missed while
   disconnected.

If reconnection fails for `max_disconnect_seconds` (default 300), apply the
configured disconnect policy: `halt` (stop trading, keep positions) or `flatten`
(exit everything). `halt` is the default; `flatten` is appropriate for
high-frequency intraday strategies that should not hold unmanaged positions.

## 7. Storage

| Data | Store | Retention |
|---|---|---|
| Bars used for backtests | Parquet, partitioned by symbol/year | Indefinite |
| Live bars as received | Parquet, daily append | 90 days (for replay debugging) |
| Journal: signals, decisions, orders, fills | SQLite (v1) | Indefinite |
| Account equity snapshots | SQLite, 1/minute | Indefinite |

Recording live bars as received matters more than it looks: when a live trade
diverges from the backtest, the first question is always "did the strategy see
different data?", and without the recording you cannot answer it.

## 8. Backtest/live parity checklist

Run before promoting any strategy to live:

- [ ] Same timeframe alignment in both paths.
- [ ] Same adjustment policy, documented.
- [ ] Same feed (`iex` vs `sip`) — or the difference quantified.
- [ ] Backtest uses only bars available at decision time (no forward fill from
      later bars).
- [ ] Synthetic/flat bars handled identically in both.
- [ ] Session boundaries and early closes come from the calendar in both.
