# Testing

## 1. Layers

| Layer | Scope | Runs against | Speed |
|---|---|---|---|
| Unit | Pure logic: indicators, sizing, risk rules, order construction | Fixtures | Every commit |
| Property | Invariants that must hold for all inputs | Generated inputs (Hypothesis) | Every commit |
| Integration | Full engine with `SimulatedBroker` and recorded data | Fixtures | Every commit |
| Broker contract | `AlpacaBroker` against the paper API | Live paper account | Nightly / pre-release |
| Soak | Full system, unattended | Live paper account | Before every promotion |

## 2. Coverage requirements

- `risk/` and `execution/` — 90% line coverage, and every rejection path
  exercised by name.
- `broker/` — every error-translation branch covered.
- Strategies — a golden-file backtest test each.
- Everything else — 70%.

Coverage is a floor, not a goal. The risk manager tests that matter are the ones
asserting that a *bad* order is refused.

## 3. Tests that must exist

### Risk manager

- Each limit rejects at its boundary, and approves just inside it.
- Resizes compound and only ever shrink.
- PDT: the 4th same-day round trip is refused under each policy.
- Daily loss breach halts and cancels.
- A rejected intent never reaches the router (assert via a spy broker).
- Strategy `risk_overrides` cannot loosen an account limit.

### Order router

- The same intent submitted twice produces one order (deterministic
  `client_order_id`).
- A timeout followed by a retry does not double-fill.
- Restart mid-flight: an order already at the broker is adopted, not resubmitted.
- Extended-hours mode never produces a market order.
- Fractional quantities are never used with bracket orders or shorts.
- A position reversal closes first and only then opens the opposite side.

### Reconciliation

- Local-only position → adopted from broker, alert raised.
- Broker-only position → adopted, alert raised.
- Quantity mismatch → broker wins; kill switch trips under
  `strict_reconciliation`.
- Missed fill across a simulated reconnect is recovered.

### Data pipeline

- Bar aggregation on session boundaries, including early closes.
- A missing minute produces a synthetic flat bar, not a gap.
- Out-of-order and duplicate bars are dropped.
- Invalid OHLC relationships are rejected.
- Staleness blocks entries but permits exits.

### Property tests

- Portfolio quantity never goes negative for a long-only config.
- Gross exposure never exceeds the configured cap after any sequence of
  approved intents.
- Realized plus unrealized P&L is consistent with the fill sequence for any
  generated fill stream.

## 4. Fixtures

Recorded, committed, and never regenerated casually:

- A normal session for a liquid symbol.
- A gap-up open on news.
- A halt-and-resume.
- A half-day session.
- A session with a websocket disconnect and backfill.
- A thin symbol with sparse prints.
- A split and a dividend date.

New fixtures are captured with `python -m tradingbot record --symbols ... --date ...`.

## 5. Broker contract tests

These place real orders on a **paper** account. They are gated behind a marker
(`pytest -m paper`) and refuse to run if the resolved mode is live — the fixture
asserts `client.paper is True` and aborts otherwise.

Covered: submit and cancel a far-from-market limit order; submit a bracket and
verify legs; verify an extended-hours market order is rejected as expected;
verify the trade-updates stream delivers events for a filled order; verify
rate-limit backoff under a burst.

## 6. The paper soak

**Mandatory before any strategy or engine change reaches live.**

Duration: **20 consecutive trading days** of unattended operation.

Pass criteria:

- Zero unhandled exceptions.
- Zero reconciliation mismatches.
- Zero duplicate orders.
- Every session started and ended cleanly, with the EOD policy applied
  correctly, including on any half day in the window.
- Realized paper performance within a reasonable band of the backtest
  expectation for the same period — the point is not that it matches, but that
  a large unexplained divergence is investigated before real money is involved.
- At least one deliberate fault injected and survived: kill the process
  mid-session and confirm the restart reconciles exactly.

A failure resets the clock. Twenty days is long enough to cross an option
expiry, a month end, and usually one genuinely ugly session — which is the
point.

## 7. CI

On every push: lint, type-check (strict), unit + property + integration tests,
backtest reproducibility check.
Nightly: broker contract tests against paper, plus a full backtest of every
committed strategy with the golden-file comparison.

CI never holds live credentials. Only paper keys, and only for the nightly job.
