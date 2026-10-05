# E04 — Pi runtime, interfaces and standalone acceptance

Follow [E00](E00-second-brain-README.md). **The Pi is the required host for
the second brain and digital twin.** A laptop may accelerate development;
it is not a required operational component. E01–E03 and later E05 increments
are not finished until their applicable Pi acceptance passes here.

## Checklist

- [~] **E04.1 — Integrate bounded streaming evaluation.**
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
  - *Laptop code, 2026-10-04:* `src/analysis/target_phi_stream.py`
    `TwinStream` decides each command sample with the stored evaluator's own
    `_assess` (so `predict` and the shared lookup kernel) over a bounded
    window, once every stream is read past its decision horizon (10.25 s for
    the Xterra profile: identity max age + skew; plus 2 s settle). Inputs pair
    by `match_ordered` against reads earlier batches left unused; a read at or
    before a time a decided record could cite is counted `late` and dropped;
    retained reads are evicted by data clock (lookback 10.25 s + 1 s) with a
    counted 20,000-per-channel cap. `target_phi.py` gained the shared pieces
    (`evaluated_specs`, `raw_specs`, `decision_horizon_s`, `lookback_s`,
    `new_candidate`, `Qualification`, incremental `Accounting`,
    `capture_requirement`; `_assess(captured=...)` so a gap in the window is
    stale, never "not captured"); the E02.3 fixture still reproduces record
    for record (`tests/test_target_phi.py` 76 pass). `src/daemon/twin_live.py`
    `LiveTwin`: a `record_observers` hook appends to a bounded intake (8,192
    reads; overflow counted `intake.dropped`); the 1 Hz tick hands the intake
    to one dedicated worker thread and never waits (`busy_ticks` while it
    runs); the worker loads the profile definition, resolves identity at the
    session's identity segments and slot tables through the resolver the
    stored brain job also uses (`src/control/twin_identity.py`
    `TwinResolver`, below), routes each read to the segment its own time
    falls in, flushes a segment when the data clock passes its end, and
    flushes a session's streams on a session change (counted).
    Published as `/status` `detectors.inference.twin` (result, applicability,
    blockers, accounting, pending/late/dropped/busy, identity, transient,
    latest prediction + 120 recent, `vehicle_state`, reached and finished
    segments with per-slot accounting or refusal) with stages `twin_tick`,
    `twin_eval` and `twin_identity` (identity resolution timed apart), and as `/control/brain/status` `live_twin` (MCP
    `get_brain_status` passes it through) and the BrainPage status rows.
    Writes nothing. Off by default: `twin_live.enabled` (config). Tests
    `tests/test_twin_live.py` (60 pass): stalled worker -> 1 hand-off, 19 busy
    ticks, intake held at capacity with drops counted, < 2 s for 2,000 reads;
    a blocking preload leaves two ticks < 0.2 s; gaps are
    `map_identity_unbracketed` with no prediction; status carries both stage
    timings; off by default says why; `test_layering.py`/`test_state_dir.py`
    pass. Laptop cost (worker thread, `.venv`, Oct 4 slice through the live
    path, 1,510 ticks): eval p50 0.414 ms, p99 0.959 ms, max 1.796 ms;
    observer 3.0 µs/record. Synthetic 8x read density (first 6 min): p99
    1.754 ms, max 2.571 ms, 546 retained reads (load average ~7). Re-measured
    after the E04.2 revision at load average 14-20: p50 1.3-1.7 ms, p99
    3.7-4.8 ms (8x: p99 3.9-8.2 ms), identity check median 0.02 ms after the
    first resolution. These two laptop runs differ by load, not code; neither
    is a budget figure. In a desk daemon replaying at 10x (below), `twin_eval`
    median 1.1-2.3 ms, p95 1.7-15.6 ms; `twin_identity` median 34-64 ms
    before per-session dating was cached; loop-side `twin_tick` median
    0.1 ms but p95 6-16 ms, max 32 ms.
  - *Remaining:* E04.3's Pi measurement (laptop numbers do not close it), then
    enabling `twin_live.enabled` on the Pi. The laptop p99 already exceeds
    the proposed 1 ms, so E04.3 must measure the real `tuning`/`ram-study`
    read rates. It should also say whether the loop-side `twin_tick` p95
    (6-16 ms in the desk daemon) is GIL contention with the worker; that is
    not established.

- [~] **E04.2 — Prove live/stored/replay parity and isolation.**
  Use one evaluator with adapters for live samples and stored acquisition
  clocks. Include changed arrival order, late data, stream gaps, restarts,
  sentinel/mode transitions and program/calibration changes. Read
  `vehicle_state` before live conclusions. Respect replay's existing
  engine-running refusal and isolate artifacts under `_replay-*` paths.
  **Accept:** identical accepted input tuples/state yield identical outputs,
  residuals, gates and counts on all paths. Replay does not mutate live
  evidence, learning state or references. Restart recovers memory while
  invalidating any transient ECU state that cannot be reconstructed.
  - *Laptop, 2026-10-04 (revised after verification):* one evaluator
    (`_assess`) and one identity/table resolver
    (`src/control/twin_identity.py` `TwinResolver`), each with two adapters:
    stored (`evaluate_session`, the brain job's `_run_twin`) and live
    (`TwinStream`, `LiveTwin`).
    - *Identity, one rule:* a capture manifest's revision wins (a
      disagreeing history is flagged `conflict`). Otherwise the flash
      history applies at the session's first stored sample
      (`session_dated_at`), with a new segment at each flash instant inside
      the data. The stored path bounds segments by the session's last
      sample and evaluates each as a windowed `evaluate_session(window=)`
      minted under its own subject (`:seg-<calibration>`; single-segment
      subjects are unchanged).
    - *Program and slots:* the program comes from the image, hashed once
      per image. The table loader is the only refusal on both paths
      (`wrong_program`/`program_unverified`). Every committed slot gets its
      own table or named refusal. `brain_jobs._twin_tables` became
      `twin_identity.twin_tables`.
    - *Causal rule:* "captured" means a read at or before `t + horizon`, and
      the coolant source is the highest-priority source with a read at or
      before `t + max_age`. Both are per sample (`captured_by`,
      `coolant_source`), so tick timing and future reads cannot change a
      decision.
    - *Replay:* `collection/replay.py` now carries each row's unshifted
      `acquired_at` (`REPLAY_SOURCE_ACQUIRED_KEY`); its per-row rebase
      includes pacing jitter. The twin dates, segments and pairs a replay on
      its recorded instants.
    - *Tests* (`tests/test_twin_live.py`, 60 pass):
      - The October 4 slice through a real `LiveFeatureSource` observer
        gives all 174 stored records field for field. Accounting, coverage,
        capture requirement, blockers, provenance and per-input paired
        counts are identical. This holds at 0.25/1/5 s ticks, with arrival
        shuffled inside 1.5 s blocks, on all 28 named-state cases, and for
        eligible, miss, sentinel, mode-transition and competing-sample
        sessions.
      - The slice run through `collection.replay`'s own `_iter_rows` +
        `replay_record` (10-20x, jittered) into a twin on the real resolver
        equals the stored brain job (`_run_twin` through the scheduler):
        same identity (ZS44A-20260920 at the session's own time, not emit
        time), segments, records, accounting and slot refusals, with no
        late reads.
      - Late reads are counted and equal stored-over-accepted. A gap is
        named `map_identity_unbracketed`. A restart equals stored over the
        reads after it, with `transient: unknown_before`.
      - A flash at 38 s is two segments; each equals the stored windowed
        evaluation on its own table. A flash recorded mid-session is picked
        up at the default 60 s recheck, flushing the old segment.
      - A refused program withholds its segment by name.
      - A DID coolant appearing at 25 s gives the same per-sample source at
        every tick rate, live and stored. An input first read mid-session is
        `input_not_captured` on both paths.
      - The real registry splits at the 23:02 flash only inside the span.
      - Replay is scoped to `_replay-<id>` and writes no file. The live twin
        mints no evidence.
    - *Desk daemon, 2026-10-05 04:45-05:00Z (laptop clock):*
      - Setup: `python -m src.main` on a scratch `state_dir`/storage
        (`twin_live.enabled`, `--profile nissan-xterra-2008`). The inputs
        were the slice as `2026-10-04T204557` (pre-flash) and a synthetic
        copy re-dated +24 h as `2026-10-05T204557` (post-flash; no real
        post-flash twin capture is on this laptop).
      - `POST /control/replay` at speed 10 (`loop: false`); `/status`
        `detectors.inference.twin` was read after stopping the replay.
      - Pre-flash: ZS44A-20260920 / registry / `nissan-1ZS44A-uprev-osiris`.
        Post-flash: ZS44A-20261004. Both: 174 candidates, all unevaluable
        (`input_stale:load` 120, `input_stale:rpm` 18,
        `map_identity_unbracketed` 15, `sentinel` 21), `duplicate_use` 0,
        4,719 rows emitted, 0 dropped.
      - The stored job (`_run_twin` in-process on the same config) gives
        identical calibration, source, program and accounting for both.
      - The daemon's own `POST /control/brain/jobs` first failed
        `child_out_of_memory` (4096 MiB address-space ceiling). The cause was
        a torch import reaching the brain child through candidate_promotion
        (VmSize 4.9 GB before any work); E03.6 moved it out. Re-run with the
        real `BrainScheduler`, the default spawn pool and the 4096 MiB cap,
        the twin job completes: VmPeak 2.80 GB, VmHWM 250 MB, no heavy
        modules loaded.
      - No checkout file changed.
  - *Behaviour change:* `EVALUATOR_VERSION` 2 → 3 (causal `captured_by` /
    per-sample `coolant_source`, `coolant_sources` list, windowed segments),
    so twin evidence minted before and after carries different versions.
  - *Remaining:* the same replay comparison on the Pi (E04.3/E04.7). A truly
    post-flash capture with the twin's channels, when one exists.

- [~] **E04.3 — Measure Pi cost before live enablement.**
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
  - *Replay measurement on the Pi, 2026-10-05 05:44–06:11Z (partial):* main at
    `a03095a`, `twin_live.enabled: true` set temporarily in
    `/etc/canary/canary.yaml` (restored from backup afterwards), Pi on the
    bench (`no_data`, no truck), `POST /control/replay` of a plain-named copy
    of `_capture-ram-study-2026-10-04T204557` at 1x (copy and `_replay-*`
    output removed afterwards). From `/status` sampled each minute:
    `twin_eval` (worker thread) p95 0.536 ms, max 0.61 ms over 300 evaluations;
    `twin_tick` (event loop) p95 0.046 ms, max 0.056 ms; `twin_identity`
    (worker) p95 77 ms, max 174 ms over 26 resolutions; 1 missed tick in the
    run (`overrun`); intake 4,525 reads observed, 0 dropped, 0 busy ticks;
    daemon RSS 575–690 MiB across the run. Live accounting 172 decided + 2
    pending vs the stored job's 174 (same reasons), one segment
    (ZS44A-20260920, program known). The replay pass reconciled (1,805,381
    rows, 0 dropped) but the controller kept `running: true` with
    `loop: false` until stopped by hand — a replay-controller end-of-pass
    defect, not the twin's.
  - *Remaining:* this was replay on an idle bench, with no baseline run and no
    collection load, so it cannot close the item. Still owed: a matched-load
    pair (twin off / on) during real collection on the truck, with collector
    record loss and queue depths, and RSS delta against a twin-off baseline;
    `twin_identity`'s 77–174 ms is off-loop but should be re-timed once
    identity caching is warm on the Pi.

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
  - *Pi, 2026-10-05 (b76a7da, then 418f367), bench, `no_data`:* job
    `bj-7a44ac1145f5107427e8` (twin evaluation of
    `_capture-ram-study-2026-10-04T204557`) queued 02:55:12Z and completed in
    ~4 s in the analysis child, evidence `ev-b96e0fe65f239a556a7f814b`, with
    accounting identical to the committed fixture. The first restart mid-job
    (02:57:40Z) exposed `failed: child_failed`, fixed in `418f367`; after the
    fix `recover()` re-ran that job to completion (attempt 2), and a second
    restart mid-job (03:32:15Z, `bj-610604b74f5e606fde51`) resumed and
    completed (attempt 2, `ev-49be9278e9dc5469b34f9fae`). A default-profile
    labelled window (`POST /control/captures`) does not make the brain yield,
    consistent with the workbench and digest, which defer only on an active
    capture profile.
  - *Remaining:* (1) Pi demonstration of a capture *profile* started mid-job
    with `/status` storage drops unchanged (needs the truck connected); (2) candidate promotion has no
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
  - *Pi, 2026-10-05:* after `scripts/restart_canary.sh` the evidence
    `ev-b96e0fe65f239a556a7f814b` resolved over HTTP, the ledger held the same
    single record before and after, a resubmitted job returned the same
    completed job, `/control/brain/status` showed identity `unchanged` and
    `transient: unknown`.
  - *Remaining:* (1) Pi: the same with a capture *profile* open (truck);
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
