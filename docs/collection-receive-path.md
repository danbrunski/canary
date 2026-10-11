# Passive CAN receive path and thread handoff (C07)

Handoff record for roadmap task C07 (`docs/roadmap/tasks/archive/07-passive-receive-and-queueing.md`).
Scope: the *standalone* passive sniffer (`src/collection/can_collector.py`,
used when `obd_source` is `disabled` or the legacy ELM327 collector runs
beside passive CAN), the thread-to-loop handoff every threaded collector
uses (`src/collection/queueing.py`), and the sink fanout in
`src/daemon/drain.py`. The combined Canable path
(`can_obd_collector.run_can_obd_loop` under `asyncio.to_thread`) was
already off the loop and is unchanged apart from adopting the new handoff.

## Reader strategy

**One owned receiver thread per bus** (`CanReader`, a named daemon
`threading.Thread`, `can-recv:<channel>`), looping `bus.recv(timeout)` and
converting each frame (`frame_to_record`) *in the thread*, then pushing it
into a `ThreadHandoff`. Not one thread-pool job per frame; not
`loop.add_reader`.

Readiness-driven receive was considered and not implemented: only SocketCAN
exposes a fileno, `slcan` and `virtual` do not, so it would have been a
second code path gated on transport with the thread path still required as
the default. The thread path gives identical behaviour on all three
transports the project runs (virtual in tests, slcan on the Canable,
SocketCAN on the Pi HAT) and removes the loop stall completely (numbers
below), so the extra path buys nothing measurable.

## Ownership and shutdown rules

- `collect_can()` owns exactly one `CanReader` for the bus it is given. It
  returns only after the reader was asked to stop **and joined** (bounded:
  `join_timeout`, default `2 * poll_interval + 1 s`), then the handoff was
  flushed and closed. `run_with_reconnect` runs `bus.shutdown()` in its
  `finally` *after* the attempt returns, so a bus is never shut down or
  rebuilt while a reader is inside `recv` on it, and a rebuilt bus always
  gets a fresh thread (tested: the old bus's `recv` is never called after
  its `shutdown`, thread idents are disjoint).
- Stop latency is unchanged: the reader notices `stop` within one
  `poll_interval` (its `recv` timeout). The owner waits on a `wake` event
  that fires for *either* stop-requested or reader-exited — waiting only
  on reader-exit let a wedged `recv` outlast the bounded join in the first
  cut; the late-reader test pins this.
- If the join times out (a `recv` stuck in kernel I/O, the WSL2 wedged-USB
  case), the owner logs `can reader thread did not stop within join
  timeout` and proceeds. The thread is daemon; whatever it eventually
  produces hits a **closed** handoff and is a counted no-op — nothing
  touches the queue and no exception is raised. The subsequent
  `bus.shutdown()` on a bus that thread may still hold is the same
  best-effort situation `record()` already documents.
- `ThreadHandoff.close()` is loop-thread only. It flushes the pending
  buffer to the queue **in order** (so a stop or reconnect boundary while a
  chunk is pending loses nothing), then flips `closed`: a late `push`
  returns `False` and counts as `dropped`; a late scheduled drain returns
  without touching the queue; there is no double delivery.
- Bus-level failure inside the reader (anything other than
  `ValueError`/`IndexError`) ends the thread, is re-raised from
  `collect_can`, and lands in the existing reconnect policy. Parser errors
  are counted (`parse_errors`), logged at bounded rate via
  `log_dropped_can_frame` (one line per read loop per
  `DROP_LOG_INTERVAL_SECONDS`, keyed by an `owner` so two live buses never
  suppress each other), and skipped — identical to `record()`.
- Sniffer invariant: `CanReader` has no send path. A test fake whose
  `send` raises is driven through busy, idle, stop and reconnect cases.

## Handoff design (C07.4 / C07.5)

`ThreadHandoff` replaces the per-record
`loop.call_soon_threadsafe(partial(put_nowait_or_drop, ...))` closure
(`threadsafe_enqueue` now returns one; call sites are unchanged because it
is callable).

- **Bounded** producer buffer: `capacity` (default 4096 records; a few
  seconds of a flooded ~1 kHz bus while the loop is busy). Overflow drops
  newest and counts; ordering of what survives is intact.
- **Coalesced wake-up**: a push schedules a drain callback only when none
  is pending. On an idle loop the drain runs on the very next turn, so a
  single sparse frame pays exactly the old latency (measured ~1 ms end to
  end from `recv` return to `queue.get`; test bound 50 ms). Under load, the
  loop's own backlog is what coalesces pushes — no timer.
- **`max_batch`** (default 256): records moved per drain callback; the rest
  reschedule with `call_soon` so other loop work interleaves.
- **`max_latency`** is structurally one event-loop turn, not a knob. A
  timed producer-side batch ("flush when 256 records or 20 ms") was
  rejected without implementing it: it can only *add* latency for sparse
  input (the thread would also need `recv(timeout=min(poll, remaining))`
  to honour the deadline), and it cannot reduce wake-ups below the
  pending-flag design, which already does one wake-up per loop turn's
  worth of pushes.
- **Accounting** (`HandoffStats`): `accepted`, `dropped`, `high_water`,
  `capacity`, `drains`, `max_batch`. `accepted + dropped` equals every
  record pushed. Queue-full drops stay with the `on_drop` counter
  (`QueueDropCounter`) — a different cause, kept attributable.
- **Drop logging is time-limited, not count-limited** (`DropLogThrottle`).
  One line per handoff per `DROP_LOG_INTERVAL_SECONDS` (1.0), the first drop
  of a burst immediate, each line summarizing its interval:
  `dropped_since_last`, a cumulative `dropped_total`, and a
  `dropped_by` breakdown of `source:channel` pairs capped at
  `DROP_LOG_BREAKDOWN_LIMIT` (5) plus `dropped_by_others`. `dropped_total` is
  always exactly the sum of that owner's `dropped_since_last` values, so it
  keeps accumulating across a collector restart, where the owner's own counter
  goes back to zero. The old
  every-100th-drop rule made log volume proportional to *loss* volume: the
  2026-09-17 window (19:24:50–19:28:10Z) dropped 150,114 records and wrote
  1,502 synchronous journald lines from inside the producer path, competing
  with the consumer that was already behind. Counters are untouched by this
  — `/status` loss numbers are exact — and the tail is never lost: the
  burst's last summary is flushed at the next drop-free observation (an
  accepted push, a drained buffer, a good frame) and forced at
  `ThreadHandoff.close()`.
- The passive path reports `CanReceiveStats` (`frames`, `parse_errors`,
  `queue_dropped`, `handoff`, `reconnects`, cumulative across reconnects)
  through `collect_can_from_config(on_receive_stats=...)`, wired by
  `default_collectors(can_receive_stats_callback=...)` to
  `HealthMonitor.observe_receive_path("can_receive", ...)`. It appears in
  `/status` under `telemetry_sinks.can_receive` with `dropped_records`
  (= `buffer_dropped + queue_dropped`), `buffer_high_water`,
  `buffer_capacity`, `parse_errors`, `batches`, `reconnects` — a named
  counter, never folded into `storage`, so C03 can attribute loss before
  the ingest queue.

## Fanout (C07.6 / C07.7)

- `run_telemetry_fanout` takes one awaited `get` then drains what is
  already queued (bounded by `FANOUT_MAX_BATCH = 256`) without yielding.
  Order is preserved; the per-record `asyncio.wait_for` (a task plus a
  timer handle each) was the dominant fanout cost.
- `TelemetrySink.accepts` is an optional per-sink filter applied in the
  fanout. Filters set in `drain_queue_with_broadcast`, each justified by
  the consumer rejecting on sight:
  - `gps` → `source == "gps"` (`GpsTracker.observe` returns `False` for any
    other source).
  - `dtc` → `source == "obd"` (`DtcState.observe` returns for any other
    source).
  - `monitor` → `source == "obd"` (`MonitorState.observe` returns for any
    other source).
  Storage, broadcast and live sinks are **unfiltered**. Storage batching,
  the serialized async writer, atomic persistence and capture transitions
  are untouched.
- Every sink's queue reports `queue_depth` (at the last observation) and
  `queue_high_water` (the monotonic max over them) under
  `telemetry_sinks.<sink>`. The fanout observes each sink's queue after
  every batch, so the mark is taken while a wedged consumer is still
  blocked — `queue_depth` alone reads 0 once the stall clears, which is how
  the C10 storage-stall scenario dropped 3,742 records behind a 256-deep
  queue that reported depth 0. Read the high-water mark, not the last
  depth, when attributing a drop.
- The daemon's writer publishes each chunk (Parquet write + fsync) on a
  background thread and keeps accepting rows meanwhile
  (`publish_in_background`, 2026-10-11). A cheap SD card stalled one fsync
  for 12.8 s, and with the publish inline the storage sink stopped draining
  for the whole stall: its queue dropped ~7,000 records a minute. Now a stall
  only grows the next chunk. `telemetry_sinks.storage` carries the publish
  side: `max_publish_ms` is the disk's worst stall (the `*_write_ms` fields
  time the sink's calls, which no longer wait on it), `publishes_deferred`
  counts flushes that waited behind a slow chunk, and `publish_waits` counts
  the buffer reaching its cap (20 chunks) and blocking as the inline writer
  always did. `publish_errors` / `last_publish_error`: a chunk write raised;
  its rows went back in the buffer for a retry. Explicit flushes (rotation,
  capture entry and exit, shutdown) still wait until everything is on disk.
- Decode/conversion dedup (the broadcast sink and storage sink both decode
  CAN frames) is **deferred**: not profiled here and ownership is shared
  between the dashboard stream and persistence; nothing in this task
  changes it.

## Measurements

The event-loop heartbeat harness below is separate from the C05 feature-stage
microbenchmark (`scripts/benchmark_live_pipeline.py`, workstation baseline in
[`data/analyses/2026-09-06-benchmark-live-pipeline-baseline.md`](../data/analyses/2026-09-06-benchmark-live-pipeline-baseline.md));
that report times `observe()` + `tick()` only and says nothing about receive
latency. The full-pipeline harness that joins both is task C10.


Laptop (WSL2), `asyncio` default loop, fake buses; script kept under the
session scratchpad, reproducible from the tests named below. "Before" is
commit `2726beb` (blocking `recv` in the coroutine, per-record
`call_soon_threadsafe`).

Event-loop heartbeat: an unrelated task ticking every 10 ms; gap = delay
beyond 10 ms.

| Workload | Before max | Before p95 / p99 | After max | After p95 / p99 |
|---|---|---|---|---|
| Idle bus, `poll_interval=0.2`, 2 s | 590.6 ms | 590.6 / 590.6 ms | 1.2 ms | 0.2 / 0.5 ms |
| Busy bus, 20k frames then idle, 1 s | 591.9 ms | 591.9 / 591.9 ms | 1.9 ms | 1.2 / 1.9 ms |

(The "before" gap exceeds one 200 ms `recv` because `asyncio.sleep(0)` after
each blocking call re-queued the collector ahead of the due timer, so
several blocking calls ran back to back.)

Thread handoff, 20k records pushed from a producer thread as fast as it
can (a synthetic ~2.5 M rec/s burst, far above any real bus):

| Path | Wall to deliver | Loop wake-ups (`call_soon_threadsafe`) | Delivered |
|---|---|---|---|
| Per-record (before) | 373.6 ms | 20 000 | 20 000 |
| `ThreadHandoff`, `capacity=20000` | 8.0 ms | 1 (+79 `call_soon` drains of ≤256) | 20 000 |
| `ThreadHandoff`, default `capacity=4096` | 10.7 ms | 5 | 19 400; `dropped=600`, `high_water=4096` |

The last row is the visible bound doing its job under a burst no real bus
can produce; on the truck the buffer never approaches it. Per-put loop
work (≈13 ms per 20k `put_nowait`) is the same on both paths — what goes
away is the wake-up per record (self-pipe write + handle allocation), which
is where the 47× wall-time gap comes from. The acceptance threshold for
keeping chunking was ">25 % fewer loop callbacks";
`test_thread_handoff_benchmark_reduces_loop_callbacks_from_a_thread`
asserts it and logs the figures on every run.

Fanout, 20k records to 5 sinks, all pre-queued: 55.5 ms before → 15.8 ms
after (batched `get_nowait` after one awaited `get`).

## Tests

`tests/test_can_collector.py` (C07 section): blocking-reference heartbeat
regression, idle-bus heartbeat, sparse-frame latency, busy-bus order and
timestamps, stop during receive, reconnect (reader replaced, closed bus
never read, cumulative stats), malformed frames, queue-overflow
reconciliation, late reader after join timeout, periodic stats.
`tests/test_queueing.py`: FIFO across threads, bound + drops + high-water,
close flushes in order then refuses, `max_batch` rescheduling, loss
attribution split, one wake-up per burst, the benchmark.
`tests/test_daemon.py`: `accepts` filter, cross-batch ordering, drain never
feeds raw CAN to GPS/DTC/monitor, `can_receive` in the health snapshot.
`tests/test_main.py`: legacy ELM327-plus-passive starts both collectors.
