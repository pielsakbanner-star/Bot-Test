# Roadmap

Phases are ordered so that the safety machinery exists before anything can place
an order. Each phase has an exit criterion; do not start the next one until it
is met.

## Phase 0 — Foundations ✅ complete

- Project scaffold, `pyproject.toml`, lint/type/test tooling, CI.
- Typed config loading and validation.
- Structured logging with correlation ids and secret scrubbing.
- `doctor` command: credentials, account state, clock, calendar, entitlements,
  symbol tradability.

**Exit:** `doctor` passes against a paper account and prints a complete
pass/fail table. Nothing can place an order yet.

**Delivered.** `config.py` (Pydantic models, live-mode gates), `errors.py`,
`observability/logging.py` (structlog, correlation ids, redaction),
`broker/protocol.py` (the seam), `broker/alpaca.py` (read-only half, error
translation, token bucket), `doctor.py` (17 checks), the Typer CLI, and CI.
The order-submitting half of the `Broker` protocol is declared but deliberately
unimplemented -- `test_phase_0_adapter_cannot_place_orders` asserts it.

## Phase 1 — Read-only market data

- Historical client and warm-up backfill.
- Live websocket stream with reconnect and backfill.
- Bar aggregator, quality gates, staleness watchdog.
- Parquet recording of live bars.

**Exit:** the bot runs a full session, logs bars for the universe, survives a
forced disconnect, and produces a gap-free recorded series.

## Phase 2 — Risk and portfolio, still no orders

- Portfolio state from broker reads.
- Reconciler.
- Risk manager with the full limit hierarchy and sizing methods.
- Journal schema and writes.

**Exit:** in `--dry-run`, the bot evaluates a strategy and journals the orders it
*would* have placed, with risk decisions and reasons, for a full session. Review
that journal by hand before continuing.

## Phase 3 — Execution on paper

- `Broker` protocol and `AlpacaBroker`.
- Order router: idempotency, timeouts, repricing, brackets, error translation.
- Trade-updates stream and fill handling.
- Kill switch and EOD policies.

**Exit:** a full session on paper with orders placed and filled, clean
reconciliation, and a verified kill switch that flattens a multi-position
portfolio in under 5 seconds.

## Phase 4 — Backtesting

- Replay engine and `SimulatedBroker` with the cost model.
- Metrics and report generation.
- Sweep and walk-forward commands.
- Reproducibility test in CI.

**Exit:** the reference strategy backtests over 2+ years, walk-forward validates,
and produces byte-identical results across two runs.

## Phase 5 — Observability and hardening

- Metrics endpoint, alert dispatch, daily summary.
- Crash-recovery tests, fault injection, restart rate limiting.
- Deployment: service definition, log rotation, host runbook.

**Exit:** a deliberate mid-session process kill is detected, alerted, restarted,
and reconciled with no manual intervention.

## Phase 6 — Paper soak

- 20 consecutive trading days unattended on paper, per
  [testing.md](testing.md#6-the-paper-soak).
- Daily review; weekly backtest-vs-paper comparison.

**Exit:** all soak pass criteria met, with a written summary of divergences and
their explanations.

## Phase 7 — Live, small

- Separate live host, live credentials, live config.
- Start at the **smallest size that is still real** — position limits set so a
  total loss of the deployed capital would be an annoyance, not an event.
- Run at that size for at least one month before considering any increase.

**Exit:** one month live with no operational incidents and P&L behaviour
consistent with the paper period.

## Later

Ordered by usefulness rather than by how interesting they are to build:

1. **Per-strategy attribution and reporting** — knowing which strategy makes
   money is worth more than adding a strategy.
2. **Postgres journal + a small dashboard** once SQLite becomes limiting.
3. **Corporate-action handling** — splits and dividends adjusting held positions.
4. **Crypto as a second asset class**, exercising the 24/7 calendar path.
5. **Multi-account support** for genuinely independent strategy sleeves.
6. **Options**, which is a larger project than it appears: different risk model,
   Greeks, assignment, expiry handling.
7. **ML inference** with a model loaded from a registry, trained offline.

## Explicit non-goals

Stated so they do not creep in: HFT and order-book strategies, smart order
routing, a hosted web UI, managing anyone else's money, and any form of
"auto-tuning" that changes live parameters without a human promoting them
through the backtest and soak gates.
