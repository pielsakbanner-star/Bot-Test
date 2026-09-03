# ADR 0006 — SQLite journal, Parquet bar archive, DuckDB for analysis

**Status:** Accepted

## Context

The bot writes two very different kinds of data. The journal — signals, risk
decisions, orders, fills, equity snapshots — is small, transactional, and
queried by id and time range. The bar archive is large, append-only, columnar,
and scanned in bulk by the backtester. Candidates ranged from "one Postgres
instance for everything" to "flat files".

## Decision

- **SQLite via SQLAlchemy 2.0** for the journal, with Alembic migrations.
- **Parquet via PyArrow** for the bar archive, partitioned by symbol and year.
- **DuckDB** for ad-hoc analytical queries over that Parquet.

## Consequences

**Good.** SQLite's single-writer model is exactly our access pattern — one
process, one account ([ADR 0002](0002-single-process-event-loop.md)) — so its
main limitation costs us nothing. No database server to operate, back up, or
have go down mid-session. The journal is a file you can copy. Parquet keeps the
bar archive compact and column-scannable, and DuckDB queries it in place without
loading it into memory, so backtest research does not need an ETL step.

**Bad.** No concurrent writers, so a future multi-account deployment needs a
migration. No network access to the journal, so a remote dashboard would need an
export or a read replica. Parquet is immutable in practice, which makes
correcting a bad recorded bar a rewrite of that partition.

**Accepted because** every one of those limits is a scale problem we do not have
and may never have, and the operational simplicity is worth real money on a
system that must run unattended.

**Migration path.** All journal access goes through SQLAlchemy, so moving to
Postgres is a connection-string change plus a migration run. `journal_url` is
already a URL in config for this reason. The Parquet archive does not move —
Postgres would be a worse home for it than files.

**Explicitly not chosen:** TimescaleDB. It is the right tool for
multi-year tick data across thousands of symbols. Our archive is bars, tens of
symbols, and read by one process.
