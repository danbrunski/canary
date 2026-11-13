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

- [~] **R29.1 Whole-session analysis still runs in-process on the Pi through
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
  - [x] A `trip_summary` job kind in `workbench_jobs.JOB_KINDS`; the route
    calls `run_off_loop`. Test: the route during a capture answers 409
    `analysis_deferred`.
    `trip_summary` added to `JOB_KINDS` and `_CONFIG_ONLY_KINDS`; its child
    entry returns `{"trip_summary": get_trip_summary(...)}` (an explicit `None`
    for a session with nothing to summarize). `GET /sessions/{id}/trip-summary`
    is now `async` and calls `control.trip_summary.trip_summary_off_loop`: a
    sidecar on disk at `SCHEMA_VERSION` is served directly (read in
    `asyncio.to_thread`, capture or not), a missing or stale one goes through
    `capture_deferral` then `run_off_loop`; `WorkbenchRefusedError` becomes the
    route's 409/503 body. Auth unchanged (public;
    `tests/test_route_table_snapshot.py` green); `TripSummarySidecar` and
    `SCHEMA_VERSION` untouched. New `tests/test_trip_summary_route.py` (9 tests):
    first request computes in the child (`jobs == ["trip_summary"]`) and the
    second reads the sidecar (no job); an existing sidecar is served during a
    capture with no job; **the first request during a capture is 409
    `analysis_deferred` (`reason: capture_active`, `profile`, `laptop_path`
    naming the session), no job, no sidecar written**; an older-schema sidecar is
    recomputed; unknown session 404 without a job; no-telemetry session 404 from
    the child; busy child 409 `analysis_busy`; the child entry's return shape.
    `test_dashboard_api.py::test_get_session_trip_summary_*` pass (the compute
    test now uses a thread-pool stand-in for the child). No span/row preflight
    was added (the item did not ask, and it would be a new refusal on a public
    route): the child's `RLIMIT_AS` ceiling and 900 s timeout bound it, as 503
    `analysis_unavailable`.
  - [~] MCP `get_delivery_error` and `compare_tuning_sessions` become proxies
    of the workbench routes (the `delivery_error` and `pull_comparison` kinds
    exist).
    **Not done: the tools are not equivalent to the routes, so a proxy would
    change what they answer.** `get_delivery_error` is the single-session
    `analysis.tuning.delivery_error` (`pairing`, `eligibility`,
    `delay_sensitivity`, `capabilities`, `min_load_source`, `engine_version: 2`,
    documented in `docs/mcp-server.md`); `/analysis/delivery-error` is
    `workbench.pooled_delivery_error` over a session pool (per-session blocks
    plus `selection`, no `capabilities`/`min_load_source`). `compare_tuning_sessions`
    is `analysis.tuning.compare_sessions` (session A vs session B, `axes`,
    `unsafe_axes`, `conditions` required beside `floor_channel`, `distinguishable`,
    `floor_refusal`); `/analysis/pull-comparison` is `workbench.pull_comparison`
    (revision A vs revision B, fixed `rpm` x `airflow`, hard-coded
    `ALL_CONDITIONS`, "choose two different revisions" 400). `axes`,
    `unsafe_axes` and `conditions` have no route parameter (mapping `conditions`
    away would quote a floor against the wrong regime, the R05.2 constraint), a
    same-revision A/B (the null run the noise floor comes from) is impossible
    through the route, and the proxy would also lose the laptop path the
    `analysis_deferred` body points at ("canary MCP tuning tools", on a pulled
    corpus, no daemon). What the item was for (no whole-session pandas in
    `canary-mcp` while a capture records) is covered for both tools by the
    capture guard below. Decision in the report: replace them with
    pooled-route proxies and a new response contract, make the routes grow the
    missing parameters, or leave them local behind the guard (recommended).
  - [x] `query_telemetry`, `get_behavior_deviations` and `search_anomaly_log`
    refuse while a capture is active, and `docs/mcp-server.md` says so.
    New `src/mcp_server/capture_guard.py::capture_refusal(client, tool=...)`
    asks the daemon `GET /status?view=compact` (`capture_profile.active`, the
    same fact `capture_deferral` reads) before each call and returns
    `{"ok": False, "error": "analysis_deferred", "reason": "capture_active",
    "tool", "profile", "message", "laptop_path"}`. **Fail open only when the
    connection cannot be made** (`httpx.ConnectError` / `ConnectTimeout` as the
    `DaemonError`'s cause: refused, no route, name unresolved; the laptop with a
    pulled corpus and no daemon must keep working, and nothing that accepts no
    connection is collecting). **Everything else fails closed**,
    `capture_state_unknown`: a read timeout (a daemon that connected and then
    stalled past the 2 s probe is the busiest one, so it is refused, not waved
    through), an HTTP error, a body that is not JSON (`DaemonClient._request`
    lets the `JSONDecodeError` escape; caught as `ValueError`), a body without
    `capture_profile.active`. The five local tools (the three named plus
    `get_delivery_error` and `compare_tuning_sessions`, which stay local per the
    item above) are `async` closures in `server.py` that call it first. Tests,
    `tests/test_mcp_capture_guard.py` (15): active -> named refusal with the
    laptop path; inactive -> runs; refused port and `ConnectTimeout` -> fail
    open; read timeout (mock transport) and a real listening socket that never
    answers (probe timeout patched to 0.2 s) -> `capture_state_unknown`; seven
    unreadable answers (500, missing field, non-bool, non-object, a 200 with a
    non-JSON body, a truncated JSON body) -> fail closed; all five tools refused
    through FastMCP while a capture records; the tools still return rows with no
    capture and with no daemon. Snapshot regenerated
    (`CANARY_UPDATE_MCP_SNAPSHOT=1 pytest -n0 tests/test_mcp_tool_snapshot.py`):
    the diff is the description text of those five tools (one added sentence
    each), no input schema changed. `docs/mcp-server.md` has the new section
    "Local analysis tools refuse while a capture records (R29.1)" with both
    failure policies (fail open only when no connection can be made) and the
    mixed-config caveat (a `canary-mcp` on a laptop
    pointed at the Pi's daemon is refused while the Pi captures).
  - [x] `capture_deferral` on `/analysis/binned`.
    `routes/public.py::get_binned_analysis` calls `capture_deferral(config,
    [session] if session else None)` after its 400 input checks and before
    `binned_analysis_response`, and maps `WorkbenchRefusedError` to
    `HTTPException(exc.status, exc.detail)`; the route stays public. Test
    `tests/test_analysis_binned_route.py::test_binned_analysis_is_deferred_while_a_capture_records`:
    200 with no capture, 409 `analysis_deferred` (profile, `laptop_path` naming
    the session) while one records, unscoped read too, bad `agg` still 400.
    `docs/headless-api.md` rows for both public routes updated.
  - Must not change: the sidecar format and `trip_summary.json` schema
    version. Held: `SCHEMA_VERSION` and `TripSummarySidecar` unchanged,
    `tests/test_trip_summary_sidecar.py` and `tests/test_route_table_snapshot.py`
    pass.

- [x] **R29.2 The bounded-child runner exists four times and "one analysis
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
  - [x] `child_pool.run_in_child(call, *, timeout, executor=None)` returning
    an outcome (`TIMED_OUT | BROKEN | OOM`) and owning pool creation and
    discard. Each module keeps its own exception types and messages.
    `child_pool.run_in_child(call, *, timeout, holder, executor=None,
    slot_wait=0.0, in_process_call=None)` returns the call's result, or raises
    `ChildFailedError` whose `outcome` is `ChildOutcome.TIMED_OUT | BROKEN | OOM
    | NOT_STARTED` (and `.cause` the original exception); the work's own
    exceptions cross unchanged. It owns pool creation (`spawn_pool`), the
    timeout, and discard (terminate unless the child answered; the slot stays
    taken until a terminated child's process has exited). `holder` is required:
    claiming the slot cannot be left to the caller. `in_process_call` is the
    uncapped call run when a test hands a thread pool, so `RLIMIT_AS` never
    lands on the test process (`capture_finalize`'s old per-pool decision,
    pinned by `test_capture_quality_reads`). Ported:
    `trip_end._in_digest_child` (the three `TripEndDigestUnavailableError`
    reasons and messages, `MemoryError` re-raised unchanged),
    `capture_finalize._run_recompute` (`CaptureQualityUnavailableError`, every
    message kept) and `workbench_jobs._compute` (the 503 `analysis_unavailable`
    bodies, `child_timed_out`/`child_failed`/`child_out_of_memory`, kept;
    a pool that cannot start is still the raw exception). `workbench_jobs` now
    imports `limit_address_space` from `child_pool` and nothing private from
    `trip_end`; the `_spawn_pool`/`_discard_pool` aliases are deleted from
    `trip_end` and `capture_finalize`. **`BrainScheduler._run` is left on its own
    lifecycle** (it already uses `child_pool.spawn_pool`/`discard_pool` and the
    registry, no private names): it polls the future every `poll_seconds` for a
    reason to yield, cancels it and records `deferred`, and times the job on its
    own clock, and it is the tenant that gives way to the analysis slot, so
    routing it through the slot-taking `run_in_child` would remove the exclusion
    it exists to honor. Regrowth guard
    `tests/test_child_pool.py::test_no_fifth_copy_of_the_bounded_child_runner`:
    no `src/` module outside `child_pool.py` and `brain_jobs.py` calls
    `spawn_pool(`, none names `_spawn_pool`/`_discard_pool` (the list may only
    shrink). `tests/test_child_pool.py` also pins each outcome (not started,
    broken, OOM, timed out), the work's own `ValueError` crossing unchanged,
    owned-pool discard (terminate only on failure), and pickling of both new
    exception classes (`test_exceptions_pickle.py` passes).
  - [x] One exclusive slot in `child_pool` with the existing yield-and-wait
    protocol, which `capture_finalize` joins.
    `child_pool.ANALYSIS_CHILD` is one slot claimed by the digest (compute,
    tuning refresh, tire measurement: holder `digest`), every workbench job
    (`workbench`) and the capture recompute (`capture_recompute`), through
    `run_in_child`; the claim is synchronous, so two callers on one loop cannot
    both win it, and a refusal is `AnalysisSlotBusyError` naming the holder.
    The brain protocol is kept: `run_in_child` asks a running brain child to
    yield and waits, bounded by `CHILD_EXIT_BOUND_SECONDS`, for its process to
    be gone (goes ahead and logs past the bound, as the digest did);
    `run_off_loop` still refuses `brain_child_yielding` before spawning and still
    shares an identical in-flight request, and its `child_busy` 409 now covers
    the slot (body gains `holder` when another child, not another workbench job,
    has it) beside the existing `_IN_FLIGHT` and digest-in-flight checks (a
    digest queued for the slot is not jumped by a new workbench request).
    `BrainScheduler.blocked()` and `_yield_reason()` also yield to
    `analysis_holder()` (reason `<holder>_running`, so `capture_recompute_running`
    is new; `digest_running` and `workbench_running` unchanged).
    **Recompute waits, bounded, then is refused by name** (decided from its
    callers): `finalize_capture` is awaited inside `ProfileCaptureSession.stop`
    after the boundary, drain and flush, reconciles against the in-memory loss
    epoch of the capture that is ending, and never raises, so a refusal drops
    the one verdict that cannot be redone with the same inputs; and the usual
    holder at that moment is the trip-end digest of the drive that just
    stopped, so refusing would lose the verdict in the common case. The boot
    path (`finalize_interrupted_capture`) runs before any other child can exist.
    The wait is `RECOMPUTE_SLOT_WAIT_SECONDS = 120` (twice the ~60 s a 20-minute
    capture takes), because a digest can hold the slot for 15 minutes; past it
    `CaptureQualityUnavailableError` names the holder, which every caller already
    maps (the stop records no verdict, read as unknown, never a pass; the routes
    answer 503). The two interactive callers keep the default (live and
    historic recompute routes). Other callers choose: the tuning refresh, which
    serves an HTTP request, passes `slot_wait=0` (refused by name, a `failed`
    note on the stored digest) instead of queueing for up to 900 s; the trip-end
    digest and tire measurement wait up to their own timeout (a shutdown deadline
    passed as `timeout` bounds it); the workbench never waits (409). **Shutdown is
    bounded by the unit's stop deadline** (found by the verifier: the capture
    stop runs before the trip-end settle, so a stop that found a GPS-edge digest
    holding the slot waited its own 120 s past `TimeoutStopSec=30`).
    `recompute_quality_off_loop`, `finalize_capture` and
    `ProfileCaptureSession.stop` take `deadline` (a `loop.time()` instant);
    `run_daemon` passes `shutdown_started + TRIP_END_SHUTDOWN_SECONDS`, the one
    instant the trip-end work is held to. The slot wait becomes
    `min(slot_wait, remaining)`, and an `asyncio.timeout_at(deadline)` around the
    whole coordinated recompute also covers what has no bound of its own (the
    queue behind another recompute, the brain-exit wait, spawn, the compute): past
    it the child is terminated, the slot released and
    `CaptureQualityUnavailableError("... did not finish before its deadline; the
    analysis child is held by digest")` raised. The stop then still stamps the
    capture (`daemon_shutdown`, real `ended_at`) with no `capture_quality.json`
    (unknown, never a pass; the rows are intact and a recompute works once the
    slot is free). Nothing recomputes it automatically at the next boot (see the
    report: `recover_interrupted_capture` only takes unclosed captures).
  - [x] Test: a recompute requested during a running digest waits or is
    refused, by name.
    `tests/test_analysis_child_registry.py`:
    `test_a_recompute_requested_during_a_digest_is_refused_by_name_past_its_wait`
    (a digest holds the slot via `_in_digest_child`; the recompute with
    `slot_wait=0.2` raises `CaptureQualityUnavailableError` matching "held by
    digest", and the scoring never ran),
    `test_a_recompute_requested_during_a_digest_waits_for_it_to_finish` (it is
    pending while the digest runs, runs after it), the reverse
    (`test_a_digest_waits_for_a_recompute_and_is_refused_by_name_past_its_timeout`),
    `test_the_workbench_refuses_busy_naming_the_holder_while_a_recompute_runs`,
    `test_the_brain_yields_to_whoever_holds_the_analysis_slot`,
    `test_a_request_serving_caller_is_refused_at_once_not_queued`. The existing
    brain tests (`test_brain_jobs`, the registry file's yield tests) pass.
    Deadline: `test_a_deadline_ends_the_wait_for_a_digest_however_long_the_slot_wait_is`
    (default 120 s wait, 0.3 s deadline, digest undisturbed),
    `test_a_deadline_cancels_a_recompute_that_is_still_computing_and_frees_the_slot`,
    `test_a_deadline_also_bounds_the_queue_behind_another_recompute`;
    `tests/test_profile_capture.py::test_a_shutdown_stop_gives_up_on_a_digest_at_its_deadline_and_still_stamps`
    (capture stamped `daemon_shutdown`, no sidecar, slot free, a later recompute
    works) and `::test_run_daemon_shutdown_with_a_digest_holding_the_slot_fits_the_unit_stop_deadline`
    (the whole `run_daemon` shutdown with a digest holding the slot ends inside
    `UNIT_STOP_TIMEOUT_SECONDS`; the stop itself under 5 s with the budget patched
    to 1 s; fails with a hang-guard timeout when the stop is not given the
    deadline, checked); `test_run_daemon_shutdown_stops_through_the_session` now
    also asserts the stop is handed a deadline. Cleanup: `run_in_child` releases
    the slot in a `finally` of its own and `discard_pool` terminates the child
    even when `pool.shutdown` raises (`tests/test_child_pool.py::test_a_pool_whose_cleanup_raises_still_frees_the_slot`
    both ways, fails against the old shape, checked;
    `::test_a_shutdown_that_raises_still_terminates_the_child`). The regrowth
    guard also refuses a `ProcessPoolExecutor(` constructed directly anywhere
    under `src/control/` outside `child_pool.py`.

- [x] **R29.3 Four `(status, detail)` exception classes and 38 hand-written
  remaps to `HTTPException`; no app-level handler.**
  `WorkbenchRefusedError`, `BrainJobRefusedError` and `BrainReadError` have
  identical bodies; `ProfileCaptureError` differs by allowing a string
  detail. `src/dashboard` has 169 `raise HTTPException`, 38
  `except <refusal> as exc: raise HTTPException(exc.status, exc.detail)`
  blocks, and no `exception_handler`. A route that forgets the `except`
  answers 500 instead of the named refusal.
  - [x] One `StatusError(Exception)` (status, detail, pickle-safe) in the
    lowest layer that needs it; the four subclass it; `create_app` registers
    one handler that returns `{"detail": …}`, the body `HTTPException`
    produces. Delete the remap-only blocks. This is the HTTP face of R24.1
    (`ReasonedRefusal`); design the two together.
    Evidence: `src/control/status_error.py` (new; layer 6, the lowest that
    needs it: every subclass is a `control` refusal, nothing below answers
    HTTP): `StatusError(status, detail)`, generic in the detail type
    (`StatusError[dict[str, Any]]` for `WorkbenchRefusedError`,
    `BrainJobRefusedError`, `BrainReadError`; `StatusError[str | dict[str, Any]]`
    for `ProfileCaptureError`, whose `render` keeps `str(detail)` for a dict),
    pickling through `src.refusal.rebuild_exception`. Relation to
    `ReasonedRefusal`: siblings, not parent and child (a `ReasonedRefusal` has a
    required stable `reason` and no status; a `StatusError` is an HTTP answer and
    not every one has a code: a capture's refusal is a sentence). The handler is
    `src/dashboard/status_errors.py`: it builds an `HTTPException(status, detail)`
    and hands it to FastAPI's own `http_exception_handler`, so body, status,
    headers and the no-body statuses are `HTTPException`'s by construction.
    `create_app` registers it, and each registrar that mounts a route able to
    raise one (`register_workbench_routes`, `register_rom_editor_routes`,
    `register_public_routes`, `register_control_routes`) installs it too
    (idempotent): the first run without that left 8 tests that build a bare
    `FastAPI()` with one registrar answering an unhandled error
    (`test_a_child_that_times_out_is_the_workbenchs_503` and seven more);
    `test_every_route_module_that_can_raise_a_refusal_installs_the_handler`
    scans the route modules so a new one cannot forget.
    Remap blocks: `raise HTTPException(exc.status, exc.detail)` /
    `(status_code=exc.status, detail=exc.detail)`: 18 sites in 6 files (the
    checklist's 38 had drifted; the strict `exc.status,` spelling is 11), all
    deleted (`workbench.py` 6, `profile_capture.py` 5, `classification.py` 2,
    `brain.py` 2, `public.py` 2, `rom_editor.py` 1); a `try` left with no
    handler was unwrapped. Kept: the inner `except WorkbenchRefusedError` in
    `rom_editor.post_rom_simulate` (re-raises with a `laptop_path` field added),
    and every `except` that changes the status or message of another exception
    type (`GateError` 400, `UnscopedMemoryError` 503, `CaptureQualityUnavailableError`
    503, `SimulationRefusalError` 409 with `reason`/`message`, ...). Regrowth
    guard: `tests/test_source_rules.py` row `remap-only-http-exception` (AST;
    allowlist empty; against a `git archive HEAD` tree it finds 18 in 6 files,
    now 0). Behavior unchanged on HTTP: `tests/test_status_error_http.py`
    pins `(status, body)` literally for every one of the 18 sites (12 patched
    callees with a status of its own, plus the real capture-deferral, capture
    session and brain refusals); it was written and passed against the tree
    before any block was deleted (16 passed at HEAD), and the same literals pass
    after. Equivalence of the handler with `HTTPException` (body, status, headers)
    over nine `(status, detail)` pairs including 204 and 304.
  - [x] Pinned by `tests/test_exceptions_pickle.py` and
    `tests/test_route_table_snapshot.py`.
    Evidence: `pytest -m "" -n 4` over `test_exceptions_pickle` (226),
    `test_status_error_http` (30), `test_route_table_snapshot`,
    `test_layering`, `test_mcp_tool_snapshot` and 11 route test files: 556
    passed, 1 failed (`test_source_rules[parquet-glob-literal]`,
    `src/storage/telemetry_query.py` is another session's uncommitted edit);
    `tests/fixtures/route_table_snapshot.json` untouched.

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

- [x] **R29.14 "Engine running" is defined three ways, and the non-volatile
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
  - [x] A test first: stale-but-fresh OBD rpm 0 with a fresh broadcast rpm of
    800 must be refused by the guard. Expect it to fail today.
    `tests/test_nonvolatile_write.py::test_a_fresh_broadcast_rpm_refuses_while_the_last_obd_zero_is_still_fresh`
    builds the state through `derive_from_inputs` (OBD rpm 0 at 1 s, CAN
    0.1 s, broadcast 800) and asserts the premise: verdict `ignition_only`,
    rpm 0, OBD age under both 5 s and 10 s. Against the unchanged guard it
    failed with `DID NOT RAISE AuthorizationRefusedError` (the guard
    granted), as did the between-blocks variant
    (`test_a_broadcast_start_between_blocks_aborts_before_the_next_write`,
    broadcast 760 at block 1); both pass after the fix. The premise held.
  - [x] A fresh-gated `broadcast_rpm` on `VehicleState`, one
    `engine_running_evidence(state)` in `vehicle_state.py` used by the guard
    and by `rom_backup`, and named state sets there that the five modules
    import.
    `classify_vehicle_state` sets `broadcast_rpm` only while CAN is fresh
    (its existing rule), so every `derive_*` path carries it and `to_dict()`
    serialises it. `engine_running_evidence` takes any reading with
    `state`/`rpm`/`broadcast_rpm` (`VehicleState` or `rom_backup.VehicleReading`)
    and returns `rom_backup`'s old wording verbatim. The guard keeps every
    old refusal and adds `broadcast_engine_running` after the OBD-age and
    `engine_running` checks. `routes/rom_library.vehicle_reading` reads
    `VehicleState.broadcast_rpm`; its `# type: ignore[arg-type]` went by
    typing `derive_vehicle_state_from_storage`'s `replay_controller` as
    `ReplayStatusSource` (all it uses). Groups `ENGINE_ON_STATES`,
    `KEY_ON_STATES`, `UNTRUSTED_STATES`, `ECM_SILENT_STATES`, `IDLE_STATES`,
    `SCORABLE_STATES` (beside `QUIET_STATES`) are imported by
    `capture_autostart` (`RUNNING_STATES`), `key_cycle_pairing`
    (`ON_STATES`, `UNTRUSTED_STATES`), `idle_shutdown` (both defaults),
    `inference._SCORABLE_VEHICLE_STATES` and `tuning_readiness` (ready =
    engine on, not ready = ECM silent); membership unchanged and pinned.
    `config.collection.IdleShutdownConfig.idle_states` is layer 0 and cannot
    import them, so a test pins it to `IDLE_STATES`. Regrowth guard:
    `test_no_new_string_set_of_state_names_outside_vehicle_state` (AST scan of
    `src/`, allowlist may only shrink: that config default and `rom_backup`'s
    mid-read `("ignition_only", "passive")`). Verdicts unchanged:
    `test_no_verdict_changed_when_broadcast_rpm_joined_the_state` compares
    every one of 2304 input combinations against a verbatim copy of the old
    rule table. Tests: `tests/test_vehicle_state.py`,
    `test_nonvolatile_write.py`, `test_rom_backup.py`,
    `test_rom_library_api.py`, `test_capture_autostart.py`,
    `test_idle_shutdown.py`, `test_key_cycle_pairing.py`,
    `test_inference_profile_gate.py`, `test_ecu_calibration_id.py`,
    `test_daemon.py`, `test_dashboard_api.py`, `test_layering.py` green;
    ruff, format, `mypy src`, `pnpm typecheck` clean.
  - Cranking gap closed (orchestrator decision after verification): the
    guard also refuses `broadcast_rpm_nonzero` when a fresh broadcast rpm is
    not exactly 0, so 1-399 rpm behind a fresh OBD `0` no longer authorizes.
    `engine_running_evidence` and `rom_backup` are unchanged: the guard is
    deliberately stricter than the backup job (its docstring says so).
    Tests: `test_a_cranking_broadcast_rpm_below_the_running_threshold_refuses`
    (1, 56.25, 399), and
    `test_a_broadcast_rpm_authorizes_only_at_exactly_zero_or_without_fresh_evidence`
    (None, 0, stale 800). KOEO check, offline: all 264 sessions in
    `~/canary-data/sessions/nissan-xterra-2008`, every `0x1F9` frame
    (bytes 2-3 big-endian / 8, the `ECM_RPM` binding) whose latest OBD
    `RPM` was 0 and at most 10 s old. Steady KOEO (next OBD `RPM` also 0,
    within 10 s): 2,484,156 frames in 89 sessions, min 0, max 56.25,
    **1 nonzero** (56.25 rpm in `_capture-tuning-2026-09-23T142926`, which I
    did not inspect further). Frames where OBD changes next (starts):
    47,214 frames in 91 sessions, 1,925 nonzero, max 1868.75; e.g.
    `2026-10-26T003705` ramps 184 → 1818 rpm within 1.3 s of an OBD `0`.
    So a stopped engine's broadcast reads exactly 0.
  - `key_cycle_pairing` re-exports `UNTRUSTED_STATES` explicitly
    (`as UNTRUSTED_STATES`), because `mypy` on `tests/` refused the
    implicit re-export.
  - Behaviour changes:
    - `/status.vehicle_state` (and MCP `get_status`, and the guard's audit
      `signals.vehicle_state`) gains `broadcast_rpm`.
    - New guard refusal codes `broadcast_engine_running` and
      `broadcast_rpm_nonzero`.
    - `idle_shutdown.DEFAULT_IDLE_STATES` order is now
      `("engine_off", "no_data", "parked")`, with the same members.
    - `capture_autostart.RUNNING_STATE` (a string) became `RUNNING_STATES`
      (a frozenset); nothing imported the old name.
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
