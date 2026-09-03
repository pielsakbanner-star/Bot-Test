# Alpaca Trading Bot

An automated, event-driven trading system that executes strategies against the
[Alpaca](https://alpaca.markets) brokerage API. The bot ingests market data,
evaluates strategy logic, applies risk controls, and submits orders — with a
hard separation between **paper** and **live** trading.

> **Status:** design / documentation phase. No executable code has been written yet.
> This repository currently contains the specification the implementation will follow.

---

## Documentation map

| Document | What it covers |
|---|---|
| [Requirements](docs/requirements.md) | Scope, goals, non-goals, functional and non-functional requirements |
| [Architecture](docs/architecture.md) | Components, event flow, threading/async model, project layout |
| [Alpaca Integration](docs/alpaca-integration.md) | SDK usage, endpoints, order semantics, rate limits, gotchas |
| [Data Pipeline](docs/data-pipeline.md) | Historical + streaming bars, bar aggregation, storage, gap handling |
| [Strategy Framework](docs/strategy-framework.md) | Strategy interface, signal model, worked example |
| [Risk Management](docs/risk-management.md) | Position sizing, exposure caps, kill switch, PDT handling |
| [Configuration](docs/configuration.md) | Config file schema, environment variables, secret handling |
| [Backtesting](docs/backtesting.md) | Replay engine, cost model, metrics, walk-forward validation |
| [Operations](docs/operations.md) | Deployment, daily runbook, monitoring, alerting, incident response |
| [Testing](docs/testing.md) | Test strategy, fixtures, the paper-trading soak requirement |
| [Roadmap](docs/roadmap.md) | Phased delivery plan with exit criteria per phase |
| [ADRs](docs/adr/) | Architecture decision records |

---

## Quickstart (target developer experience)

The commands below describe the interface the implementation must provide.
They will not work until Phase 1 of the [roadmap](docs/roadmap.md) is complete.

```bash
# 1. Install
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env          # then fill in your Alpaca paper keys
cp config/example.yaml config/local.yaml

# 3. Verify connectivity and account state (read-only, no orders)
python -m tradingbot doctor

# 4. Backtest a strategy
python -m tradingbot backtest --strategy sma_crossover --start 2023-01-01 --end 2024-12-31

# 5. Run against the paper account
python -m tradingbot run --config config/local.yaml --mode paper

# 6. Live trading requires an explicit, separate opt-in (see docs/operations.md)
python -m tradingbot run --config config/local.yaml --mode live --i-understand-the-risk
```

---

## Core design principles

1. **Paper is the default.** Live mode requires a distinct config file, distinct
   API keys, and an explicit CLI flag. There is no way to reach live trading by
   omission or by a single edited boolean.
2. **The risk manager is the last gate.** Every order passes through it. It can
   veto, resize, or halt. A strategy cannot bypass it.
3. **Strategies are pure.** A strategy consumes market state and emits *intents*.
   It never calls the broker directly, which keeps it testable and backtestable
   against the same code path used live.
4. **Idempotent order submission.** Every order carries a deterministic
   `client_order_id` so a retry after a network failure cannot double-fill.
5. **Reconcile, never assume.** Broker state is the source of truth for positions
   and orders. Local state is a cache that is re-synced on every startup and
   periodically during the session.
6. **Fail closed.** On unrecoverable error — data gap, auth failure, repeated
   rejections — the bot flattens or halts per policy, rather than continuing to
   trade blind.

---

## Repository layout (planned)

```
.
├── README.md
├── pyproject.toml
├── .env.example
├── config/
│   ├── example.yaml           # committed template
│   └── local.yaml             # gitignored, yours
├── docs/                      # this documentation set
├── src/tradingbot/
│   ├── __main__.py            # CLI entrypoint
│   ├── config.py              # typed config loading + validation
│   ├── broker/                # Alpaca adapter behind a Broker protocol
│   ├── data/                  # historical + live market data
│   ├── strategies/            # strategy implementations
│   ├── risk/                  # risk manager, position sizing
│   ├── execution/             # order router, reconciliation
│   ├── portfolio/             # positions, P&L, exposure
│   ├── backtest/              # replay engine + metrics
│   └── observability/         # logging, metrics, alerts
└── tests/
    ├── unit/
    ├── integration/           # against Alpaca paper
    └── fixtures/              # recorded market data
```

---

## Risk notice

This software places real orders with real money when run in live mode.
Automated strategies can lose money quickly and in ways that are not obvious
during backtesting. Nothing in this repository is investment advice, and no
strategy included here is a recommendation. You are responsible for every order
your deployment submits. Read [docs/risk-management.md](docs/risk-management.md)
before enabling live mode, and treat the paper-trading soak period in
[docs/testing.md](docs/testing.md) as mandatory rather than optional.
