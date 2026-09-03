# Strategy Framework

A strategy is a pure function of market state and portfolio state that returns
intents. It does not call the broker, does not size positions, does not know
about buying power, and does not know whether it is running in a backtest or
against the live account. Those constraints are what make one code path usable
in both places.

## 1. Interface

```python
class Strategy(Protocol):
    id: str
    symbols: list[str]
    timeframe: TimeFrame
    warmup_bars: int          # how much history before signals are valid

    def on_start(self, ctx: Context) -> None: ...
    def on_bar(self, bar: Bar, window: BarWindow, ctx: Context) -> list[Intent]: ...
    def on_fill(self, fill: Fill, ctx: Context) -> None: ...
    def on_stop(self, ctx: Context) -> None: ...
```

`Context` gives read-only access to the portfolio snapshot, the clock, config
params, and a logger bound to the strategy id. It exposes no method that mutates
anything outside the strategy.

`BarWindow` is a rolling, immutable view of the last `N` bars with cached
indicator helpers (`sma`, `ema`, `atr`, `rsi`, `vwap`, `bbands`). Indicators are
computed once per bar per symbol and shared across strategies.

## 2. Intent model

```python
@dataclass(frozen=True)
class Intent:
    strategy_id: str
    symbol: str
    side: Literal["buy", "sell"]
    kind: Literal["entry", "exit", "adjust"]
    # Exactly one sizing hint; the risk manager decides the final quantity.
    target_pct: Decimal | None = None    # % of equity
    target_qty: Decimal | None = None    # explicit shares
    close_position: bool = False         # exit the whole position
    # Execution preferences (advisory; the router may override)
    order_type: Literal["market", "limit"] = "limit"
    limit_offset_bps: int = 5
    time_in_force: str = "day"
    # Exit plan — required for entries unless exit_managed
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    max_holding_bars: int | None = None
    tag: Literal["intraday", "swing"] = "intraday"
    reason: str = ""                     # human-readable, journaled
```

`reason` is not decoration. When you are reading the journal three weeks later
trying to work out why the bot bought something, it is the field you will want.

## 3. Rules strategies must follow

1. **No look-ahead.** `on_bar` receives a *closed* bar. Referencing the current
   incomplete bar, or any future data, is the most common way to produce a
   backtest that cannot be reproduced live.
2. **No broker or network calls.** If a strategy needs external data, it is
   fetched by a data provider and injected through `Context`.
3. **Deterministic.** Given the same window and params, the same intents. Seed
   any randomness from config.
4. **State must be rebuildable.** Anything in `self` has to be reconstructible by
   replaying `warmup_bars`. This is what lets the bot restart mid-session.
5. **Bounded runtime.** `on_bar` should complete well inside the timeframe.
   Exceeding the budget disables the strategy for the session.
6. **Idempotent intents.** Emitting the same entry intent twice on consecutive
   bars must not build a double position — check `ctx.portfolio.position(symbol)`
   first.

## 4. Worked example

An ATR-stopped moving-average crossover. Not a recommendation — it is here
because it exercises every part of the interface.

```python
class SmaCrossover(Strategy):
    id = "sma_crossover"

    def __init__(self, symbols, fast=20, slow=50, atr_mult=2.0):
        self.symbols = symbols
        self.timeframe = TimeFrame.Minute(5)
        self.fast, self.slow, self.atr_mult = fast, slow, atr_mult
        self.warmup_bars = slow + 20

    def on_bar(self, bar, window, ctx):
        if len(window) < self.warmup_bars:
            return []

        fast_now,  slow_now  = window.sma(self.fast), window.sma(self.slow)
        fast_prev, slow_prev = window.sma(self.fast, offset=1), window.sma(self.slow, offset=1)
        atr = window.atr(14)
        pos = ctx.portfolio.position(bar.symbol)

        crossed_up   = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up and pos is None:
            return [Intent(
                strategy_id=self.id, symbol=bar.symbol, side="buy", kind="entry",
                target_pct=Decimal("5"),
                stop_loss=bar.close - Decimal(self.atr_mult) * atr,
                take_profit=bar.close + Decimal(self.atr_mult * 2) * atr,
                tag="intraday",
                reason=f"SMA{self.fast} crossed above SMA{self.slow}",
            )]

        if crossed_down and pos is not None and pos.qty > 0:
            return [Intent(
                strategy_id=self.id, symbol=bar.symbol, side="sell", kind="exit",
                close_position=True,
                reason=f"SMA{self.fast} crossed below SMA{self.slow}",
            )]

        return []
```

Note what the strategy does *not* do: it asks for 5% of equity and a stop level,
and leaves the share count, the buying-power check, the PDT check, and the order
type to the layers below.

## 5. Registration and config

```yaml
strategies:
  - id: sma_crossover
    enabled: true
    class: tradingbot.strategies.sma_crossover:SmaCrossover
    symbols: [SPY, QQQ, AAPL, MSFT]
    timeframe: 5Min
    params:
      fast: 20
      slow: 50
      atr_mult: 2.0
    risk_overrides:            # may only tighten account-level limits
      max_position_pct: 5.0
```

`risk_overrides` are clamped: a strategy can make its own limits stricter, never
looser than the account-level configuration.

## 6. Multiple strategies on one symbol

Allowed, with the netting rules made explicit up front:

- Positions are tracked **per account**, not per strategy — the broker has one
  position per symbol. Per-strategy attribution is journal-only bookkeeping.
- If two strategies emit opposing intents on the same bar, both are logged and
  **both are dropped**. Netting opposing signals into a smaller trade silently
  invents a position neither strategy asked for.
- Exit intents from a strategy that does not own the attributed position are
  rejected. This prevents one strategy closing another's trade.

If you want strategies to run genuinely independently, run them in separate
Alpaca accounts. Sharing one account means sharing one position.

## 7. Adding a strategy — checklist

1. Implement the protocol in `src/tradingbot/strategies/`.
2. Unit-test `on_bar` against fixture windows, including the warm-up boundary.
3. Backtest across at least two years including a drawdown regime.
4. Walk-forward validate (see [backtesting.md](backtesting.md)).
5. Add a golden-file test pinning backtest metrics so a refactor cannot silently
   change behaviour.
6. Run on paper for the full soak period in [testing.md](testing.md).
7. Only then enable it in a live config.
