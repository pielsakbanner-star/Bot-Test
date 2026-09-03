# ADR 0002 — Single-process asyncio event loop with an in-process event bus

**Status:** Accepted

## Context

The obvious alternative is a service-oriented layout — data ingester, strategy
runner, and execution service communicating through Redis or Kafka — which is
how larger trading systems are built.

## Decision

One process, one asyncio loop, an in-process event bus, CPU-bound work offloaded
to a thread pool.

## Consequences

**Good.** No broker infrastructure to operate, no cross-service serialization,
no distributed-state bugs, and no partial-failure mode where the execution
service is up while the data service is down and positions go unmanaged. Alpaca
permits only one market-data websocket per account anyway, so the ingester could
not be scaled out regardless. Debugging is a single stack trace.

**Bad.** No independent scaling or independent deployment; a crash takes down
everything; one Python process bounds throughput.

**Accepted because** the workload is one operator, one account, a universe in
the tens of symbols, and bars at 1 minute or slower. The crash concern is
addressed by making crash-recovery a first-class requirement (F-3, N-3) rather
than by distributing the system.

**Escape hatch.** Components communicate only through the event bus and the
`Broker` protocol, so replacing the in-process bus with a real one later is a
mechanical change rather than a rewrite.
