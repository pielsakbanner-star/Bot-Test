# Operations

## 1. Deployment

**Development:** the operator's Windows workstation, paper mode only.

**Production: Linux.** Develop on Windows, deploy to a small always-on Linux
VPS. Three concrete reasons, not preference:

- `uvloop` does not exist on Windows, so the asyncio loop is slower there.
- Windows' Proactor event loop has its own subprocess and signal-handling
  quirks — friction you do not want anywhere near the crash-recovery path.
- systemd's `StartLimitBurst` / `StartLimitIntervalSec` give you the restart
  rate limit below for free. On Windows you build it yourself, and an unbounded
  crash loop can submit unbounded orders.

WSL2 gives dev/prod parity on the workstation. The production host needs:

- Wired network, or at minimum a connection you would not describe as "usually
  fine". A dropped connection during an open position is the failure mode that
  costs money.
- Automatic restart on crash with a restart *rate limit* — 3 restarts in 10
  minutes, then stop and alert.
- NTP time sync enabled. Bar alignment depends on the clock.
- Disk space monitoring — the journal and bar recordings grow.

Co-location or low-latency hosting is not needed. Nothing in the design is
latency-sensitive at the millisecond scale; correctness is the binding
constraint.

Run production from a container built against a committed `uv.lock`, or from a
virtualenv created with `uv sync --frozen`. Never resolve dependencies on the
live host. The live host holds only live credentials; the dev machine holds only
paper credentials. Do not put both key pairs anywhere.

## 2. Daily runbook

### Pre-market (T-30 minutes)

```bash
python -m tradingbot doctor --config config/live.yaml
```

Confirm: authentication, account not blocked, equity and buying power as
expected, PDT counter, no unexpected open positions from yesterday, data feed
entitlement, calendar for today (is it a half day?), disk and log rotation.

Check whether today is an early close or a scheduled market holiday. Check for
known events — CPI, FOMC, earnings for names in the universe — and decide
consciously whether to run. Wide-spread, high-volatility sessions are where
mean-reversion strategies discover their tails.

### Start

```bash
python -m tradingbot run --config config/live.yaml --mode live --i-understand-the-risk
```

Verify in the first minutes: warm-up completed for every symbol, both websockets
connected, reconciliation clean, no symbols flagged stale or suspect.

### During the session

Do not watch it continuously — that leads to intervention, and manual trades in
the same account break reconciliation. Respond to alerts. Check in at the open,
midday, and 15 minutes before the flatten window.

### Post-close

- Confirm the EOD policy executed: expected positions held, everything else flat.
- Read the daily summary alert: P&L, trade count, rejections, risk vetoes,
  errors.
- Compare realized fills against expected prices. Persistent negative slippage
  means the execution parameters need work.
- Note any manual intervention in a session log — future-you will need it when
  the journal does not explain something.

### Weekly

- Review risk vetoes: a limit that fires constantly is either mis-set or is
  telling you something true about the strategy.
- Reconcile bot P&L against the Alpaca statement.
- Re-run the strategy's backtest including the newest data and compare live
  results against backtest expectations for the same period. Divergence here is
  the earliest reliable warning that an edge has decayed.
- Check for `alpaca-py` and dependency updates; apply on paper first.

## 3. Monitoring

Metrics exported on `metrics_port`:

| Metric | Alert threshold |
|---|---|
| `bot_up` | Missing for 60 s |
| `data_last_bar_age_seconds{symbol}` | > 90 s during regular hours |
| `ws_connected{stream}` | 0 for > 30 s |
| `orders_submitted_total`, `orders_rejected_total` | Reject rate > 10% |
| `risk_vetoes_total{rule}` | Informational; spikes are worth a look |
| `order_latency_seconds` (bar close to submit) | p99 > 2 s |
| `account_equity`, `daily_pnl_pct` | Daily loss within 0.5% of the limit |
| `open_positions`, `gross_exposure_pct` | Above 90% of the configured cap |
| `reconciliation_mismatches_total` | Any |
| `unhandled_exceptions_total` | Any |

## 4. Alerting

Alerts go to a channel you actually see on your phone. Categories:

| Severity | Events | Expected response |
|---|---|---|
| **Critical** | Kill switch tripped, risk halt, reconciliation mismatch, auth failure, crash loop | Immediate — check the account in the Alpaca UI |
| **Warning** | Data disconnect, order reject streak, stale symbols, rate limiting, latency breach | Investigate before the next session |
| **Info** | Startup, shutdown, daily summary, individual fills (optional) | Read at leisure |

Turn fill alerts off after the first week unless you enjoy the noise. Alert
fatigue is how the critical one gets ignored.

## 5. Incident response

### The bot is placing wrong or unexpected orders

```bash
python -m tradingbot kill --config config/live.yaml    # cancels all; liquidates if configured
```

If the CLI is unresponsive, cancel and flatten in the **Alpaca web UI** — it is
the authoritative control and does not depend on your process being healthy.
Then kill the process. Do not restart until you know which strategy and which
bar produced the orders; the journal's correlation id gives you that.

### The process died with open positions

Positions and broker-side bracket legs remain live at Alpaca. Decide whether to
manage them manually or restart. On restart the reconciler adopts them; a
strategy will not recognize a position it has no state for, so
`orphan_position_policy` (`adopt`, `flatten`, or `ignore`) governs what happens.
Default is `flatten` in live mode — an unmanaged position with no exit logic is
worse than a small realized loss.

### Positions do not match the broker

Trust the broker. The reconciler adopts broker state and alerts. Investigate via
the journal: the usual causes are a missed trade-update across a reconnect, a
manual trade in the same account, or a partial fill handled incorrectly.

### Alpaca is down or degraded

You cannot exit through the API. Check Alpaca's status page. If positions are
open and the outage is prolonged, this is the risk you accepted when you sized
the position. The bot halts new entries automatically once data goes stale.

### Suspected key compromise

Revoke the key pair in the Alpaca dashboard immediately — that invalidates it
regardless of who holds it. Then flatten from the web UI, generate a new pair,
update the host's environment, restart, and review the journal for orders you
did not originate.

## 6. Key rotation

Quarterly, and immediately on any suspicion:

1. Generate a new key pair in the Alpaca dashboard.
2. Update the host environment or keystore.
3. Restart during a closed market with `doctor` first.
4. Revoke the old pair.
5. Record the rotation date.

## 7. Change management

- No code changes to a running live session. Stop, deploy, `doctor`, restart —
  outside market hours.
- Every change goes to paper for at least one full session first. Strategy logic
  changes go through the full soak in [testing.md](testing.md).
- Tag every live deployment; the journal records the git sha per session, so any
  trade can be traced to the exact code that produced it.
- Config changes are committed (minus secrets) so "what were the limits that
  day?" is always answerable.

## 8. Records

Keep, indefinitely: the journal database, monthly Alpaca statements, backtest
run directories for every deployed strategy, and the session log of manual
interventions. Consult a tax professional about reporting obligations in your
jurisdiction — wash-sale rules in particular interact badly with high-turnover
automated strategies, and that is a question for an accountant, not for this
document.
