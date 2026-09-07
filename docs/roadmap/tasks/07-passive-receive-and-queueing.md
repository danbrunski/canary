# C07 — Keep passive receive and queue handoff off the event-loop bottleneck

Priority: medium. Dependencies: C05 for comparable measurements; C03 for loss
attribution. This task changes collection transport, not detector algorithms.

## Problem and code entry points

`collect_can()` in [can_collector.py](../../../src/collection/can_collector.py)
calls blocking `bus.recv(timeout=0.2)` in an async coroutine. The combined
CAN/OBD collector already runs its synchronous loop in a worker; the affected
standalone path is used when OBD is disabled or the legacy ELM327 collector
runs beside passive CAN.

[queueing.py](../../../src/collection/queueing.py) schedules one
`call_soon_threadsafe` callback per record. The bounded asyncio queue is
downstream of that callback backlog. [drain.py](../../../src/daemon/drain.py)
fans every record to sink queues; storage already batches up to 1024 records
with a short collection wait and uses a serialized asynchronous writer.

## Implementation checklist

- [ ] C07.1 Add an event-loop heartbeat test with an idle/sparse blocking
  fake bus to demonstrate the passive collector delays unrelated work.
- [ ] C07.2 Move blocking passive receive into an owned receiver thread or
  use readiness-driven receive for supported transports. Prefer one owned
  receiver loop over one thread-pool job per frame. Preserve virtual, SocketCAN,
  and any still-supported adapter behavior.
- [ ] C07.3 Preserve frame timestamps, ordering, malformed-frame accounting,
  reconnect policy, shutdown ownership, and the pure sniffer invariant. Stop
  must not leave a reader using a closed or rebuilt bus.
- [ ] C07.4 Measure per-record thread handoff and fanout overhead with C05/C10
  instrumentation. Implement bounded chunk handoff if it materially reduces
  overhead: one scheduled drain callback for a bounded producer buffer rather
  than an unbounded callback per record.
- [ ] C07.5 Bound and account for the producer buffer before the asyncio
  queue, expose its pressure/losses to C03, and preserve source ordering.
  Set explicit maximum batch size and latency; sparse telemetry must not wait
  indefinitely for a full batch.
- [ ] C07.6 Audit sink subscriptions. Avoid forwarding irrelevant raw CAN
  traffic to GPS/DTC/monitor sinks when their consumers reject it immediately.
  Retain required records and loss semantics; do not drop raw capture or
  validated model inputs merely to improve benchmark numbers.
- [ ] C07.7 Retain existing storage batching, writer serialization, atomic
  persistence, and capture transitions. Optimize duplicate decode/conversion
  work only after profiling identifies it and shared ownership is clear.

## Regression and acceptance checklist

- [ ] C07.8 Test idle/sparse/busy buses, stop during receive, reconnect,
  malformed frames, queue overflow, late callbacks during shutdown, and capture
  boundaries while a handoff batch is pending.
- [ ] C07.9 Verify the sniffer never calls `send`, raw order/timestamps remain
  intact, and accepted plus explicitly dropped records reconcile with injected
  input. Include the legacy ELM327-plus-passive configuration.
- [ ] C07.10 Extend CAN collector/queueing/daemon sink tests and measure
  event-loop p95/p99 delay at fixed workload before and after the change.
- [ ] C07.11 Acceptance: an idle CAN receive cannot block unrelated asyncio
  tasks for the receive timeout, all buffers have visible bounds, and any
  throughput improvement preserves required data and sparse-input latency.

## Handoff

- [ ] C07.12 Record the selected reader strategy, ownership/shutdown rules,
  measured benefit, and any optimization rejected for lack of benefit. Apply
  the shared validation gate.
