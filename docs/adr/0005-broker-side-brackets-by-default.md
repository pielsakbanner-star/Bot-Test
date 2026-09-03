# ADR 0005 — Broker-side bracket orders by default

**Status:** Accepted

## Context

Stop-losses can be managed by the bot (watch the price, submit an exit when
breached) or placed at the broker as bracket legs that rest on Alpaca's side.

## Decision

Default to broker-side bracket orders (`order_class=bracket`). Bot-managed exits
are opt-in per strategy via `exit_managed=True`, and those strategies get a wide
protective broker-side stop as a backstop (`emergency_broker_stop_pct`).

## Consequences

**Good.** The stop survives the bot crashing, the host losing power, and the
network dropping — which is exactly when you most need it. It removes the worst
failure mode in the system: an open position with no exit and no process to
manage it.

**Bad.** Bracket orders constrain what is possible: no fractional quantities, no
extended hours, and dynamic or indicator-based trailing logic cannot be
expressed as a static leg. Modifying a resting leg costs an API call, so
frequently-adjusted stops burn rate-limit budget.

**Consequence.** Strategies wanting dynamic exits accept a documented tradeoff
and always carry the wide backstop stop. The backstop is deliberately wide — it
exists to prevent catastrophe, not to be the strategy's actual risk control.
