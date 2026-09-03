# ADR 0001 — Python with the official `alpaca-py` SDK

**Status:** Accepted

## Context

The bot needs an Alpaca client, market-data handling, indicator computation, and
a backtester. Candidate stacks: Python, TypeScript/Node, Go, Rust. Alpaca
publishes first-party SDKs for Python, Go, JS and C#.

## Decision

Python 3.11+ with `alpaca-py`.

## Consequences

**Good.** The data and quantitative ecosystem (pandas, numpy, pyarrow, scipy,
statsmodels) is where the analysis work actually happens, and keeping backtest
analysis in the same language as the live engine is what makes the shared code
path in [ADR 0004](0004-broker-protocol-shared-code-path.md) practical.
`alpaca-py` is first-party, so REST, websockets, and models track API changes.

**Bad.** Python is the slowest of the candidates and the GIL constrains
CPU-bound work. Accepted because the design is bar-driven at 1-minute-plus
resolution, where per-decision latency budget is milliseconds against a
60-second bar. A strategy that needs tick-level speed is out of scope per
[requirements.md](../requirements.md).

**Mitigations.** Pin dependencies with a lockfile; run strict type checking, as
dynamic typing around money is a real hazard; push CPU-bound indicator work to
numpy and a thread pool.
