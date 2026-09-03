# ADR 0003 — Risk manager as a mandatory gate outside strategies

**Status:** Accepted

## Context

Risk limits could live inside each strategy (each sizes and caps its own
positions) or as a separate component every order must pass through.

## Decision

A separate `RiskManager`. Strategies emit *intents* with sizing hints; the risk
manager decides the actual quantity and can reject. There is no code path from a
strategy to the broker that bypasses it.

## Consequences

**Good.** Account-level limits — daily loss, drawdown, gross exposure, PDT — are
inherently cross-strategy and cannot be enforced correctly from inside a single
strategy. Adding a strategy cannot weaken the account's protections. All risk
logic is in one place with one test suite and one high coverage bar. Every
rejection is logged with a rule name, so "why didn't it trade?" is answerable.

**Bad.** Strategies lose direct control over exact position size, which makes
some sizing-sensitive designs awkward to express. Backtests must run the risk
manager to be realistic, so it sits in the hot path of every simulation.

**Mitigation.** `risk_overrides` let a strategy tighten its own limits; the
clamp guarantees they can only ever be stricter than the account settings.
