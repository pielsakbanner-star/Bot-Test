# ADR 0008 — Decimal for money, numpy for series

**Status:** Accepted

## Context

Prices, quantities and P&L can be represented as `float`, `Decimal`, or scaled
integers. Meanwhile indicator computation over a rolling window wants contiguous
float arrays for speed. These two needs pull in opposite directions.

## Decision

- **`decimal.Decimal` for every monetary and quantity value** that crosses a
  boundary: config, intents, orders, fills, positions, P&L, and everything
  written to the journal.
- **`numpy.float64` arrays inside the bar window and indicator computation**,
  converted to `Decimal` at the point an indicator result influences an order.
- **Quantization is explicit.** Share quantities quantize to the asset's
  increment and round **down**; prices quantize to the tick size and round in the
  conservative direction for the side.

## Consequences

**Good.** Float arithmetic on prices produces position sizes that are subtly
wrong in ways unit tests do not catch, because the error is small until it is
compounded across a sizing calculation, a rounding step, and a comparison
against a limit. Using Decimal at the boundaries means the journal is exact and
reconciliation against Alpaca compares equal values rather than
nearly-equal ones. Keeping numpy inside the window preserves indicator speed
where precision genuinely does not matter — a 20-period SMA is an estimate
regardless.

**Bad.** Two numeric types in one codebase, and a conversion boundary that must
be respected. Decimal is slower, and mixing the types raises at runtime rather
than silently coercing.

**Mitigation.** `mypy --strict` makes the boundary a compile-time concern: a
`float` cannot reach a field typed `Decimal`. The conversion happens in one
place per direction, and a property test asserts that a round trip through the
journal preserves values exactly.

**Corollary.** Never compare money with `==` against a computed float, and never
construct a `Decimal` from a `float` literal — `Decimal("10.05")`, never
`Decimal(10.05)`. A lint rule enforces the second one.
