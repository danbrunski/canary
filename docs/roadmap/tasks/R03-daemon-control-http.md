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

- [x] **R03.12 Withhold IF and sequence scoring while a non-default profile
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

  **Done 2026-09-25 (implementer).** `daemon/inference.capture_profile_gate`
  runs beside the vehicle-state gate; a tick the state allows but the profile
  refuses counts `emitter_counters.skipped_capture_profile` (never both
  counters for one tick). `/status` `detectors.capture_profile_gate` =
  `{scoring_allowed, profile, source}`, `source` `live_profile` or
  `replay_manifest`. `ReplayController.start` reads the source's
  `capture_profile.json` once (`ReplayStatus.source_corpus_eligible`, not in
  the `/control/replay` payload). Conservative option taken on the open
  questions: IF + sequence only; behavior models and plausibility unchanged;
  `corpus_eligible: true` still allowed for non-default profiles (the gate
  honours it). Tests (`tests/test_inference_profile_gate.py`, failing before
  — no counter, IF emitted): non-eligible profile + `engine_running` emits
  nothing and counts; return to `default` resumes scoring; replay of a
  non-eligible manifest scores nothing; `_capture-tuning-*` is refused by the
  replay controller outright; a default-manifest replay scores under a live
  tuning profile; sequence `prepare` builds no job when withheld.
  **Replay not run:** under replay the gate reads the source manifest, which
  a default-profile session lacks, so a default-profile replay is unchanged
  by construction; `test_daemon.py` replay-scoring tests pass unchanged.
  Operator questions above stay open (listed to the operator 2026-09-25).

- [x] **R03.1 One vehicle-state module. The inference copy has lost the
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

  **Done 2026-09-25 (implementer).** `src/vehicle_state.py` holds
  `VehicleState`, the thresholds (from `src.operating`),
  `classify_vehicle_state(obd_rpm=…, broadcast_rpm=…, …)` (`broadcast_rpm` a
  required keyword: omitting it is a `TypeError`), `derive_from_inputs`,
  `derive_from_live_source`, the Parquet variant `derive_from_latest_rows`
  (the read stays in `status.derive_vehicle_state_from_storage`),
  `running_replay_id` and `VehicleStateCache`. `status.py` re-exports every
  old name (`_derive_vehicle_state`, `_derive_vehicle_state_live`,
  `_active_session_dir` kept as aliases); `daemon/inference.py` and
  `control/nonvolatile_write.py` import `src.vehicle_state`, and both OWED
  allowances are gone from `tests/test_layering.py`. `routes/control.py` now
  imports public names (`active_session_dir`,
  `derive_vehicle_state_from_storage`, `control.sessions.latest_session`).
  **Freshness rule (provisional — the operator question stays open):** the
  broadcast fallback needs CAN freshness (the live copy's rule); the Parquet
  copy used to take a stale decoded rpm whenever the OBD rpm was unusable.
  Conservative: never claims `engine_running` on stale evidence. Tests:
  `test_inference_gate_reads_engine_running_from_the_broadcast` (failed
  before: gate said `engine_off`, status said `engine_running`),
  `test_all_did_capture_is_withheld_by_the_profile_not_the_state`
  (`skipped_capture_profile` counts, `skipped_vehicle_state` stays 0), and
  six new classifier tests in `test_vehicle_state.py` (required argument,
  stale-OBD/fresh-broadcast, stale broadcast is not evidence, the three
  derivations agree). `test_vehicle_state.py`, `test_daemon.py`,
  `test_decoded_mode_signals.py`, `test_dashboard_status_cache.py`,
  `test_layering.py` pass. Monkeypatch target retargeted:
  `status._derive_vehicle_state` → `status.derive_vehicle_state_from_storage`
  (`test_dashboard_status_cache.py`). Replay not run: a replay classifies
  `replay` before any rpm rule, so it cannot observe this change.

- [x] **R03.2 Route session classification through the bounded child.**
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

  **Done 2026-09-25 (implementer).** `classification.classify_one_session`
  split into `classify_and_write` (child: classify, fingerprint, write
  sidecars; returns payload + `fingerprint_written` + `session_dir`) and
  `finish_classification` (parent: trend alerting with the live emitter, the
  catalog). `workbench_jobs` kind `classify` (config-only: no bindings/DID
  inputs). The route is async: 400 non-live id and 404 missing dir (same
  string details as before), then `capture_deferral`, the row preflight via
  `select_sessions` (409 `over_budget`), then `run_off_loop`. Tests
  (`tests/test_control_classification.py`): computes in the child
  (`jobs == ["classify"]`, trend alert + catalog written in the parent),
  defers while a capture records (409 `analysis_deferred`, no job), refuses
  over the row budget (409, no job), keeps its 400/404. A real `spawn` child
  ran the `classify` kind once (0.7 s, sidecars written). The MCP
  `classify_session` tool posts to this route; `test_mcp_tools.py`,
  `test_control_api.py`, `test_storage_catalog.py` pass.

- [x] **R03.3 `DELETE /control/sessions/{id}` must not remove the session
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

  **Done 2026-09-25 (implementer).** `_guard_active_capture` also refuses the
  writer's directory, now through `ProfileCaptureSession.is_writer_session`
  (R03.5): 409 "cannot delete the session being written".
  `test_delete_refuses_the_session_the_writer_is_filling`
  (`tests/test_control_api.py`; returned 200 and deleted before). Replay not
  run: the guard is a route refusal, not on the telemetry path.

- [x] **R03.4 Pin the route table before moving anything.** This is the
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

  **Done 2026-09-25 (implementer).** `tests/test_route_table_snapshot.py` +
  `tests/fixtures/route_table_snapshot.json` pin, per route, method, path,
  auth rule, declared status code, query-parameter names and body presence,
  for the dashboard (110 rows) and the companion (35 rows), built with the
  optional components present (a bare app is asserted to have the same
  table). Auth is read from a `canary_auth` marker on the shared dependency,
  so a route guarded by a copy reads as unauthenticated. The fixture was
  generated from the tree *before* the auth change, identifying the four old
  closures by name, and the marker-based table after the change matches it
  byte for byte; the one hand edit was `/ws/control`, which authenticated
  inline and is now `control`. Also: every `/control/*` and `/ws/control`
  route is `control`, the dashboard has exactly one auth rule, and no route
  is shadowed by an earlier registration (order is not in the snapshot).
  `security.control_auth_dependency(token)` replaced the four identical
  closures (`control.py`, `calibration.py`, `workbench.py`, `rom_maps.py`);
  `/ws/control` uses `security.websocket_control_auth_dependency(token)`
  (constant-time, loopback-exempt only while no token is configured, 1008 on
  refusal). Tests: `test_control_websocket_refuses_a_wrong_or_missing_token`,
  `test_control_websocket_rule_matches_the_http_rule`
  (`tests/test_control_api.py`). The companion's deliberately different
  optional bearer is marked `companion`.

## Structure

- [x] **R03.5 Extract the profile-capture lifecycle out of the route
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

  **Done 2026-09-25 (implementer).** `src/control/profile_capture.py`:
  `ProfileCaptureSession(storage_writer, capture_controller, profile_store,
  health_monitor)` with `async start(ProfileCaptureStart)`,
  `async stop(reason)`, `pause()`, `live_quality()`, `is_writer_session()`;
  `CaptureAutostart(session, live_source, replay_controller, tick_seconds=)`
  owns the decider and its task (`arm`/`disarm`/`snapshot`/`tick`/`aclose`).
  Refusals are `ProfileCaptureError(status, detail)` carrying the routes'
  exact codes and bodies; control events stay at the edges (route, or the
  autostart with the publisher captured at arm). The seven `nonlocal`s are
  gone from `register_control_routes`; the autostart no longer calls route
  handlers with `_auth=None`. Both hang on `DaemonComponents`
  (`profile_capture`, `capture_autostart`, built in `run_daemon`) and reach
  the routes through new `create_app(profile_capture=, capture_autostart=)`
  keywords (signature otherwise unchanged; `None` builds them). Runtime
  shutdown calls `profile_capture.stop("daemon_shutdown")`: finalize against
  the in-memory epoch + stamp, then — as before — leaves the window, writer
  and profile alone. The dead `CaptureProfileStore(… behavior_models)`
  fallback is deleted (`capture_profile_store` is now required by
  `register_control_routes`; `create_app` always resolved one).
  `AUTOSTART_TICK_SECONDS` lives in `profile_capture`; `routes.control`
  keeps an alias that an app built without an injected autostart reads per
  tick (the `test_capture_profiles.py` monkeypatch target still works).
  Tests (`tests/test_profile_capture.py`): start → stop without `TestClient`
  (idempotent restart/stop, manifest `operator_stop`, writer back on a live
  session); a shutdown-ended capture reports `persistence.loss_epoch_start`
  in the returned and stored sidecar (the old path passed `start=None`);
  refusal codes 404/409/503; `run_daemon` shutdown stops through the session
  with `daemon_shutdown`. Route snapshot unchanged. `test_capture_autostart`,
  `test_capture_finalize`, `test_canaryctl_script`, `test_mcp_tools`,
  `test_daemon`, `test_daemon_startup_order` pass. `test_capture_profiles.py`
  41/41 on a clean `git archive HEAD` export with only this diff applied; in
  the shared tree `test_capture_profile_api_crud_and_auth` fails (45.01 vs
  45.0 `channel_reads_per_second`) from the concurrent R02 edit to
  `src/collection/capture_profiles.py`, not from this change.
  **Verifier fix 2026-09-25:** `daemon/server._build_app` (the daemon's only
  `create_app` call) now passes `profile_capture=` / `capture_autostart=`
  from the components, and refuses to build the app when a writer is wired
  but the pair is not, so the routes can no longer drive a private session
  that shutdown never stops. Test:
  `test_an_http_started_capture_ended_by_daemon_shutdown_reports_its_epoch`
  (`tests/test_profile_capture.py`): full `run_daemon`, capture started by
  `POST /control/capture` on the daemon's own app, shutdown, then the stored
  sidecar has `persistence.loss_epoch_start` and the manifest
  `daemon_shutdown`; also asserts `app.state.control_context.session is
  components.profile_capture` (and the autostart). Fails on HEAD's
  `server.py` (two different sessions).

- [x] **R03.6 Split `register_control_routes` into per-domain `APIRouter`s
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

  **Done 2026-09-25 (implementer).** `src/dashboard/routes/control.py` is now
  the package `src/dashboard/routes/control/`: `context.py`
  (`ControlContext`, frozen; `router()` = `APIRouter(dependencies=[Depends(
  require_auth)])`; `resolve_session` with the 404 mapping; `SessionDir =
  Annotated[Path, Depends(...)]`, reading the context from
  `app.state.control_context`) and one `build_router(ctx)` module each for
  `system`, `captures`, `replay`, `sessions`, `bus`, `profile_capture`,
  `classification` (classification/digest/trends/events), `jobs`, `models`,
  `feedback`, `tuning`, `custom_pids`. Route bodies moved verbatim by a
  generator (decorator `@app.`→`@router.`, the 77 per-route
  `_auth: None = Depends(require_control_auth)` parameters dropped for
  router-level auth, free names bound from the context).
  `register_control_routes` keeps its signature (`create_app` unchanged) and
  includes the routers in registration order; the SPA mount stays last. One
  auth definition: `security.control_auth_dependency` (R03.4). Largest route
  file 216 lines (`profile_capture.py`); `public.py` 393 (untouched). The
  route snapshot is unchanged and no route is shadowed
  (`tests/test_route_table_snapshot.py`, 5 passed). The routes' logger keeps
  the name `src.dashboard.routes.control`. Every test file importing the
  dashboard app (33 files, fast tier) was run; see the report for the one
  foreign failure.

- [~] **R03.7 Share the session-read routes between the companion and the
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

  **Partial 2026-09-25 (implementer).** Done:
  `src/dashboard/routes/session_reads.build_session_read_router(resolve_root,
  auth)` serves `…/chart`, `…/gps.gpx` and `…/gps.kml`; the dashboard
  includes it over the storage root with the control dependency, the
  companion over `lib.sessions_dir` with `require_local_auth` (its optional
  bearer, marked `companion`, unchanged). The two copies of the three routes
  are gone. Route snapshot unchanged for both apps; `test_companion_api.py`,
  `test_companion_services.py`, `test_companion_library.py`,
  `test_session_chart_route.py` pass (50). **Remaining:** the
  hook-parameterized `companion/services.*_response` functions (session
  list/get/export/checksum/metadata/pin/unpin/delete) are still called from
  both apps with per-app hooks (`finalize`, `after_delete`, `guard`,
  `serialize`); turning them into shared handlers is not done.

- [x] **R03.8 `drain.py`: one sink-registration helper and one per-record
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

  **Done 2026-09-25 (implementer).** `_run_record_sink(name, queue, handle,
  stop_event, *, failure, poll_interval, queue_warning_fraction,
  health_monitor)` is the one per-record loop; `run_live_source_sink`,
  `run_dtc_sink` and `run_monitor_sink` keep their public signatures and pass
  their callback (log text unchanged: `"<x>.observe failed; skipping
  record"`). `drain_queue_with_broadcast` registers all six sinks through a
  local `add_sink(name, overflow, runner, accepts=None)` with one `common`
  kwargs dict. Net size is about flat (992 → 1006 lines): the three wrappers
  and their docstrings stay, so the ~150-line estimate did not materialise.
  `test_daemon.py`, `test_daemon_startup_order.py`,
  `test_trip_speed_sources.py`, `test_replay_row_accounting.py`,
  `test_frame_decoder.py` and `test_sustained_validation.py` (the C10
  validation harness, slow tier included: whole scenarios through the real
  fanout) pass — 157 passed, 1 xfailed. Replay not run separately; the
  harness drives the same sinks end to end.

- [~] **R03.9 Split `daemon/inference.py` by detector.**
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

  **Partial 2026-09-25 (implementer).** Done: `src/daemon/detectors/`
  `common.py` (`TickContext`, `Detector`, `_PreparedDetector[Job, Computed]`
  owning the one `tick`, `_persistence_sustained`, `gate_and_collect`,
  `_publish_score`, `_with_effective_role`, the schema-reconciliation log),
  `isolation_forest.py` (465 lines), `sequence.py` (252), `behavior.py`
  (`_observe_behavior` split into `_grade_surface_models` and
  `_grade_channel_overrides`). Bodies moved verbatim by a generator.
  `inference.py` (2,222 → 1,179 lines) keeps gating, the pipeline, executors
  and `build_live_feature_source`, and re-exports every moved name (`X as X`);
  the moved code logs under `src.daemon.inference` as before.
  `inference_worker`'s lazy imports now name the detector modules. The 9
  monkeypatch sites were retargeted: 4 in `test_behavior_lifecycle.py` /
  `test_behavior_serving_contracts.py` → `src.daemon.detectors.behavior`,
  5 in `test_scoring_continuity.py` → `src.daemon.detectors.sequence`.
  `test_layering.py` STANDING allowances moved with the edges (inference's
  `dashboard.behavior_live` edge is now `detectors/behavior.py`'s; two
  `dashboard.broadcast` edges added for `detectors/common.py` and
  `detectors/isolation_forest.py` — moved, not new). The 15 test files that
  import `src.daemon.inference` pass (324 passed, 1 xfailed).
  **Remaining:** the before/after replay event diff was not run. Offline, the
  live pipeline ticks on the wall clock, so two replays of one session differ
  by timing alone; a meaningful diff needs the operator's replay protocol
  (`start_replay` on a quiet daemon) or a deterministic tick driver.

- [x] **R03.10 One base for the LSTM/TCN config models.**
  `config.LSTMAutoencoderConfig` and `TCNAutoencoderConfig` share 17
  identical training fields and validators, so a new training knob can reach
  one backbone and not the other. **Do:** create a `_SequenceTrainingConfig`
  base. LSTM redeclares `sequence_length`, `latent_dim` and `threshold_k` as
  required. **Accept:** the dumped JSON schema of both models is unchanged.
  Size **S**.

  **Done 2026-09-25 (implementer).** `config._SequenceTrainingConfig(_Strict)`
  holds the 17 shared training fields plus `enabled`, `sequence_length`,
  `latent_dim`, `threshold_k` at the TCN defaults; `LSTMAutoencoderConfig`
  redeclares those four as required and adds `hidden_size`/`num_layers`;
  `TCNAutoencoderConfig` adds `channels`, `kernel_size`, `num_levels`,
  `use_delta_decay` (its docstring is its schema `description`, so it is
  unchanged). `tests/test_sequence_config_base.py` compares both models'
  `model_json_schema()` to `tests/fixtures/sequence_config_schemas.json`,
  recorded from the classes before the change: equal as JSON objects (only
  property key order moved: subclass-only fields now follow the shared
  ones). Also pins the shared-field split and LSTM's required set. The 44
  test files that construct either config pass apart from R07.2 repo-guard
  errors in `test_daemon.py`/`test_control_trends_route.py` (daemon state
  written under `data/control`, pre-existing) and one failure in the
  R07-dirty `test_retrain_script.py`.

- [~] **R03.11 Small cleanups (batch them).**
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

  **Partial 2026-09-25 (implementer).** Done: `runtime._cancel_all(tasks)`
  (in order, skips `None`, suppresses only the task's own `CancelledError`)
  replaces the seven teardown blocks;
  `test_cancel_all_cancels_in_order_and_skips_absent_tasks`
  (`tests/test_daemon.py`); `test_daemon.py` passes. **Remaining:** the
  `--collection-only` drain is untouched pending the operator decision
  (keep through `drain.run_storage_sink`, or retire); the conservative
  option — no change — is what ships until then.

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
