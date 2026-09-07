# C07 — Keep passive receive and queue handoff off the event-loop bottleneck

Priority: medium. Dependencies: C05 for comparable measurements; C03 for loss
attribution. This task changes collection transport, not detector algorithms.

## Problem and code entry points

`collect_can()` in [can_collector.py](../../../../src/collection/can_collector.py)
calls blocking `bus.recv(timeout=0.2)` in an async coroutine. The combined
CAN/OBD collector already runs its synchronous loop in a worker; the affected
standalone path is used when OBD is disabled or the legacy ELM327 collector
runs beside passive CAN.

[queueing.py](../../../../src/collection/queueing.py) schedules one
`call_soon_threadsafe` callback per record. The bounded asyncio queue is
downstream of that callback backlog. [drain.py](../../../../src/daemon/drain.py)
fans every record to sink queues; storage already batches up to 1024 records
with a short collection wait and uses a serialized asynchronous writer.

## Implementation checklist

- [x] C07.1 Add an event-loop heartbeat test with an idle/sparse blocking
  fake bus to demonstrate the passive collector delays unrelated work.
  _done 2026-09-06: heartbeat tests in `tests/test_can_collector.py` — blocking reference shape stalls a 10 ms ticker ≥ 100 ms (measured 590 ms max on an idle bus); new path < 100 ms (measured ~1 ms)._
- [x] C07.2 Move blocking passive receive into an owned receiver thread or
  use readiness-driven receive for supported transports. Prefer one owned
  receiver loop over one thread-pool job per frame. Preserve virtual, SocketCAN,
  and any still-supported adapter behavior.
  _done: `CanReader` — one named daemon thread per bus (`can-recv:<channel>`) looping `bus.recv`; virtual/SocketCAN/slcan untouched (only `recv` used). Readiness-driven `add_reader` rejected: SocketCAN-only fileno, no measurable gain._
- [x] C07.3 Preserve frame timestamps, ordering, malformed-frame accounting,
  reconnect policy, shutdown ownership, and the pure sniffer invariant. Stop
  must not leave a reader using a closed or rebuilt bus.
  _done: `msg.timestamp` preserved; single producer → FIFO; parse errors counted via `log_dropped_can_frame`; reconnect joins the reader (bounded) before teardown/rebuild; late reader after join timeout is a no-op; sniffer has no send path (test asserts `send` never called)._
- [x] C07.4 Measure per-record thread handoff and fanout overhead with C05/C10
  instrumentation. Implement bounded chunk handoff if it materially reduces
  overhead: one scheduled drain callback for a bounded producer buffer rather
  than an unbounded callback per record.
  _done: measured 20k records: per-record handoff 20,000 loop wake-ups / ~340 ms → `ThreadHandoff` 1 wake-up + 79 drains / ~8 ms; fanout 55 → 16 ms. Chunked handoff kept (threshold > 25% fewer callbacks). Timed producer batch rejected (adds sparse latency)._
- [x] C07.5 Bound and account for the producer buffer before the asyncio
  queue, expose its pressure/losses to C03, and preserve source ordering.
  Set explicit maximum batch size and latency; sparse telemetry must not wait
  indefinitely for a full batch.
  _done: `ThreadHandoff` bounded (capacity 4096, max_batch 256), `HandoffStats` accepted/dropped/high_water/capacity; buffer drops vs asyncio-queue drops attributed separately; surfaced as `telemetry_sinks.can_receive` via `HealthMonitor.observe_receive_path`; drain runs next loop turn so sparse input never waits._
- [x] C07.6 Audit sink subscriptions. Avoid forwarding irrelevant raw CAN
  traffic to GPS/DTC/monitor sinks when their consumers reject it immediately.
  Retain required records and loss semantics; do not drop raw capture or
  validated model inputs merely to improve benchmark numbers.
  _done: `TelemetrySink.accepts` source filters for gps (`gps`), dtc and monitor (`obd`) — each consumer already returned immediately on other sources; storage/broadcast/live unfiltered; fanout batches up to 256 per wake with order preserved._
- [x] C07.7 Retain existing storage batching, writer serialization, atomic
  persistence, and capture transitions. Optimize duplicate decode/conversion
  work only after profiling identifies it and shared ownership is clear.
  _done: `src/storage/` untouched; batching/writer/atomic persistence/capture transitions unchanged; decode dedup documented as deferred pending profiling._

## Regression and acceptance checklist

- [x] C07.8 Test idle/sparse/busy buses, stop during receive, reconnect,
  malformed frames, queue overflow, late callbacks during shutdown, and capture
  boundaries while a handoff batch is pending.
  _done: idle/sparse/busy, stop-during-receive, reconnect, malformed frames, overflow reconciliation, late callbacks, pending-chunk-at-close tests._
- [x] C07.9 Verify the sniffer never calls `send`, raw order/timestamps remain
  intact, and accepted plus explicitly dropped records reconcile with injected
  input. Include the legacy ELM327-plus-passive configuration.
  _done: sniffer never sends; order/timestamps intact (test sized to the burst, bound asserted separately); accepted + dropped == injected; legacy ELM327-plus-passive config test starts both collectors._
- [x] C07.10 Extend CAN collector/queueing/daemon sink tests and measure
  event-loop p95/p99 delay at fixed workload before and after the change.
  _done: collector/queueing/daemon/main tests extended (114 in the four files); event-loop max/p95/p99 before/after recorded in `docs/collection-receive-path.md` (idle 590/590/590 ms → 1.2/0.2/0.5 ms)._
- [x] C07.11 Acceptance: an idle CAN receive cannot block unrelated asyncio
  tasks for the receive timeout, all buffers have visible bounds, and any
  throughput improvement preserves required data and sparse-input latency.
  _done: idle receive gap ~1 ms vs 200 ms timeout; every buffer bound visible in `/status`; sparse-frame latency < 50 ms asserted._

## Handoff

- [x] C07.12 Record the selected reader strategy, ownership/shutdown rules,
  measured benefit, and any optimization rejected for lack of benefit. Apply
  the shared validation gate.
  _done: `docs/collection-receive-path.md` records strategy, ownership/shutdown rules, measurements, rejected optimisations, and links the C05 baseline. Gate green._
