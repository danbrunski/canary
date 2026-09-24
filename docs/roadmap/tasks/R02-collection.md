# R02 — collection: the OBD loop, the scheduler, recorders, ISO-TP

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).
`src/collection/` is 21k lines. Its two largest files are the serve path's
core:

- `obd_scheduler.py`: 3,137 lines.
- `can_obd_collector.py`: 2,555 lines. `run_can_obd_loop` alone is 727
  lines.

Several drift bugs sit inside the duplication, so the correctness items come
before the structural ones.

## Rules

- **This is the serve path.** Every item that touches the OBD loop runs the
  sustained validation harness (`scripts/sustained_validation.py`; slow tier
  `tests/test_sustained_validation.py`) before and after, and replays a pinned
  session. Hardware throughput claims still need the truck, and replay cannot
  establish them.
- Transports stay first-class and symmetric: ELM327 `obd_collector.py`,
  SocketCAN/slcan, K-line. Nothing here deletes or demotes one.
- The July decisions stand unless an item says what changed.
  - The collection `decode_*` functions stay separate (complementary PID
    coverage).
  - `decode_commanded_lambda`/`decode_wide_lambda` stay separate: their
    bodies are identical, but the names carry φ vs λ.
- Structural moves keep the old import paths as re-exports. List the ones
  tests monkeypatch, for example `test_hybrid_scheduling.py` patching
  `can_obd_collector.next_wake_at` and `_drain_frames_until`.

---

## Correctness first

- [ ] **R02.1 Catch slcan parse errors inside `receive_frame`.** *Drift bug,
  serve path.*
  - The collector's `_recv_and_tap` catches `ValueError`/`IndexError` (slcan
    parser errors). `can_obd_client.receive_frame` documents "Parse errors
    raised by the driver propagate; the collector counts them", but not every
    receiver is the collector.
  - A parse error during a discovery probe travels `CanObdClient._recv` →
    `receive_frame` → `probe_support` → `_issue_discovery_probe`, which
    re-raises. That leaves `run_can_obd_loop`.
  - `run_with_reconnect(reraise=(ValueError,))` treats `ValueError` as a
    config error and re-raises it (chain verified in code 2026-09-24).
    `daemon/runtime.py` then calls `task.result()`.
  - The reviewer's reading is that the daemon exits and any capture is
    dropped. `sensor_can_collector.collect_sensor_can` (bare `bus.recv`) has
    the same exposure.

  **Do:** `receive_frame` catches the parse errors, reports them through an
  `on_parse_error` callback and the throttled `log_dropped_can_frame`, and
  returns `None`. `_recv_and_tap` shrinks to accounting. The sensor-bus
  receiver uses `receive_frame`. **Accept:** a failing test first. A fake bus
  whose `recv` raises `ValueError` once during a discovery probe leaves the
  loop running and counts one parse error, and the same holds on the sensor
  bus. Size **S**.

- [ ] **R02.2 One "served in the Mode 22 window" predicate.** *Drift bug
  (latent), serve path.*
  - `obd_scheduler.SESSION_WINDOW_KINDS = {"did", "memory"}` exists so that
    the window controller, the plan and the eligibility matrix cannot
    disagree. Four window-semantics sites still test `kind == "did"`:
    - `can_obd_collector._loop_request_verdict`, the verdict production
      actually uses. Its twin `DidWindowController.request_verdict` uses
      `SESSION_WINDOW_KINDS` and is called only by
      `tests/test_diagnostic_discovery.py`, so the tests pin the copy
      production doesn't run. `DidWindowController.request_eligible` has no
      callers.
    - `obd_scheduler._next_request_due_at`: boundary-grid anchoring is
      DID-only. Its own comment describes the "skips every other window" bug
      that memory reads still have.
    - `can_obd_collector._cycle_plan_for`: `did_requests` leaves out memory
      reads, so `CyclePlan.feasible` / `mode01_share_seconds` come out
      optimistic.
    - `obd_scheduler._resolved_polling_plan`: memory reads don't set
      `selected_dids`, so session overhead is 0. Its per-DID window cap
      repeats a `_request_rate_hz` that has already been applied (idempotent,
      dead).
  - No starter profile combines memory channels with windows today, so this
    is latent.

  **Do:** add `_PollRequest.in_session_window` (a property over
  `SESSION_WINDOW_KINDS`) and route all four through it. The loop calls
  `controller.request_verdict` plus the profile/forced gates. Delete the
  duplicate verdict and `request_eligible`. DID-only *batching* sites
  (`select_did_batch_companions`, `0x62`, `did_batch_*`) stay as they are.
  **Accept:** a failing test first: a windowed profile with one memory
  channel is anchored to the window grid, charged session overhead, and
  counted in the cycle plan. `grep -n 'kind == "did"' src/collection` lists
  only batching sites. Size **S–M**.

- [ ] **R02.3 Make the capture-profile preview and the live plan one
  function.** *Drift bug.*
  - `CaptureProfileStore.resolved_plan` (142 lines) says it is "the same
    identifier-level plan used by live metrics". It is a re-implementation of
    `_resolved_polling_plan` + `_request_rate_hz`, and it disagrees:
    - Per-DID rate precedence: the preview uses *profile cycle → profile
      interval → config cycle*. The live path, `DidWindowController` and the
      preview's own session-overhead block use *profile cycle → config cycle
      → profile interval*.
    - The preview caps DID rate when `did_session is None`; live does not.
    - The preview ignores `round_timestamps` uniform cadence.
  - The window-period precedence is written out five times: capture_profiles
    ×2, `_request_rate_hz`, `_resolved_polling_plan` ×2, plus
    `_interval`/`_cycle`/`_cycle_plan_for`.
  - The only test (`test_capture_profiles.py`) checks one starter profile and
    does no parity check.
  - Root cause: the rate policy reads the global `tuning_profile` singleton,
    so the preview cannot call it with a candidate profile.

  **Do:**
  - Add `CaptureProfile.did_window_period(session) -> float` as the one
    precedence rule.
  - Make the rate/plan functions pure in `(request, spec, profile)`. The
    `tuning_profile` getters become thin wrappers that pass the active
    profile.
  - The preview calls the same functions with the candidate.

  **Accept:** a parity test over a small config × profile matrix (every
  starter profile, with and without `did_session`, with `round_timestamps`)
  asserts preview == live plan. The five precedence spellings reduce to one.
  Size **M–L**. Callers: dashboard `routes/control.py` preview routes plus
  the serve-path rate policy.

- [ ] **R02.4 One frame-timestamp policy.** *Drift bug, serve path.* Three
  policies stamp received frames:
  - `can_collector.frame_to_record` uses raw `float(msg.timestamp)` with no
    guard. Every broadcast row on both paths goes through it.
  - `can_obd_collector._frame_arrival` uses the kernel stamp only within
    `KERNEL_STAMP_MAX_SKEW_SECONDS`, otherwise userspace, and records which it
    used.
  - `sensor_can_collector.frame_to_records` uses `> 0 else now`.

  After a suspend-stale SocketCAN stamp (the case `_frame_arrival` documents),
  broadcast rows and OBD readings from the same instant are stamped
  differently. That breaks same-instant pairing, which `compare_channels`
  depends on. **Do:** one `frame_arrival(msg)` in a shared module, used by
  all three. **Accept:** a failing test first: a frame with a stale kernel
  stamp gets the same timestamp on the broadcast and OBD paths. Size **S–M**.

- [ ] **R02.5 Pin the OBD request's responder in the client, not in each
  caller.** *Drift bug.*
  - Inside `can_obd_collector.py`, `_query_with_tap` and
    `_query_batched_with_tap` are the same ~45-line receive loop: deadline,
    ID window, `request_id + 8` pinning, `0x78` P2* extension, NRC,
    flow-control, first-frame arrival.
  - `CanObdClient._collect_response_or_nrc` has the loop without the `0x78`
    extension and without responder pinning. So `/control/obd_request`, which
    builds `CanObdClient(bus, timeout_seconds=…)`, accepts any 0x7E8–0x7EF
    reply to a physical 0x7E0 request.
  - The tools pin it by hand (`verify_memory_channels.py`, `did_sweep.py`), a
    fix every caller must remember.

  **What changed since the July "don't merge the ISO-TP helpers" decision:**
  the `pid_echo`/`identifier` split was absorbed by
  `isotp.IsoTpResponseAssembler(identifier=…)`, which both files use now. What
  remains duplicated is loop *policy*.

  **Do:** add `isotp.await_response(recv, send_fc, assembler, request_id,
  deadline, stop) -> Exchange`, used by all three paths. Physical-address
  pinning is derived from `request_id` and is not optional. **Accept:**
  failing tests first:
  - `obd_request` to 0x7E0 ignores a 0x7E9 reply;
  - the client honours `0x78` response-pending.

  Size **M**.

- [ ] **R02.6 `record_can_obd` must stop on a full disk.** *Drift bug,
  recorder only.*
  - `can_collector.record` maps a writer `OSError` to
    `stopped_by="disk_error"` + `dropped_writes`.
  - In `record_can_obd` the writer runs inside `on_frame`, so the `OSError`
    escapes the loop and is treated as a bus failure. The bus is rebuilt
    (which succeeds and resets the counters), and it retries about every 10 s
    until the deadline or a signal, never reporting `disk_error`.
  - `record_can_obd` also lacks the "recording stopped" summary log, and
    imports the private `can_collector._write_manifest`.

  **Do:** see R02.8 for the shared skeleton. The minimum fix is a typed
  `DiskWriteError` raised by the wrapped writer, which ends the run as
  `disk_error`. **Accept:** a failing test first: a writer that raises
  `OSError(ENOSPC)` ends `record_can_obd` with `stopped_by="disk_error"` on
  the first failure, with no rebuild. Size **S**.

- [ ] **R02.7 Build the OBD spec from config in one place;
  `scripts/record.py` has drifted.**
  - `collectors.default_collectors`, a 30-field `CanObdSpec`, and
    `scripts/record.py` build the spec from the same config differently.
  - The recorder silently ignores `obd_batch_max_pids`,
    `obd_did_batch_max_dids`, DID response widths, `obd_priority_channels`,
    the DTC/monitor intervals, `obd_monitor_mids`, custom PIDs and the
    calibration id.
  - `default_collectors` computes the DID union
    (`obd_dids + inventory + profile.mode22`) twice.

  **Do:** add `CanObdSpec.from_config(collection, can, *, catalog=None, …)`
  in `obd_scheduler`, used by both. `validation/harness.build_can_spec` stays
  scenario-specific. **Accept:** a test that the recorder's spec equals the
  daemon's for the example config. Name the behaviour change in the
  evidence: batching turns on in the recorder, and that is the point. Size
  **S–M**.

## Structure

- [ ] **R02.8 One recorder skeleton.**
  - `can_collector.record` (235 lines) and `record_can_obd` (200 lines)
    duplicate the stop checks, the four `RebuildOutcome` → `stop_reason`
    branches, best-effort shutdown, the `finally` that shuts down a rebuilt
    bus, `RecordingResult`, and the manifest try/except.
  - R02.6 is the drift already found between them.

  **Do:** `src/collection/recording.py` owns `RecordingResult`,
  `write_manifest` (public) and a `RecorderRun` context holding stop reasons,
  the rebuild mapping, finalization and `DiskWriteError`. Both recorders use
  it. **Accept:** `test_can_collector`, the `test_can_obd_collector` recorder
  tests and `test_record_script` pass unchanged. There is no private
  cross-module import. Size **M**.

- [ ] **R02.9 Turn `run_can_obd_loop` into a class, with a `BatchGovernor`.**
  - `run_can_obd_loop` is 727 lines threading ~40 locals. The duplication is
    inside the function:
    - `next_budget_at = max(next_budget_at + 1/bus_budget.effective(...),
      time.monotonic())` ×5
    - `_drain_frames_until(bus, on_frame, stats, stop_event, wake.at,
      deadline)` ×5
    - `ecu_answered_since_{pid,did}_batch_failure = True` ×3
    - a hand-mirrored ~65-line PID-vs-DID batch-feedback block over 10
      locals (`batching_enabled`, `active_batch_max`, `consecutive_*`,
      `ecu_answered_*`, `probe_single_*`)
    - a literal `min(..., 3)` that should be `isotp.MAX_BATCHED_DIDS`
  - It has already drifted: the in-loop `_scheduler_metrics(...)` passes
    `pid_batch_max`/`did_batch_max`, and the call at loop exit omits them. The
    final snapshot's `resolved_plan` therefore reports the *configured* batch
    size, not the narrowed or disabled one.

  **Do:**
  - `BatchGovernor(max, narrow=…)` with `size_for_next()`,
    `on_batch(answered, nrc)`, `on_single(answered)`, `on_ecu_answer()` and
    `metrics()`. The PID policy halves and the DID policy disables, as a
    strategy.
  - `_CanObdLoop` holds `rounds`, `controller`, discovery, installed/wait/
    blocked, `forced`, the budget clock and the two governors. Methods:
    `_on_generation_change`, `_apply_triggers`, `_publish_metrics` (one call
    site, also used at exit), `_tick_controller`,
    `_maybe_read_calibration_id`, `_maybe_probe_discovery`, `_ensure_round`,
    `_serve_batch`, `_serve_single`, `_spend_budget_slot` and `_drain_until`.
  - `run_can_obd_loop()` stays as a thin wrapper.
  - File moves: the loop goes to `can_obd_loop.py`. Wire I/O
    (`_query_with_tap`, `_query_batched_with_tap`, `_send_session_control`,
    `_read_calibration_id`, `_recv_and_tap`, `QueryResult`, `FrameArrival`)
    goes to `can_obd_io.py`. The pure `_decode_response` /
    `_response_to_reading` go to `obd_response_decode.py`.
    `can_obd_collector.py` keeps the daemon and recorder entry points
    (~400 lines).

  **Accept:**
  - A failing test first for the exit-snapshot drift.
  - Then the move, with `test_can_obd_collector` (2,229 lines),
    `test_multi_pid_batching`, `test_multi_did_batching`,
    `test_capture_rounds`, `test_d02_accounting` and `test_ecu_calibration_id`
    passing, and monkeypatch targets retargeted.
  - The sustained harness and a pinned replay before and after, with the
    diff recorded here.

  Size **L**. Do it after R02.1–R02.5, which change the same code.

- [ ] **R02.10 Split `obd_scheduler.py` into a package and make its de-facto
  public names public.**
  - `can_obd_collector` imports 12 underscore names from it
    (`_PollRequest`, `_PollRequestState`, `_select_next_due_request`,
    `_scheduler_metrics`, …). `_PollRequest` appears in 6 test files.
  - `tuning_profile` types requests as `Any` in 3 functions.
  - There are 12 function-local `from src.collection import tuning_profile`
    imports with no cycle behind them (`tuning_profile` does not import
    `obd_scheduler`).
  - Two kind `Literal`s disagree: `_PollRequest.kind` vs
    `request_eligibility.RequestKind`, and `evaluate(kind: str)` is
    unchecked.

  **Do:**
  - Create `obd_scheduler/{spec,requests,cadence,selection,rounds,did_window,metrics}.py`.
  - `__init__` re-exports everything currently imported from outside.
    Generate the list with grep and put it in the evidence.
  - Rename `_PollRequest` → `PollRequest` and `_PollRequestState` →
    `PollRequestState`, keeping aliases.
  - Move the imports to the top of the modules.
  - Use one kind `Literal`.

  **Accept:** mypy is clean with `tuning_profile`'s `Any` replaced by
  `PollRequest`. All ~25 importers work through the re-exports. Size **L**.
  Do it after R02.2, R02.3 and R02.9.

- [ ] **R02.11 Make the scheduler-metrics contracts structural.**
  - `capture_quality.BATCHING_COUNTERS` / `MODE22_BATCHING_COUNTERS` read
    `_batching_metrics`' dict keys with `int(block.get(name) or 0)`. A renamed
    key reads as a verified zero, and no test pins the key sets.
  - `ObdSchedulerMetrics.to_dict` hand-lists 27 keys. The comment on its
    `receive` field records that this enumeration already dropped a field
    once (C06.12).

  **Do:** build `to_dict` from `dataclasses.fields` plus the one alias. The
  batching counters become a `TypedDict` or const tuple owned by the
  producer and imported by `capture_quality`, which refuses an absent key
  rather than reading zero. **Accept:** a test that the producer's keys equal
  the consumer's sets. Size **S**.

- [ ] **R02.12 Move `bus_factory` and friends out of the sniffer module.**
  - 15 importers take `bus_factory` from `can_collector`: `profiles`,
    `control/*`, `dashboard/routes/calibration`, 9 tools and `preflight`.
    Most of them transmit, and the module's docstring says "sniffer… never
    calls bus.send()".
  - `frame_to_record` and `DEFAULT_RECONNECT_POLICY` also live there.
    `DEFAULT_RECONNECT_POLICY = ReconnectPolicy()` is written in 7 modules.
  - `sensor_bus_factory` duplicates the virtual-bitrate rule.

  **Do:** create `src/collection/can_bus.py` and re-export from
  `can_collector`. Retarget `tests/test_profiles.py`'s patches of
  `src.collection.can_collector.bus_factory`. Size **S–M**.

- [ ] **R02.13 Move the J1979 decoders out of the transport client.** This is
  a relocation, not a merge: the two decoder sets stay separate.
  - `can_obd_client.py` (1,178 lines) holds `CanObdClient` plus ~370 lines of
    pure decoders and `PID_DECODERS`.
  - `obd_registry` imports from it, including the private `_need`, and the
    client has to inject `byte_length_for` to avoid a cycle.

  **Do:** create `src/collection/pid_decoders.py` (no python-can import) and
  re-export from `can_obd_client`. `assert_pid_tables_consistent` still runs
  at import. Size **M**.

- [ ] **R02.14 Small cleanups (batch them).**
  - Delete dead code: `CanObdClient._handle_single_frame` (no callers) and
    `_extract_service_payload` (test-only).
  - Remove the 66 unused `# noqa: E501` in `obd_registry`
    (`ruff --select RUF100`).
  - Remove the trivial wrappers `can_obd_collector.parse_can_id` and
    `can_collector.make_session_dir`.
  - `kline_collector._wait` and the inline `wait_for(stop.wait())` in the
    IMU, supply-voltage and replay collectors switch to
    `reconnect.wait_for_stop_or_timeout`.
  - Docstrings that call ELM327 "dead" or describe a "Canable-only direction"
    (`can_obd_collector` module docstring, `can_obd_client` module docstring)
    break the no-hardware-editorializing rule. Reword them neutrally; the
    in-vehicle rig is SocketCAN on the HAT anyway.
  - `pids.py` claims to be the "single source of truth" but is ELM327-only.
    Say so.
  - `uprev_session` hard-codes the dev vehicle's startup sequence. Move it to
    the vehicle profile or name it as Xterra-specific.

  Size **S**.

## Looked at and fine

- `capture_quality.py` (1,167 lines): one cohesive evaluator, mostly
  documented constraints. Only R02.11 applies.
- `capture_profiles.CaptureProfile`: the "151-line function" is the field
  block; the validators are fine.
- `obd_registry._MODE01_SPEC`: a typed data table (the "775-line function"
  is the tuple literal). No YAML.
- `kline/`, `reconnect.py`, `queueing.py`, `isotp.py`, `discovery.py`,
  `hybrid_cycle.py`, `polling_plan.py`, `telemetry.py`, `session.py`,
  `can_ids.py`: fine.
- The IMU, supply-voltage and GPS collectors: thin wrappers over
  `run_with_reconnect`.
