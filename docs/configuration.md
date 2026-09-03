# Configuration

Configuration comes from three layers, merged in this order (later wins):

1. Built-in defaults (in code, conservative).
2. YAML config file (`--config`).
3. Environment variables (secrets and host-specific overrides only).

Nothing is silently defaulted for anything that affects money. Missing risk
limits are a startup error, not a fallback to something permissive.

## 1. Secrets

Secrets never appear in YAML and never enter the repository.

```
# .env  (gitignored)
ALPACA_PAPER_KEY_ID=PK...
ALPACA_PAPER_SECRET_KEY=...

# live host only
ALPACA_LIVE_KEY_ID=AK...
ALPACA_LIVE_SECRET_KEY=...

ALPACA_DATA_FEED=iex
ALERT_WEBHOOK_URL=https://...
```

Rules the implementation enforces:

- The logger scrubs any value matching a known key pattern, and any config field
  whose name contains `key`, `secret`, `token`, or `password`.
- `mode: live` requires `ALPACA_LIVE_*`. Live keys are never read in paper mode
  and paper keys are never read in live mode — there is no shared code path that
  could pick the wrong one.
- On a shared or non-dedicated machine, prefer the OS keystore (Windows
  Credential Manager via `keyring`) over a `.env` file.
- Key rotation is a documented operational task, not an emergency procedure —
  see [operations.md](operations.md).

## 2. Config file schema

```yaml
# config/example.yaml
mode: paper                    # paper | live  (live also requires the CLI flag)

account:
  data_feed: iex               # iex | sip
  asset_class: us_equity       # us_equity | crypto

universe:
  symbols: [SPY, QQQ, AAPL, MSFT, NVDA]
  # or: source: sp100  /  source: file:./universe.txt

session:
  trade_regular_hours: true
  trade_extended_hours: false  # limit + day orders only if true
  warmup_multiplier: 1.5

strategies:
  - id: sma_crossover
    enabled: true
    class: tradingbot.strategies.sma_crossover:SmaCrossover
    symbols: [SPY, QQQ]
    timeframe: 5Min
    params:
      fast: 20
      slow: 50
      atr_mult: 2.0
    risk_overrides:
      max_position_pct: 5.0

risk:
  max_daily_loss_pct: 2.0
  max_drawdown_pct: 10.0
  max_gross_exposure_pct: 100.0
  max_net_exposure_pct: 100.0
  max_open_positions: 10
  max_position_pct: 10.0
  max_order_notional_pct: 5.0
  max_order_pct_adv: 1.0
  min_price: 5.00
  max_spread_bps: 30
  allow_shorts: false
  pdt_policy: block_entries     # strict | block_entries | ignore
  sizing:
    method: volatility_target   # volatility_target | fixed_fractional | fixed_notional | equal_weight
    risk_per_trade_pct: 0.5
    atr_period: 14
    atr_multiple: 2.0
  eod_policy: flatten_intraday  # flatten_all | flatten_intraday | hold
  flatten_lead_minutes: 10
  strict_reconciliation: true
  kill_liquidates: true

execution:
  default_order_type: limit
  limit_offset_bps: 5           # marketable-limit aggressiveness
  order_timeout_seconds: 60
  reprice_attempts: 2
  use_bracket_orders: true
  emergency_broker_stop_pct: 10.0

data:
  stale_threshold_seconds: 90
  max_bar_move_pct: 20.0
  reconnect_max_seconds: 300
  disconnect_policy: halt       # halt | flatten
  record_live_bars: true

reconciliation:
  on_startup: true
  interval_minutes: 15

observability:
  log_level: INFO
  log_format: json
  log_dir: ./logs
  metrics_port: 9090
  alerts:
    webhook_env: ALERT_WEBHOOK_URL
    on: [kill_switch, risk_halt, reconciliation_mismatch, order_reject_streak,
         data_disconnect, unhandled_exception, daily_summary]

storage:
  journal_url: sqlite:///./data/journal.db
  bars_dir: ./data/bars
```

## 3. Validation at startup

The bot refuses to start if any of these fail:

| Check | Why |
|---|---|
| Every risk limit present and within its allowed range | No permissive defaults for money |
| `mode: live` matched by the `--i-understand-the-risk` flag | Prevents accidental live runs from a copied config |
| Live mode uses a config file whose path differs from the paper config | Prevents one-character edits flipping environments |
| Credentials present for the selected mode, and authenticate | Fail before any strategy loads |
| Every strategy class importable and protocol-conformant | Fail fast on typos |
| Every symbol exists, is tradable, and matches the asset class | Catches delisted or misspelled tickers |
| Data feed entitlement matches config (`sip` requested but not entitled) | Catches silent downgrade to a thinner feed |
| `trade_extended_hours` implies limit/day orders only | Extended-hours market orders are always rejected |
| No other instance holds the lock file | One instance per account |
| Broker clock skew under 2 s | Bar alignment depends on it |

`python -m tradingbot doctor` runs exactly this list and prints a pass/fail
table without placing orders. Run it after every config change.

## 4. Paper vs live separation

| | Paper | Live |
|---|---|---|
| Config file | `config/paper.yaml` | `config/live.yaml` |
| Credentials | `ALPACA_PAPER_*` | `ALPACA_LIVE_*` |
| CLI | `--mode paper` | `--mode live --i-understand-the-risk` |
| Journal DB | `data/journal-paper.db` | `data/journal-live.db` |
| Log prefix | `[PAPER]` | `[LIVE]` |
| Alert channel | optional | required |

Four independent things must agree before a live order can be sent. That is
deliberate redundancy, and none of it should be refactored away for convenience.
