# Architecture Decision Records

Short records of decisions that are expensive to reverse, so the reasoning
survives the person who made it.

Format: context, decision, consequences. One file per decision, numbered,
never edited after acceptance — superseded records get a successor and a note.

| # | Decision | Status |
|---|---|---|
| [0001](0001-python-and-alpaca-py.md) | Python with the official `alpaca-py` SDK | Accepted |
| [0002](0002-single-process-event-loop.md) | Single-process asyncio event loop, in-process event bus | Accepted |
| [0003](0003-risk-manager-as-mandatory-gate.md) | Risk manager as a mandatory gate outside strategies | Accepted |
| [0004](0004-broker-protocol-shared-code-path.md) | Broker protocol so backtest and live share one code path | Accepted |
| [0005](0005-broker-side-brackets-by-default.md) | Broker-side bracket orders by default | Accepted |
| [0006](0006-storage-sqlite-parquet-duckdb.md) | SQLite journal, Parquet bar archive, DuckDB for analysis | Accepted |
| [0007](0007-observability-stack.md) | structlog, prometheus-client, and Apprise | Accepted |
| [0008](0008-numeric-conventions.md) | Decimal for money, numpy for series | Accepted |
