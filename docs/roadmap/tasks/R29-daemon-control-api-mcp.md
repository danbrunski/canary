# R29 — Daemon, control plane, HTTP API and MCP

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope:
`src/daemon/`, `src/control/`, `src/dashboard/` (Python), `src/mcp_server/`,
`src/companion/`, `src/vehicle_state.py`, `src/config/`. Structure,
duplication and efficiency, plus three defects found on the way (R29.1,
R29.2, R29.4). Counts are review snapshots; locate code by symbol. Nothing
here has been implemented. Shared acceptance rules are in R22.

**State of the area.** Authentication is centralized and proven per route
(`control_auth_dependency`, `tests/test_route_table_snapshot.py`). The MCP
layer is thin: 65 of 80 tools are `Delegation` rows whose schema comes from
the delegate. `src/vehicle_state.py` is the one classifier (R03.1 holds).
`src/config/` (R21) has not regrown. Plain `def` routes run in FastAPI's
thread pool, so their synchronous reads do not block the loop. The debt is in
rules that each caller has to remember: where whole-session analysis may run,
how many analysis children may run at once, and how a refusal becomes an HTTP
status.

`src/daemon/health.py` has another session's uncommitted edits. R29.9 touches
it; go after that work lands.

- [ ] **R29.1 Whole-session analysis still runs in-process on the Pi through
  routes and tools the bounded-child rule does not cover.** R03.2 moved
  classify and digest into the workbench's single-flight child, deferred while
  a capture records. Three paths were left outside it:
  - `GET /sessions/{id}/trip-summary` (`routes/public.py`, no auth). The first
    request for a session computes it in the daemon's thread pool:
    `get_trip_summary` → `get_or_compute_trip_summary` →
    `resample_and_segment` over the whole session, the call that justified
    R03.2. No capture check.
  - The MCP tools `get_delivery_error`, `compare_tuning_sessions`,
    `get_behavior_deviations` and `query_telemetry` run pandas or DuckDB over
    stored parquet inside `canary-mcp`, which `systemd/canary-mcp.service`
    runs on the Pi. None checks `tuning_profile.is_active()`, uses a child, or
    has a row or span budget. `get_delivery_error` is also a second
    implementation of `/analysis/delivery-error` with different parameter
    names and error shape.
  - `GET /analysis/binned` (no auth) runs DuckDB in the thread pool, bounded
    only by a 200,000-row limit.

  `AGENTS.md`: "No analysis on the Pi while it collects"; the incident cost
  150k records.
  - [ ] A `trip_summary` job kind in `workbench_jobs.JOB_KINDS`; the route
    calls `run_off_loop`. Test: the route during a capture answers 409
    `analysis_deferred`.
  - [ ] MCP `get_delivery_error` and `compare_tuning_sessions` become proxies
    of the workbench routes (the `delivery_error` and `pull_comparison` kinds
    exist).
  - [ ] `query_telemetry`, `get_behavior_deviations` and `search_anomaly_log`
    refuse while a capture is active, and `docs/mcp-server.md` says so.
  - [ ] `capture_deferral` on `/analysis/binned`.
  - Must not change: the sidecar format and `trip_summary.json` schema
    version.

- [ ] **R29.2 The bounded-child runner exists four times and "one analysis
  child at a time" is enforced three ways and once not at all.**
  `trip_end._in_digest_child`, `capture_finalize._run_recompute`,
  `workbench_jobs._compute` and `BrainScheduler._run` share one skeleton
  (spawn pool, `run_in_executor`, `wait_for`, discard on failure) and differ
  in the exception raised. `workbench_jobs` imports the private `_spawn_pool`
  and `_discard_pool` from `trip_end`. The exclusion differs: the digest waits
  for the brain child to exit; `run_off_loop` refuses, asks the brain to
  yield, and refuses while a digest is in flight; `_run_recompute` checks
  nothing (its only `child_pool` call is the address-space cap). A
  capture-stop recompute can therefore overlap a trip-end digest or a
  workbench job, which is the memory contention the rule exists to prevent.
  The Pi's cgroup is 6 G with swap off.
  - [ ] `child_pool.run_in_child(call, *, timeout, executor=None)` returning
    an outcome (`TIMED_OUT | BROKEN | OOM`) and owning pool creation and
    discard. Each module keeps its own exception types and messages.
  - [ ] One exclusive slot in `child_pool` with the existing yield-and-wait
    protocol, which `capture_finalize` joins.
  - [ ] Test: a recompute requested during a running digest waits or is
    refused, by name.

- [ ] **R29.3 Four `(status, detail)` exception classes and 38 hand-written
  remaps to `HTTPException`; no app-level handler.**
  `WorkbenchRefusedError`, `BrainJobRefusedError` and `BrainReadError` have
  identical bodies; `ProfileCaptureError` differs by allowing a string
  detail. `src/dashboard` has 169 `raise HTTPException`, 38
  `except <refusal> as exc: raise HTTPException(exc.status, exc.detail)`
  blocks, and no `exception_handler`. A route that forgets the `except`
  answers 500 instead of the named refusal.
  - [ ] One `StatusError(Exception)` (status, detail, pickle-safe) in the
    lowest layer that needs it; the four subclass it; `create_app` registers
    one handler that returns `{"detail": …}`, the body `HTTPException`
    produces. Delete the remap-only blocks. This is the HTTP face of R24.1
    (`ReasonedRefusal`); design the two together.
  - Pinned by `tests/test_exceptions_pickle.py` and
    `tests/test_route_table_snapshot.py`.

- [x] **R29.4 The MCP `get_binned_analysis` tool dropped `units`.** The
  closure in `mcp_server/server.py` stops at `until`; the delegate in
  `tools/binned_analysis.py` and the daemon route take
  `units: str = "stored"`, whose docstring calls the raw-count default "the
  trap this option closes". An MCP client cannot ask for engineering units,
  so binning `decoded:ECM_RPM` through MCP works in counts.
  `tests/fixtures/mcp_tool_snapshot.json` pins the drifted schema.
  - [x] Delete the closure, add a `Delegation` row, regenerate the snapshot.
    Closure deleted from `_register_read_tools`; `Delegation(binned_analysis_tool.get_binned_analysis)`
    added to `READ_DELEGATIONS`. Snapshot regenerated with
    `CANARY_UPDATE_MCP_SNAPSHOT=1 pytest -n0 tests/test_mcp_tool_snapshot.py`;
    its diff is the added `units` property (default `"stored"`) and the
    description now taken from the delegate, nothing else. New
    `tests/test_mcp_tools.py::test_get_binned_analysis_tool_forwards_units_through_fastmcp`
    calls the bound tool via FastMCP with `units="engineering"` and asserts the
    daemon request carries `units=engineering` (default is not sent).
    `pytest tests/test_mcp_*.py tests/test_binned_analysis.py tests/test_layering.py`:
    284 passed; ruff, ruff format, mypy src clean. `docs/mcp-server.md` already
    documented `units`.

- [ ] **R29.5 Fifteen MCP tools are closures that restate their delegate's
  signature.** `record_feedback` and `record_vehicle_event` exist only to
  narrow a `str` to a `Literal`, which `bind_delegation` and FastMCP already
  do. `search_anomaly_log` has 14 parameters in the closure and 16 in the
  delegate. `VehicleEventKind` is defined twice, restated as two tuples, and
  the daemon's `VehicleEventRequest.kind` is a bare `str`.
  - [ ] Convert the pure forwards to delegations with `Literal` parameters.
    Keep closures only where `cfg`-derived arguments or conditional
    registration need one.
  - [ ] A test that every remaining closure's parameter names equal its
    delegate's. It would have caught R29.4.
  - [ ] One definition of the event-kind literal, used by the route schema
    too.

- [ ] **R29.6 `create_app` threads about 45 daemon objects through four
  layers.** The same names are spelled out in `create_app`'s signature, the
  `register_public_routes(...)` call, the `register_control_routes(...)` call,
  `ControlContext(...)` and `daemon/server._build_app`. `ControlContext`
  (R03.6) solved this for `/control/*`, where the auth dependency is applied
  at router level "so no route can be registered without it". Five non-control
  modules (`calibration`, `rom_editor`, `rom_library`, `rom_maps`,
  `workbench`: 30 routes) each build the dependency and attach it per route.
  - [ ] One frozen app context built in `create_app` and passed to every
    `register_*`; non-control modules use its `router()`.
  - Must not change: `create_app`'s keyword signature (52 files call it), the
    route table snapshot.

- [ ] **R29.7 Route modules hold domain logic in closures.**
  `register_public_routes` is one 383-line function with 17 closures covering
  status, telemetry reads, catalog reads, OBD coverage, binned analysis, LLM
  cards and trip summary. Logic that cannot be tested without building an app:
  `_polled_did_names` / `_decoded_did_rows` (DID token resolution and raw-row
  decode) in `public.py`; `_vin_refusal` (a five-rung precondition ladder) and
  the `get_control_profiles` payload shaping in `routes/control/system.py`;
  the storage-isolation enter/exit transaction in
  `routes/control/bus.py` `post_control_tuning_mode`. `rom_library.py`'s
  `vehicle_reading` is imported by `system.py`: one route module serving
  another.
  - [ ] `public.py` → `status.py`, `catalog_reads.py`, `analysis_reads.py`.
    The DID helpers to `dashboard/readers.py` or `decode/dids.py`;
    `_vin_refusal` to `control/`, taking a `VehicleReading`;
    `vehicle_reading` to `vehicle_state.py` or `control/`.
  - [ ] Operator question: `/control/tuning-mode` and `/control/capture` look
    like two lifecycles for one thing. Does the dashboard still use the
    former?

- [ ] **R29.8 Four `async def` handlers do file work on the event loop, and
  the deferral preamble is copied.** `routes/workbench.py`
  `get_session_frame` and `get_cell_residency`,
  `routes/control/classification.py` `post_session_classification` and
  `routes/control/replay.py` `post_replay_start` call `resolve_session`,
  `select_sessions` (which opens every parquet chunk footer of the session)
  or `derive_vehicle_state_from_storage` directly. The preamble
  `capture_deferral` → `select_sessions` → `409 analysis_refused over_budget`
  is written three times with an identical dict. `run_off_loop` is documented
  as "the only way a route may compute one" and does not defer by itself, so
  each route has to remember.
  - [ ] `prepare_single_session_job(config, session_id, …)` in
    `workbench_jobs`, doing resolve, defer and budget off the loop.
  - [ ] `run_off_loop` calls `capture_deferral` itself.

- [ ] **R29.9 The dashboard polls the full `/status` every 5 s.**
  `hooks/useStatus.ts` calls `getStatus`, which requests `/status` with no
  `view`. The `get_status` tool's docstring records the full payload at
  1.3–2.6 s on the Pi and points pollers at `view=compact` (about a tenth of
  the cost on the laptop). Each full poll globs and parses every model
  manifest twice, lists every session directory twice (`latest_session`
  19 ms and `latest_capture_summary` 34 ms at 523 sessions on the laptop),
  re-reads the tune registry and re-validates the DID catalog. A second
  defect rides on it: `HealthMonitor._cpu_percent` is the delta since the
  previous `snapshot()`, and every `/status` and MCP `get_status` calls
  `snapshot()`, so the reading depends on who polled last.
  - [ ] Time `GET /status` against `?view=compact` on the Pi and record both
    here.
  - [ ] The dashboard polls `view=compact` and fetches the full block only
    where a panel shows it.
  - [ ] Memoize the near-static blocks on file identity
    (`(path, mtime_ns, size)`, as `dids._parsed_yaml` does). R24.3 is the
    shared helper.
  - [ ] A host sampler that owns the CPU baseline and samples on its own
    clock.

- [ ] **R29.10 `HealthMonitor` is five jobs in one class.** 534 lines, 31
  methods: inference latency accounting; detector-state mailboxes that stash a
  value for `snapshot` to echo (seven `observe_*` / `note_*` setters); sink,
  queue and loss-epoch accounting; OBD scheduler passthrough; stateful host
  metrics. The host readers (`system_metrics`, `power_status`,
  `can0_counters`, a `vcgencmd` shell-out) are another 170 module-level lines
  in the same file.
  - [ ] `daemon/host_metrics.py` (readers and the sampler from R29.9) and
    `daemon/sink_health.py` (with `CaptureQualityInputs`, `LossEpoch`).
    `HealthMonitor` stays as the facade, so `/status` keys and every
    `health_monitor.observe_*` call site are unchanged.
  - [ ] `set_detector_block(name, value)` in place of the seven mailboxes.

- [ ] **R29.11 The live observers repeat one skeleton and `_snapshot` calls
  them five different ways.** `lambda_delivery_live`, `proposals_live`,
  `twin_live`, `plausibility_live` and `committees_live` each carry
  `_last_tick_ms`, an `observe_failures` counter, a guarded
  `record_observers` hook, a `"cost"` block and a `tick_live` scoped by
  `live_session_key`. `LiveInferencePipeline._snapshot` (187 lines) calls
  them with five argument shapes, each in its own `try / except Exception`,
  and recomputes `_live_vehicle_state` up to three times a tick.
  `InferenceTimingRecorder` has five `set_*` setters for the five blocks.
  - [ ] A `LiveObserver` protocol and a `TickCost` helper; `_snapshot` loops
    over the observers through one guard; `set_block(name, mapping)`.
  - [ ] Split `_snapshot` into build-row, gate-scoring, run-observers and
    prepare-jobs.
  - Not a shared base class: cadence differs (twin and proposals hand work to
    a worker, lambda delivery runs inline). Serve path: replay a fixed session
    before and after.

- [ ] **R29.12 `run_daemon` (382 lines) keeps its start order and its
  teardown order as two hand-maintained lists.** Tasks are created in four
  places and cancelled by a separate tuple in `finally`; a new task needs both
  edits and a missed one outlives shutdown. `DaemonComponents` has eight
  fields assigned late and typed `X | None`, so every consumer null-checks.
  `drain_kwargs()` returns `dict[str, Any]` splatted into the 22-parameter
  `drain_queue_with_broadcast`, losing types at the seam R04.13 called
  dangerous. `CANARY_CONFIG`'s default is written in two files.
  - [ ] A task registry (`spawn(name, coro)`, `cancel_all()` in reverse
    order).
  - [ ] `drain_kwargs` returns a frozen `DrainWiring` dataclass.
  - [ ] `validation/harness._start_pipeline` is a second composition root;
    give both one `start_pipeline(components, …)`.

- [ ] **R29.13 `RomBackupJob` is a 920-line class.** Lifecycle (~150 lines),
  preconditions (90), the wire protocol (~275: `_listen`, `_send`,
  `_session_step`, `_read_block`, `_reread`, …) and post-processing that needs
  no bus (~150: `_store`, `_integrity`, `_size_caveat`). A
  `counters: dict[str, Any]` is threaded through six methods and mutated by
  key. The recipe schema is another 140 lines above it.
  - [ ] `rom_backup_recipe.py`, `rom_backup_wire.py` (a reader with a counters
    dataclass), `rom_backup_store.py`. `RomBackupJob` and `BackupRefusedError`
    stay importable from `src.control.rom_backup`.
  - This is a read path (service 0x23), not a non-volatile write. Behavior
    identical; pinned by the scripted-ECU tests.
  - `brain_jobs.py` (1,155 lines) splits the same way: store and index, kind
    runners and `precheck`, scheduler.

- [ ] **R29.14 "Engine running" is defined three ways, and the non-volatile
  write guard uses the weakest.** `classify_vehicle_state` uses the OBD rpm
  while OBD is fresh (10 s) and the broadcast rpm only otherwise. So a last
  OBD `0` followed by a start classifies as `ignition_only` until the OBD
  value ages out, while the broadcast already reads 800.
  `rom_backup._engine_running` documents exactly this and also refuses on a
  fresh broadcast rpm at or above the running threshold.
  `NonvolatileWriteGuard._check_signals` does not: it requires
  `ignition_only`, `rpm == 0` and OBD younger than
  `max_signal_age_seconds` (5 s), and `NonvolatileWriteSignals` carries no
  broadcast rpm. Nothing outside tests calls `authorize_nonvolatile_write`
  yet, so no write is exposed; this is the gate `AGENTS.md` calls "the line
  that actually matters", and it should be closed before the first caller.
  The state-name sets are also restated in five modules
  (`capture_autostart.RUNNING_STATE`, `key_cycle_pairing.ON_STATES`,
  `idle_shutdown.DEFAULT_ACTIVE_STATES`,
  `inference._SCORABLE_VEHICLE_STATES`, `tuning_readiness`), which is how
  `parked` (2026-10-04) needed several edits.
  - [ ] A test first: stale-but-fresh OBD rpm 0 with a fresh broadcast rpm of
    800 must be refused by the guard. Expect it to fail today.
  - [ ] A fresh-gated `broadcast_rpm` on `VehicleState`, one
    `engine_running_evidence(state)` in `vehicle_state.py` used by the guard
    and by `rom_backup`, and named state sets there that the five modules
    import.
  - Deep tier (near the non-volatile-write conditions). No change to what
    `ignition_only` means for anything else.

- [ ] **R29.15 Dead code.**
  - `src/companion/services.py`, about 115 of 421 lines: `companion_config`,
    `write_companion_config`, `_library_session_path`,
    `_dataset_row_to_dict`, `_require_optional_bearer` (a copy of the check in
    `dashboard/security.py`), `_unlink_path`, `feedback_list_response`,
    `submit_feedback_response`. Their callers went with the standalone
    companion app on 2026-10-16; the module's `__all__` exports
    `_`-prefixed names, which kept them looking live.
  - `control/trip_end.handle_trip_end`, `control/twin_identity.segment_index`.
    `dashboard/schemas.TripSummaryResponse` has no Python user; the
    TypeScript type's comment names it as the model it mirrors, so either use
    it as the route's response model or fix the comment.
  - Test-only: `drain.reset_drop_bursts`, `trip_end.digest_interrupted`,
    `schemas.DtcReportResponse`.
  - Checked by word-grep over `src`, `scripts`, `tools`, `tests`, `docs`.

- [ ] **R29.16 Small items.**
  - The active profile slug is recovered as
    `config.collection.storage_path.name` in three places
    (`routes/control/system.py`, `dashboard/status.py`,
    `vehicle/profiles.py`). One `CanaryConfig` property.
  - `MAX_ROM_UPLOAD` and the "octet-stream, stream with a size cap" block are
    in both `routes/rom_maps.py` and `control/rom_library.py`.
  - `JobController._emit` has the one silent `except Exception: pass` in
    `control/`.
  - `companion/services._download_session_export` holds a multi-hundred-MB
    zip in memory twice (`read_bytes`, then `BytesIO`). Stream from the file.
  - `CaptureProfileStore` and `CaptureAutostart` are each constructed in two
    places; the second exists for tests.
  - `docs/mcp-server.md` has table cells several kilobytes long. Move the
    per-tool prose under headings so the table is a table.

The path-keyed lock registry and locked JSONL append found here
(`brain_jobs`, `evidence/ledger`, `vehicle_ledger`) are R24.9.

## Looked at and fine

- Function-level `from src…` imports in `daemon/` (61), `dashboard/` (31)
  and `control/` (19) are boot-time laziness on the Pi, not hidden cycles.
- `except Exception` in `daemon/` (52) and `control/` (48) is nearly all
  "best effort, logged, with a named reason".
- `drain_queue_with_broadcast`'s `add_sink` table is the right shape; only
  its untyped wiring is a finding (R29.12).
- There is no single job abstraction and there should not be: subprocess
  jobs, async child jobs, a one-at-a-time thread job and the update runner
  have different lifecycles. The persistence primitive is the common part.

## Not reviewed

`src/validation/{evidence,scenarios,scripted_ecu,synthetic_bus}.py`;
`src/provisioning/{bluez,netsock}.py`; `inference_worker.py` internals;
`twin_live.py` body; `daemon/detectors/`; the bodies of `replay_control.py`,
`update_runner.py` and `routes/companion_link.py`.
