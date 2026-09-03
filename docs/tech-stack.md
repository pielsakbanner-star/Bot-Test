# Tech Stack

Python 3.12 with the official `alpaca-py` SDK. The rationale for the language
and broker SDK is in [ADR 0001](adr/0001-python-and-alpaca-py.md); the component
choices below are recorded in ADRs [0006](adr/0006-storage-sqlite-parquet-duckdb.md),
[0007](adr/0007-observability-stack.md), and [0008](adr/0008-numeric-conventions.md).

Versions in [pyproject.toml](../pyproject.toml) are **floors**, not pins. `uv`
resolves the real versions into `uv.lock`, which is what production installs
from. Verify the floors against current releases the first time you install.

## Runtime

| Choice | Why |
|---|---|
| **Python 3.12** | Stable and fully supported by every dependency here. Not 3.13 free-threaded — a system that moves money is the wrong place to be early on a runtime change. |
| **uv** | Environment and dependency management. Fast, and `uv.lock` gives the reproducible production install that N-7 (deterministic backtests) depends on. |
| **hatchling** | Build backend. Nothing exotic needed. |

## Broker and data

| Choice | Role |
|---|---|
| **alpaca-py** | REST trading API, market-data websocket, trade-updates websocket. First-party, so it tracks API changes. |
| **numpy** | The live bar window is a preallocated ring buffer, not a DataFrame — no reallocation on every bar. Also the indicator implementations. |
| **Polars** | Backtest and analysis dataframes. Faster and more memory-predictable than pandas on the bar volumes involved. |
| **PyArrow** | Parquet read/write for the bar archive. |
| **DuckDB** | Ad-hoc analytical queries over the Parquet archive without loading it into memory. |

**Indicators are hand-written in numpy.** SMA, EMA, ATR, RSI, VWAP and Bollinger
bands are roughly twenty lines each, and you need to control exactly how yours
behave at the warm-up boundary — that boundary is where backtest/live divergence
starts. Each is validated once against TA-Lib in a test, and after that the code
is yours. This also avoids compiling TA-Lib's C library on Windows.

## Config and validation

| Choice | Role |
|---|---|
| **Pydantic v2** | Typed config models. The startup validation table in [configuration.md](configuration.md#3-validation-at-startup) is a set of Pydantic validators. |
| **pydantic-settings** | Environment and `.env` loading, with the secret-field detection that drives log scrubbing. |
| **PyYAML** | Config file parsing. |
| **Typer** | CLI (`run`, `doctor`, `backtest`, `sweep`, `walkforward`, `replay`, `record`, `kill`). |

## Storage

| Choice | Role |
|---|---|
| **SQLite + SQLAlchemy 2.0** | The journal: signals, risk decisions, orders, fills, equity snapshots. Single-writer is exactly our access pattern. |
| **Parquet** (via PyArrow) | Bar archive, partitioned by symbol and year. |
| **Alembic** | Journal schema migrations, so a schema change doesn't cost you history. |

Postgres is a later migration, not a starting point — see
[ADR 0006](adr/0006-storage-sqlite-parquet-duckdb.md).

## Observability

| Choice | Role |
|---|---|
| **structlog** | JSON logs with the correlation id threaded bar → signal → intent → order → fill, plus the secret-scrubbing processor. |
| **prometheus-client** | The metrics in [operations.md](operations.md#3-monitoring), scraped on `metrics_port`. |
| **Apprise** | Alert fan-out. One integration covers Slack, Discord, ntfy, Telegram and email, so the alert channel is a config string rather than code. |

## Testing and quality

| Choice | Role |
|---|---|
| **pytest** + **pytest-asyncio** | Test runner; the engine is async throughout. |
| **Hypothesis** | The property tests in [testing.md](testing.md#3-tests-that-must-exist) — exposure caps and P&L consistency over generated fill streams. |
| **time-machine** | Clock control for session-boundary, early-close and EOD-flatten tests. |
| **respx** | HTTP mocking for broker error-translation tests without touching the network. |
| **pytest-cov** | Coverage floors. |
| **ruff** | Lint and format. |
| **mypy --strict** | Type checking. Non-negotiable here: the type system is what stops a `float` reaching a price field or a `None` position being treated as flat. |

## Deployment

**Develop on Windows, run production on Linux.** Concretely:

- `uvloop` does not exist on Windows, so the asyncio loop is slower there.
- Windows' Proactor event loop has its own subprocess and signal-handling
  quirks, which is friction you do not want in the crash-recovery path.
- systemd's `StartLimitBurst` / `StartLimitIntervalSec` implement the restart
  rate limit from [operations.md](operations.md#1-deployment) directly. Task
  Scheduler makes you build it yourself, and an unbounded crash loop can submit
  unbounded orders.

WSL2 gives dev/prod parity on your machine. Production is a small always-on VPS
running Docker under systemd. Nothing here needs a large host — the workload is
tens of symbols on 1-minute bars.

## What was rejected, and why

| Rejected | Reason |
|---|---|
| **backtrader** | Effectively unmaintained. |
| **zipline-reloaded** | Heavy, oriented toward daily US equity research. |
| **vectorbt** | Vectorized, so it cannot run the real event-driven risk manager — which defeats [ADR 0004](adr/0004-broker-protocol-shared-code-path.md). Fine as a separate research tool for parameter sweeps; not the backtester of record. |
| **TA-Lib** | C build on Windows, and we want to own the warm-up semantics. |
| **pandas-ta** | Thin maintenance for a dependency in the decision path. |
| **pandas** (live path) | DataFrame reallocation per bar; numpy ring buffer is the right shape. Polars covers the analysis side. |
| **Postgres/TimescaleDB (v1)** | Real operational cost for no benefit at single-operator scale. Revisit when SQLite hurts. |
| **Go / Rust engine** | Would split research (Python) from execution, reintroducing exactly the backtest/live drift ADR 0004 exists to prevent. |
| **LEAN / QuantConnect** | Would replace most of this design wholesale, including the risk gate we specifically want to own. |
