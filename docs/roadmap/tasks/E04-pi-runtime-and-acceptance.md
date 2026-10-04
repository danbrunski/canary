# E04 — Pi runtime, interfaces and standalone acceptance

Follow [E00](E00-second-brain-README.md). **The Pi is the required host for
the second brain and digital twin.** A laptop may accelerate development;
it is not a required operational component. E01–E03 and later E05 increments
are not finished until their applicable Pi acceptance passes here.

## Checklist

- [ ] **E04.1 — Integrate bounded streaming evaluation.**
  Feed acquisition-stamped arrived samples into a bounded consumer at the
  existing inference tick/scoring-child seam. Reuse the arrival/pairing
  patterns in `lambda_delivery_stream` and live-feature observers where
  appropriate. A 1 Hz publication tick must not turn input clocks into 1 s
  means. Preload the active profile/ROM outside the collector path, retain
  only bounded state, and account for pending, late and skipped evaluations.
  Publish stage timing and applicability in existing status payloads.
  **Accept:** the collector never awaits twin computation, evidence-store
  I/O or a language model; a stalled consumer cannot create an unbounded
  backlog. Missing/gapped state causes named unevaluability rather than
  inventing intermediate ECU state. Layering and state-dir checks pass.

- [ ] **E04.2 — Prove live/stored/replay parity and isolation.**
  Use one evaluator with adapters for live samples and stored acquisition
  clocks. Include changed arrival order, late data, stream gaps, restarts,
  sentinel/mode transitions and program/calibration changes. Read
  `vehicle_state` before live conclusions. Respect replay's existing
  engine-running refusal and isolate artifacts under `_replay-*` paths.
  **Accept:** identical accepted input tuples/state yield identical outputs,
  residuals, gates and counts on all paths. Replay does not mutate live
  evidence, learning state or references. Restart recovers memory while
  invalidating any transient ECU state that cannot be reconstructed.

- [ ] **E04.3 — Measure Pi cost before live enablement.**
  E02's proposed budget is **≤1 ms p99, ≤5 ms maximum additional computation
  per 1 Hz inference tick, ≤1 MiB incremental resident table/state**. Measure
  it on the Pi, including worst supported sample batches and transition
  handling. Measure each E03/E05 increment and the combined brain against
  actual remaining tick headroom; the twin budget is not a budget for every
  later subsystem. Record baseline/with-brain missed ticks, collector record
  loss, queue depths, CPU/RSS and input rates under matched load.
  **Accept:** tool-produced measurements meet the declared budget with no
  added missed ticks or collector record loss. Skipped evaluation is visible,
  never normal. A laptop benchmark cannot close this item. A failing budget
  requires implementation/scope work before enablement. **Pi required.**

- [ ] **E04.4 — Run heavier work locally when collection is inactive.**
  Route session evaluation, memory consolidation and candidate promotion
  through the existing bounded single-flight child/job mechanisms. Persist
  progress, expose queued/running/deferred/completed/failed state and yield
  when collection resumes. Share resource scheduling with the trip digest;
  do not assume parked or idling means collection stopped. Work that cannot
  finish in the UPS key-off window stays pending for the next powered idle
  window, with no laptop handoff requirement.
  **Accept:** a Pi job progresses and completes without a remote host; a
  resumed capture preempts/defers heavy work without collector loss, and a
  shutdown/restart neither loses the job nor duplicates its evidence/outcome.
  Missing held-out data remains pending rather than forcing promotion.

- [ ] **E04.5 — Serve the brain through the Pi's existing interfaces.**
  Add consistent HTTP/MCP access and dashboard views for current predictions,
  residuals, applicability, program/image/slot identity, investigations,
  evidence and job status. Present the ECU computation and measured engine
  response as distinct parts of the explanation. Use E01 templates; the
  Android phone is a client of the Pi, with no required inference runtime.
  Reuse current authentication, routing and UI patterns.
  **Accept:** the phone dashboard can explain a discrepancy, show the
  observed/predicted trace and evidence, retrieve a prior case, and display
  missing inputs/next measurement while internet and optional language
  hosts are unavailable. HTTP and MCP return the same computed facts.

- [ ] **E04.6 — Persist and recover the standalone brain.**
  Exercise per-vehicle stores and candidate checkpoints across daemon/Pi
  restarts, active-image changes and source outages. Use
  `scripts/restart_canary.sh` and preserve operator captures. Invalidate
  program-specific model state when the program changes; a calibration-only
  change selects new tables and invalidates affected predictions without
  automatically declaring RAM addresses moved.
  **Accept:** a recovered Pi retrieves the same historical evidence and
  current valid reference, reports unknown transient state honestly, and
  resumes eligible streaming work. All mutable artifacts remain outside the
  checkout; retention/replay cannot contaminate the training corpus.

- [ ] **E04.7 — Demonstrate the entire loop on the Pi and phone.**
  Run the committed known-answer cases on the Pi, then a qualified live
  drive and a later inactive-collection evaluation window. Exercise one
  retained investigation through recurrence, competing explanations, next
  measurement and outcome. Include an absent-input case and a retraction.
  Use a replayed/explicitly synthetic discrepancy where no real fault exists;
  do not claim it proves real-fault detection.
  **Accept:** a report identifies build/profile/ROM hashes, exercised regions,
  counts and budgets; the Pi independently computes, stores, retrieves and
  explains the result with only the phone dashboard. No required step calls
  a laptop, cloud service or LLM. Hardware gaps stay `[~]` with their next
  capture named. **Pi and vehicle required for the live portion.**
