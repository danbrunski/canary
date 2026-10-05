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

- [~] **E04.4 — Run heavier work locally when collection is inactive.**
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
  - *Laptop code, 2026-10-04 (revised after verification):*
    `src/control/brain_jobs.py` — `BrainJobStore` (append-only `jobs.jsonl`
    under `control/<slug>/brain/`, read incrementally by offset; job id = kind
    + canonical params; `running` reopens `deferred: interrupted_by_restart`)
    and `BrainScheduler`, whose job log, precheck, identity reconcile and
    transitions all run through `asyncio.to_thread` (only in-memory checks on
    the loop). One analysis child at a time: `src/control/child_pool.py` holds
    a brain child alive until its process has exited; `run_off_loop` answers
    `analysis_busy: brain_child_yielding` and asks it to yield; the digest
    waits off-loop, bounded by `CHILD_EXIT_BOUND_SECONDS` (reaper grace + 1 s),
    before spawning; the scheduler refuses a tick not above that bound.
    `collection_activity` treats only `no_data` with no capture/replay as idle.
    Kinds: `twin_session_evaluation` (`src/analysis/twin_evidence.py`, one E01
    record per resolved slot, content-deduped) and `memory_consolidation`.
    After each trip-end digest the daemon queues the twin's evaluation, off
    the loop, only when the session's capture manifest recorded the twin's
    command word (operator decision: option (c)). An open session stays
    `pending: session_open`. Tests `tests/test_brain_jobs.py` (15 pass) and
    `tests/test_analysis_child_registry.py` (4 pass): completes without a
    remote host (174 samples, `unvalidated`, blockers named); parked bus
    defers; a capture or a workbench request mid-run yields; restart reuses
    the same evidence ids; a 0.6 s precheck leaves the loop's heartbeat gap
    < 0.2 s; a real spawn child stays registered until its process exits; the
    digest starts only after the brain child exits; auto-enqueue skips plain
    drives and captures without the command word.
  - *Pi finding 2026-10-05, fixed:* `scripts/restart_canary.sh` mid-job left
    `failed: child_failed` (BrokenProcessPool): the stop signals the whole
    cgroup, so the child died before the scheduler saw the shutdown. Now a
    broken child is `deferred: daemon_shutdown` while stopping, otherwise
    `deferred: child_interrupted` with one automatic retry (`MAX_BREAKS = 2`);
    only a second consecutive break is `failed`. `recover()` gives a
    pre-existing `failed: child_failed` record its retry. Tests
    `test_a_restart_killing_the_child_defers_the_job_and_recover_reruns_it`,
    `test_an_outside_kill_gets_one_retry_and_a_second_is_a_failure`,
    `test_recover_reruns_a_child_failed_record_from_before_the_retry_rule`
    (real spawn child sent SIGTERM mid-job). Re-run the Pi restart demo.
  - *Remaining:* (1) Pi demonstration: a job completing on the Pi in a no_data
    window, a capture started mid-job with `/status` storage drops unchanged,
    and `scripts/restart_canary.sh` mid-job; (2) candidate promotion has no
    runner yet — E03 owns candidate learning; its held-out wait uses the same
    `pending` state.

- [~] **E04.5 — Serve the brain through the Pi's existing interfaces.**
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
  - *Laptop code, 2026-10-04:* one function per fact in
    `src/control/brain_reads.py`, served by `/control/brain/{status,memory,
    evidence/{id},investigations/{id}/card,twin/sessions/{session},jobs,
    jobs/{id}}` (`src/dashboard/routes/control/brain.py`, control token) and
    passed through by MCP `get_brain_status`, `query_memory`,
    `resolve_evidence`, `get_investigation_card`,
    `get_twin_session_evaluation`, `list_brain_jobs`, `get_brain_job`, plus
    `get_ecu_application_model` for E05.1's route. Cards split evidence into
    `ecu_computation` (twin, `labels.computation: ecu_command`) and
    `engine_response`. Dashboard *Diagnose → Investigations*
    (`panels/BrainPage.tsx`): identity/recovery, jobs (queue/state/reason),
    memory search, card with the two parts, unknowns and next measurement, twin
    trace (observed sparkline, predicted table, missing inputs, capture
    requirement). `tests/test_brain_routes.py::test_http_and_mcp_return_the_same_computed_facts`
    asserts HTTP == shared function == MCP for all eight; `BrainPage.test.tsx`
    (5 pass).
  - *Remaining:* (1) live current predictions/residuals/applicability need the
    streaming evaluator (E04.1); slot identity is per stored evaluation only;
    (2) phone demonstration on the Pi with internet off.

- [~] **E04.6 — Persist and recover the standalone brain.**
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
  - *Laptop code, 2026-10-04:* `src/control/brain_state.py` — `brain_dir`
    beside the memory ledger (`control/<slug>/brain/` under `state_dir`,
    `tests/test_brain_state.py::test_the_brain_directory_is_per_vehicle_state_under_state_dir`);
    `reconcile_identity` persists the program/calibration last run under. An
    image the model does not list is identified by hashing its W08 library
    bytes over the model's program spans (`image_program`): the same program
    hash is `calibration_changed` (predictions re-based, tables reselected,
    RAM `unchanged`), a different hash is `program_changed` (status
    `other_program`, program state invalid, RAM `reverify`), and only an image
    nobody holds is `program_unknown` (RAM `unverified`, never "moved"); twin
    evidence takes its `program_id` the same way. `recovery_report` reports
    `transient: unknown (not_reconstructible)`. Tests (11 pass): the real
    flash history (2026-10-04 23:02 is calibration-only); library images of
    one program differing only in a calibration byte, and in a program byte;
    an unheld image; a simulated restart reopens the same ledger records,
    evidence ids and completed job.
  - *Remaining:* (1) Pi: `scripts/restart_canary.sh` with a job and a capture
    open, then `GET /control/brain/status` and the same evidence ids;
    (2) candidate checkpoints (E03) and resuming streaming work (E04.1) do not
    exist yet; (3) source outages are only the job's `pending`/`failed`
    states, not exercised on hardware.

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
