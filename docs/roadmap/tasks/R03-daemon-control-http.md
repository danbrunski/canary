# R03 — daemon, control and the HTTP layer

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).
Three problems carry most of the weight here:

- **One route function.** `src/dashboard/routes/control.py` is 1,823 lines,
  and 1,640 of them are a single function, `register_control_routes`. It
  holds 78 nested route closures and 7 pieces of `nonlocal` mutable state.
  No `APIRouter` is used anywhere.
- **Vehicle state has three derivations**, and the one the inference gate
  uses has lost a rule.
- **One route reads a whole session in-process**, unbounded.

## Rules

- **HTTP contracts do not change.** Paths, methods, status codes, response
  shapes and auth stay the same. Before any route moves, a snapshot test pins
  them (R03.4).
- Live-session reads go through `control.sessions.latest_session`. Anything
  that reads a whole session goes through the bounded single-flight child
  (`workbench_jobs.run_off_loop`) and defers while a capture records.
- `scripts/restart_canary.sh` semantics are untouched. Nothing here adds a
  restart path.
- Serve-path items (R03.1, R03.3, R03.7, R03.8) replay a pinned session before
  and after.

---

## Correctness first

- [ ] **R03.12 Withhold IF and sequence scoring while a non-default profile
  polls.** *Operator decision 2026-09-24, serve path. Added after the review;
  it goes before R03.1.*

  **The decision.** Tuning logs don't represent the default profile, so they
  are not in the anomaly detectors: not in training, calibration or
  evaluation, and not in live scoring. The detectors learned "normal" under
  the default profile's channels and rates. A tuning capture polls a
  different set at different rates, so scoring it measures the profile
  change, not the engine.

  **Today (checked 2026-09-24):**
  - Training and calibration are protected. The `tuning` profile is
    `corpus_eligible: false`, so `ParquetWriter` writes it to
    `_capture-tuning-*`, which `discover_sessions` skips.
  - Live scoring is not protected. The inference gate (`scoring_allowed`
    in `daemon/inference.py`) looks only at `vehicle_state`. The Xterra
    `tuning` profile polls Mode 01 RPM, so vehicle state reads
    `engine_running` and IF/sequence score and emit on tuning data for the
    whole capture.
  - All-DID profiles are withheld only by accident: vehicle state misreads
    them as `engine_off` (R03.1).

  **Do:**
  - Add a second gate beside the vehicle-state one: IF and sequence score
    only while the active profile is corpus-eligible
    (`tuning_profile.active_profile().corpus_eligible`). Count refusals as
    `skipped_capture_profile`, a separate counter from
    `skipped_vehicle_state`, so an empty score stream says which kind of
    empty it is.
  - Surface the withheld state in `/status` detectors.
  - During replay the gate reads the replayed session's capture manifest
    (`read_capture_manifest(...).corpus_eligible`), not the daemon's live
    profile.
  - Behavior models and plausibility checks are not IF/sequence and are out
    of scope here (see below).

  **Accept:**
  - Failing tests first:
    - with a non-eligible profile active and `engine_running`, IF and
      sequence emit nothing and `skipped_capture_profile` counts;
    - on return to `default`, scoring resumes;
    - replaying a `_capture-tuning-*` session scores nothing.
  - A replay of a default-profile session is unchanged.

  **Coordinate first:** `src/daemon/inference.py` was dirty in another
  session on 2026-09-24.

  **Operator questions (record the answers here):**
  - Should `corpus_eligible: true` stay possible for a non-default profile?
    The schema allows it today and no profile uses it. The decision's
    wording, "doesn't represent the default profile", suggests it should
    not.
  - Do behavior-model deviations also stop during tuning captures? Some
    refit recipes deliberately fit on capture-only DIDs (B-series), so that
    is a separate call.
  - Should plausibility checks (A05) keep running? They are deterministic
    range/stuck/freshness checks, and profile-independent.

  Size **S–M**.

- [ ] **R03.1 One vehicle-state module. The inference copy has lost the
  broadcast-RPM fallback.** *Drift bug, serve path. Land R03.12 first:
  without it, this fix turns scoring **on** during all-DID tuning
  captures.* `VehicleState` is derived three times:
  - `dashboard/status._derive_vehicle_state` (from Parquet).
  - `status._derive_vehicle_state_live` (from memory).
  - `daemon/inference._live_vehicle_state`. Its docstring says it "Mirrors
    `dashboard.status._derive_vehicle_state`", but it passes only
    rpm/coolant/run_time/freshness to `classify_vehicle_state`. It ignores
    `inputs["broadcast_rpm"]` and never sets `rpm_from_broadcast` (verified
    2026-09-24).

  Both status variants carry the "OBD stale → use decoded `ECM_RPM`" rule
  (`7f0d933`). That rule exists for the all-DID capture, where Mode 01 is dark
  by the ECU's design. The two copies also differ from each other: the live
  one requires CAN freshness and the Parquet one does not.

  During a Mode-01-less capture, `/status` and autostart therefore say
  `engine_running` while the inference gate says `engine_off`.
  - The withheld IF/sequence scoring that results is the *right outcome for
    the wrong reason*: tuning captures must not be scored (R03.12). Once
    R03.12 gates on the profile, the classifier no longer has to be wrong to
    keep them out.
  - The same wrong state is fed to `idle_shutdown.observe` (opt-in, ships
    disabled).
  - The future non-volatile-write gate, which proceeds only from
    `ignition_only`, will need one trustworthy classifier.

  There are also import inversions:
  - `daemon/inference.py` and `control/nonvolatile_write.py` import
    `src.dashboard.status`, which pulls in replay_control,
    feature_spec_health and more.
  - `routes/control.py` imports four private names from `status`.

  **Do:** create `src/vehicle_state.py` holding `VehicleState`, the
  thresholds (R01.7), `classify_vehicle_state`, one
  `derive_from_inputs(inputs, replay_id, now)` used by both the live route and
  inference, the Parquet variant, and `VehicleStateCache`. Move the broadcast
  fallback *into the classifier's signature*:
  `classify_vehicle_state(obd_rpm=…, broadcast_rpm=…, …)` resolves which rpm
  to use, so no caller can skip it. `status.py` re-exports. Decide which of
  the two freshness rules is right, and record the choice here.

  **Accept:**
  - A failing test first: an all-DID capture (Mode 01 stale, broadcast RPM
    fresh) classifies `engine_running` in the inference gate. The same test
    shows IF/sequence still withheld, now counted under
    `skipped_capture_profile`.
  - `test_vehicle_state.py`, `test_daemon.py`'s vehicle-state tests,
    `test_decoded_mode_signals.py` and `test_dashboard_status_cache.py` pass.

  Size **M**.

- [ ] **R03.2 Route session classification through the bounded child.**
  *Serve-path OOM risk.*
  - `POST /control/sessions/{id}/classification` calls
    `control/classification.classify_one_session` synchronously in the
    FastAPI threadpool (verified 2026-09-24: no `run_off_loop` in the route).
  - That runs `classify_session` → `resample_and_segment`, then
    `compute_fingerprint`. The `control/trip_end.py` docstring measures these
    loads at 5.4 GB for 2.6 M rows.
  - The route has no row preflight, no capture deferral, no RLIMIT and no
    single-flight.
  - Agents reach it through the MCP `classify_session` write tool.
    `workbench_jobs.run_off_loop` says it is "the only way a route may
    compute one"; this route is the exception.

  **Do:** add a `classify` kind to `workbench_jobs._compute` and call
  `run_off_loop` from the route. Trend alerting, the emitter and the catalog
  update stay in the parent, after the child returns sidecar paths.
  **Accept:**
  - A test that the route defers while a capture records.
  - A test that the route refuses over the row preflight.
  - `test_control_api.py` and `test_mcp_tools.py` classification tests pass.

  Size **M**.

- [ ] **R03.3 `DELETE /control/sessions/{id}` must not remove the session
  being written.** *Drift bug.*
  - `_guard_active_capture` refuses only when the id equals
    `capture_controller.current_capture().session_id` (verified 2026-09-24).
  - Retention protects the writer's live directory through
    `protect=storage_writer.paths.root`, but the delete route has no such
    check. With no capture active, the plain live session can be `rmtree`d
    under the writer.

  **Do:** the guard asks "is this the writer's current directory, or the
  active capture?" R03.5 gives the question an owner, but the one-line fix
  lands here first. **Accept:** a failing test first: deleting the writer's
  current session returns 409. Size **S**.

- [ ] **R03.4 Pin the route table before moving anything.** This is the
  safety net for R03.5 and R03.6, in the style of
  `tests/test_mcp_tool_snapshot.py`. **Do:** add
  `tests/test_route_table_snapshot.py`, which builds the app with every
  optional component present and records `(method, path, requires_auth)` for
  every route. The fixture is `tests/fixtures/route_table_snapshot.json`.
  - It must fail if a `/control/*` route is unauthenticated.
  - The reviewer verified by script that every `/control/*` route is
    authenticated today; only the 16 `public.py` routes are open.
  - `/ws/control` (`routes/streams.py`) compares the token with `!=`, not
    `secrets.compare_digest`, and has no loopback exemption. Fix it here
    through a shared `security.control_auth_dependency(token)`.

  **Accept:** the snapshot is committed and `/ws/control` uses the shared
  check. Size **S**.

## Structure

- [ ] **R03.5 Extract the profile-capture lifecycle out of the route
  closures.**
  - `register_control_routes` holds 7 pieces of `nonlocal` state:
    `budget_before_capture`, `last_ended_capture`, `superseded_window`,
    `capture_transition_lock`, `autostart`, `autostart_task` and
    `capture_start_epoch`.
  - That state is shared by `start_profile_capture` (150 lines, with an
    8-step rollback), `stop_profile_capture`, pause, quality, and the three
    autostart routes (~400 lines).
  - `_autostart_tick` calls the route handlers directly with `_auth=None`.
  - `daemon/runtime.py` shutdown re-implements part of the stop sequence
    (`finalize_capture(start=None)` + `stamp_capture_end`). It cannot reach
    the in-memory epoch, so `loss_epoch_start` is reported `None` for
    captures ended by shutdown.
  - The fallback `CaptureProfileStore(… config.daemon.behavior_models)` is
    dead: `api.py` always passes a store built with `behavior_models_path`.

  **Do:**
  - Create `src/control/profile_capture.py::ProfileCaptureSession(storage_writer,
    capture_controller, profile_store, health_monitor)` with `async
    start(req)`, `async stop(reason)`, `pause()`, `live_quality()` and
    `is_writer_session(path)`.
  - Add `CaptureAutostart(session, live_source, replay_controller)`, which
    owns the decider and its task.
  - Hang both on `DaemonComponents`. Routes become thin, runtime shutdown
    calls `session.stop("daemon_shutdown")`, and R03.3's guard asks the
    session. Delete the dead fallback.

  **Accept:**
  - The route snapshot (R03.4) is unchanged.
  - `test_capture_profiles.py`, `test_capture_autostart.py`,
    `test_capture_finalize.py`, `test_canaryctl_script.py` and
    `test_mcp_tools.py` pass.
  - A new unit test drives start → stop without `TestClient`.
  - A failing test first: a capture ended by shutdown reports its
    `loss_epoch_start`.

  Size **L**.

- [ ] **R03.6 Split `register_control_routes` into per-domain `APIRouter`s
  with router-level auth.**
  - The dashboard has 106 routes, 78 of them in `control.py`.
    `Depends(require_control_auth)` is repeated 77 times, plus 12
    `Depends(authorize)`.
  - The auth closure is copied 4 times with identical bodies: `control.py`,
    `calibration.py`, `workbench.py` and `rom_maps.py`.
  - The `resolve_session` → `HTTPException(404)` mapping is inlined in
    `control.py` (×2), `workbench.py` (×2) and companion `routes.py` (×2).

  **Do:**
  - Create `src/dashboard/routes/control/{sessions,captures,profile_capture,replay,bus,jobs,models,feedback,tuning,custom_pids,system}.py`,
    each with `build_router(ctx) -> APIRouter` created as
    `APIRouter(dependencies=[Depends(ctx.require_auth)])`.
  - `ControlContext` is a frozen dataclass: config, token,
    `publish_control_event`, controllers/stores, and a
    `SessionDir = Annotated[Path, Depends(...)]` dependency that owns the 404
    mapping.
  - Keep the `create_app` signature (124 call sites, mostly tests) and build
    the context inside it. The SPA mount stays last.

  **Accept:** the route snapshot is unchanged, one auth definition remains,
  and no route file is over ~400 lines. Size **L** (M after R03.5).

- [ ] **R03.7 Share the session-read routes between the companion and the
  dashboard.**
  - Companion `routes.py`'s gpx/kml/chart routes are near-copies of
    `control.py`'s, differing only in root and auth. They post-date the July
    `companion/services.py` extraction, so they weren't included in it.
  - The companion serves ~21 of the same `/control/*` paths.

  **Do:** after R03.6, add `build_session_read_router(resolve_root,
  auth_dep)`, included by both apps. The hook-parameterized
  `services.*_response` functions become ordinary handlers.
  `companion._require_optional_bearer` keeps its deliberately different
  semantics (on the laptop, no token means open). **Accept:** the companion
  tests and `test_session_chart_route.py` pass. Size **M**.

- [ ] **R03.8 `drain.py`: one sink-registration helper and one per-record
  loop.**
  - In `drain_queue_with_broadcast` (221 lines), sink registration is the
    same queue + `TelemetrySink` + `create_task(… poll_interval,
    queue_warning_fraction, health_monitor)` block, 6 times.
  - `run_live_source_sink`, `run_dtc_sink` and `run_monitor_sink` are the
    same 20-line loop, differing only in the callback.

  **Do:** add `_run_record_sink(name, queue, handle, stop_event, **common)`
  and a local `add_sink(name, policy, runner, accepts=None)`, which should
  save ~150 lines. **Accept:** `test_daemon.py`, `test_daemon_startup_order.py`,
  `test_trip_speed_sources.py` and the validation harness pass. The
  validation harness reuses the same factories: see R04.13. Size **S–M**.

- [ ] **R03.9 Split `daemon/inference.py` by detector.**
  - The file is 2,214 lines, laid out as: gating/generation;
    `Detector` protocol + the IF, Sequence and Behavior classes (their two
    `tick` methods are AST-identical); the pipeline; executors; sequence
    engine helpers; IF compute/apply; and behavior observation
    (`_observe_behavior`, 159 lines, two loops).

  **Do:**
  - Create `daemon/detectors/{isolation_forest,sequence,behavior}.py` with a
    `_PreparedDetector` mixin for the shared `tick`.
  - `inference.py` keeps the pipeline and re-exports.
  - Split `_observe_behavior` into its two loops.
  - `_finite_or_none`/`_num` go to R01.3.

  **Accept:** 12 test files import privates from here. Retarget the 9
  `monkeypatch.setattr(inference, "gate_and_collect" | "_publish_score" |
  "_build_sequence_engine")` sites. Replay before and after with the event
  diff. Size **M**; mostly navigability.

- [ ] **R03.10 One base for the LSTM/TCN config models.**
  `config.LSTMAutoencoderConfig` and `TCNAutoencoderConfig` share 17
  identical training fields and validators, so a new training knob can reach
  one backbone and not the other. **Do:** create a `_SequenceTrainingConfig`
  base. LSTM redeclares `sequence_length`, `latent_dim` and `threshold_k` as
  required. **Accept:** the dumped JSON schema of both models is unchanged.
  Size **S**.

- [ ] **R03.11 Small cleanups (batch them).**
  - `run_daemon` teardown repeats the cancel/await/suppress block 7 times.
    Add `_cancel_all(tasks)`.
  - `--collection-only` keeps a second, synchronous drain
    (`main._drain_queue`, `run_collection`) without decode or arming.
    `docs/daemon-architecture.md` calls it non-production, and only
    `tests/test_main.py` uses it. Either run it through
    `drain.run_storage_sink` or retire it. **Operator decision**: record which.
  - `mcp_server/parquet_query.TelemetryQuery` and
    `alerts/emitter.IsolationForestInferenceEngine` are covered by R01.8.

  Size **S**.

## Looked at and fine

- `config.py` as a whole: flat pydantic declarations imported from 249
  places. Only R03.10 is worth doing.
- MCP `_register_read_tools`/`_register_write_tools`: mostly LLM-facing
  docstrings. The 15 hand-written tools stay (D03.23).
- `public.py`'s 16 routes and the `workbench.py` registrar: fine once auth is
  router-level.
- `_now`/`_utc_now` copies: trivial, and they serve as test seams.
- The 31 inline `yaml.safe_load` calls: error handling differs per caller.
- `ControlAuditLogger`, the job ledger and `AnomalyEventLogger`: nothing has
  changed since July.
- `control/jobs.py::_build_command`: a flat dispatch table.
- `status.build_status`: fine once R03.1 moves vehicle state out.
- The name-sorted `iterdir` in `retention.py`: restricted to plain timestamp
  names, so the order is chronological. It still moves under R01.1.
