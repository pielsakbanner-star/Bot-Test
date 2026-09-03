# Risk Management

The risk manager is the only component that can stop an order, and every order
goes through it. If you read one document in this set before enabling live mode,
read this one.

## 1. Decision interface

```python
@dataclass(frozen=True)
class RiskDecision:
    action: Literal["APPROVE", "RESIZE", "REJECT"]
    qty: Decimal | None      # set when RESIZE
    reason: str              # always populated, always logged
    rule: str                # which limit fired
```

Rules are evaluated in a fixed order. The **first** rule that rejects wins, and
resizes compound (each rule may only shrink the order, never grow it).

## 2. Limit hierarchy

Evaluated outermost first.

### Tier 1 — account halts (reject everything)

| Rule | Default | Behaviour on breach |
|---|---|---|
| `account_blocked` / `trading_blocked` | — | Halt, alert, exit |
| `max_daily_loss_pct` | 2% of start-of-day equity | Cancel open orders, flatten, halt for the day |
| `max_drawdown_pct` | 10% from peak equity | Halt, require manual re-enable |
| `kill_switch_engaged` | off | Halt |
| `data_stale` | > 60 s without an expected bar | Block new entries; exits still allowed |

The asymmetry in the last row is deliberate: when you are blind, you should
still be able to get out, just not get in.

### Tier 2 — portfolio limits (reject or resize)

| Rule | Default |
|---|---|
| `max_gross_exposure_pct` | 100% of equity (no leverage in v1) |
| `max_net_exposure_pct` | 100% |
| `max_open_positions` | 10 |
| `max_position_pct` | 10% of equity per symbol |
| `max_sector_pct` | 30% (requires a symbol-to-sector map; skip if unavailable) |
| `max_correlated_cluster_pct` | 40% for user-declared clusters (e.g. all semis) |

### Tier 3 — per-order limits

| Rule | Default |
|---|---|
| `max_order_notional` | 5% of equity |
| `max_order_pct_adv` | 1% of 20-day average daily volume — the liquidity guard |
| `min_price` | $5.00 (avoid sub-penny and low-float behaviour) |
| `max_spread_bps` | 30 bps; wider means skip the trade, not pay the spread |
| `allow_shorts` | false by default |
| `symbol_allowlist` / `denylist` | denylist wins |

### Tier 4 — regulatory

**PDT (Pattern Day Trader).** If equity is under $25,000 and the account is not
already flagged, the bot tracks day trades in a rolling 5-business-day window and
refuses any order that would be the 4th. Because a day trade is only identifiable
once the closing leg is placed, the check happens at *exit* time on a same-day
entry:

```python
would_be_day_trade = position.opened_session == current_session
if equity < 25_000 and daytrade_count >= 3 and would_be_day_trade:
    reject("PDT limit reached")
```

Config offers three policies:

- `strict` — reject the exit and hold overnight. Note this converts an intended
  day trade into an unintended overnight position, which is its own risk.
- `block_entries` — stop opening new same-day positions once `daytrade_count`
  reaches 2. **Recommended default**, because it prevents the situation rather
  than reacting to it.
- `ignore` — only valid when equity is at or above $25k, or the account is
  already flagged as PDT with margin.

## 3. Position sizing

Sizing runs before the limit checks; limits then trim the result.

**Default: volatility-targeted sizing.**

```
target_risk_dollars = equity * risk_per_trade_pct        # default 0.5%
stop_distance       = atr_multiple * ATR(14)             # default 2.0
qty                 = target_risk_dollars / stop_distance
```

This gives every position roughly the same dollar risk regardless of how
volatile the symbol is, which is the property you want when strategies span
different names.

Alternatives, selectable per strategy: `fixed_fractional` (fixed % of equity),
`fixed_notional`, `equal_weight` (equity / max_open_positions).

**Kelly and its fractions are deliberately not offered.** Kelly sizing is acutely
sensitive to the estimated edge, and backtest-estimated edge is almost always
overstated. If you want it, implement it as a strategy-level override and cap it
at quarter-Kelly.

Rounding: shares round **down** to whole units unless fractional is enabled and
the order type permits it. A size that rounds to zero is a reject, not a
one-share order.

## 4. Stops and exits

Every entry must declare its exit. The router refuses an entry intent with no
`stop_loss` unless the strategy sets `exit_managed=True` and registers a
time-based or signal-based exit.

- **Bracket orders** for straightforward entry/stop/target — the stop lives at
  the broker, so it survives the bot crashing. This is the recommended default.
- **Bot-managed stops** only when the logic is dynamic (trailing on a
  strategy-computed level). Understand the tradeoff: if the process dies, the
  stop dies with it. `emergency_broker_stop_pct` places a wide protective stop at
  the broker as a backstop for these.
- **Time stop:** close a position that has hit neither target nor stop within
  `max_holding_bars`.

## 5. End-of-day policy

| Policy | Behaviour |
|---|---|
| `flatten_all` | Close everything before the close. Safest; no gap risk. |
| `flatten_intraday` | Close positions tagged intraday; hold swing positions. Default. |
| `hold` | Hold everything overnight. Requires `overnight_exposure_pct` at or below 50%. |

The flatten is scheduled relative to `clock.next_close` minus
`flatten_lead_minutes` (default 10), so early closes are handled automatically.
Flatten uses marketable limits with a widening ladder, escalating to market at
`next_close - 2 minutes` — a passive limit that never fills is worse than paying
the spread.

## 6. Kill switch

Triggered by CLI (`python -m tradingbot kill`), a sentinel file, or automatically
on: daily loss limit, max drawdown, reconciliation mismatch under
`strict_reconciliation`, or three consecutive order rejections.

Sequence:

1. Set `halted = True` — the risk manager rejects everything from this moment.
2. `cancel_all_orders()`.
3. If `kill_liquidates`: `close_all_positions(cancel_orders=True)`.
4. Journal the trigger, alert, and keep the process alive in a read-only state
   so you can inspect it.

Re-enabling after an automatic trip requires deleting the halt sentinel by hand.
This is intentional friction: automatic recovery from a risk breach means the
same breach can repeat all day.

## 7. Configuration example

```yaml
risk:
  max_daily_loss_pct: 2.0
  max_drawdown_pct: 10.0
  max_gross_exposure_pct: 100.0
  max_open_positions: 10
  max_position_pct: 10.0
  max_order_pct_adv: 1.0
  min_price: 5.00
  max_spread_bps: 30
  allow_shorts: false
  pdt_policy: block_entries
  sizing:
    method: volatility_target
    risk_per_trade_pct: 0.5
    atr_period: 14
    atr_multiple: 2.0
  eod_policy: flatten_intraday
  flatten_lead_minutes: 10
  strict_reconciliation: true
  kill_liquidates: true
```

## 8. What this does not protect you from

Stated plainly, because the limits above can create a false sense of safety:

- **Gap risk.** A stop does not execute at your stop price when the market gaps
  through it overnight or on news. Only position size limits help here.
- **Overfitted strategies.** No risk limit rescues a strategy whose edge exists
  only in the backtest. That is what walk-forward validation and the paper soak
  are for — see [backtesting.md](backtesting.md).
- **Correlated blowups.** Ten positions that are all the same trade is one
  position. The cluster limit only works if you declare the clusters honestly.
- **Broker or venue outage.** If Alpaca is down, you cannot exit. Size on the
  assumption that this will happen at least once.
- **Your own intervention.** Manual trades in the same account confuse
  reconciliation. Use a separate account for discretionary trading.
