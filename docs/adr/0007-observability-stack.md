# ADR 0007 — structlog, prometheus-client, and Apprise

**Status:** Accepted

## Context

Requirement N-5 says every order must be traceable from the bar that triggered it
to the fill. Requirement F-12 says every signal, decision, order and fill is
journaled. The operator watches alerts rather than the process, so the alerting
path is part of the safety design, not a nicety.

## Decision

- **structlog** for structured JSON logging.
- **prometheus-client** exposing metrics on `metrics_port`.
- **Apprise** for alert delivery.

## Consequences

**Good.** structlog's context binding is what makes the correlation id practical:
bind it once when a bar is sealed and every downstream log line carries it, with
no threading of an argument through six call sites. Its processor chain is also
where secret scrubbing lives, so redaction is structural rather than something
each log call must remember. Prometheus is pull-based, so a scraper outage cannot
block the trading loop — which a push-based client could. Apprise turns the alert
channel into a config string, so switching from Discord to ntfy to email is a
config edit rather than a code change, and adding a second channel for critical
alerts is one more line.

**Bad.** JSON logs are unpleasant to read raw during development; the dev config
uses structlog's console renderer instead, which means the two environments
render differently. Prometheus means running a scraper if you want history and
dashboards, though the metrics endpoint is useful on its own with `curl`.
Apprise is a broad dependency for what could be a 20-line webhook POST.

**Rejected.** OpenTelemetry — the right answer for a distributed system, and
overhead we cannot justify for a single process. Sentry — useful, and can be
added later behind the same alert interface. A hand-rolled webhook — cheaper
today, but the second channel is where it starts costing more than Apprise.

**Consequence for testing.** Because alerting is a safety control, the alert
path is covered by tests: a tripped kill switch must produce a dispatched alert,
asserted against a fake Apprise transport.
