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

- [~] **R29.7 Route modules hold domain logic in closures.**
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
  - [x] `public.py` → `status.py`, `catalog_reads.py`, `analysis_reads.py`.
    The DID helpers to `dashboard/readers.py` or `decode/dids.py`;
    `_vin_refusal` to `control/`, taking a `VehicleReading`;
    `vehicle_reading` to `vehicle_state.py` or `control/`.
    **Route modules** (`src/dashboard/routes/`): `status_routes.py` (named so it
    is not mistaken for the payload builder `dashboard/status.py`; `/identity`,
    `/status`, `/config`, `/anomalies`, `/telemetry/latest`), `catalog_reads.py`
    (`/models/behavior[/live]`, `/bindings`, `/dids`, `/test-profiles`,
    `/obd/coverage`, `/can/coverage`, `/obd/dtc`, `/obd/monitors`),
    `analysis_reads.py` (`/analysis/binned`, `/analysis/llm-assist/cards`,
    `/sessions/{id}/trip-summary`; it installs the `StatusError` handler, the only
    one of the three that can raise a refusal, so a bare app mounting only it
    answers `analysis_deferred`). `public.py::register_public_routes` keeps its
    signature and calls the three in the old registration order.
    **Logic out of the closures**: `polled_did_names(did_map, obd_dids)` /
    `decoded_did_rows(rows, dids_path)` in `dashboard/readers.py`;
    `src/control/vin_refusal.py::vin_refusal(config, take_reading)`;
    `src/control/vehicle_reading.py::vehicle_reading(...)`;
    `src/control/profile_payloads.py` (`profiles_payload`, `edit_payload`,
    `active_pending_restart`, `profile_delete_refusal`, the two `APPLIES_*`
    texts); `src/control/tuning_mode.py::set_tuning_mode` (the arm + isolate +
    roll back transaction; its failure is a `TuningStorageSwitchError`
    (`StatusError`, 500) the app handler answers).
    Two departures from the item text, both forced: `vehicle_reading` cannot take
    `derive_vehicle_state_from_storage` (a dashboard function; `control` may not
    import `dashboard`), so it takes it as a required `storage_state` callable
    and `dashboard/readers.py::read_vehicle_reading` binds it once for both
    callers; and `vin_refusal` takes a `take_reading` callable rather than a
    `VehicleReading`, because the old closure read the vehicle only after its
    four cheaper rungs passed (a parquet read when there is no live source),
    and `test_the_vehicle_is_not_read_when_a_cheaper_rung_already_refused` pins
    that.
    **Bodies identical**: an AST diff against `HEAD` of every moved body, after
    dropping docstrings and substituting only the named closure-captured
    expressions for their parameters, is identical for all 26 pairs (17 routes
    incl. decorators and signatures, 2 DID helpers, `_vin_refusal`,
    `vehicle_reading`, `get_control_profiles`, `_edit_payload`, `_pending`,
    `_delete_refusal`, the tuning-mode body). Two non-parameter changes, both in
    the tuning-mode transaction. (1) `HTTPException(500, ...)` ->
    `TuningStorageSwitchError(500, ...)` (same `(status, body)`, pinned over
    HTTP). (2) The ERROR record "tuning-mode storage isolation failed; reverting
    poll profile" moved from logger `src.dashboard.routes.control` to
    `src.control.tuning_mode` (`log = logging.getLogger(__name__)`); level and
    `exc_info` are unchanged and no consumer in the repo reads the old name.
    **Responses unchanged**: 44 `(status, body)` pairs across every route R29.7
    touches, recorded on the tree before the move and after it, are equal
    (`/status` by key set; temp paths normalised). New pins:
    `tests/test_public_route_pins.py` (18), `test_tuning_mode_transaction.py`
    (12, incl. the rollback on a writer that fails to enter and to exit),
    `test_vin_refusal.py` (16), `test_vehicle_reading.py` (5),
    `test_profile_payloads.py` (10), `test_did_readers.py` (11).
    `tests/fixtures/route_table_snapshot.json` and
    `mcp_tool_snapshot.json` untouched.
    **Gate**: `pytest -n 4 -m ""` over the 86 test files that import
    `create_app`/`TestClient`/the touched routes, plus the route-table and MCP
    snapshots, `test_status_error_http`, `test_layering`, `test_source_rules`:
    1894 passed, 1 xfailed, 1 failed
    (`test_source_rules::test_the_allowlist_only_shrinks[tests-recompute-repo-root]`,
    on another session's uncommitted `tests/test_profiles_split.py:43`, not on
    this change); `ruff check .`, `ruff format --check .`, `mypy` clean.
    **Repointed patches**: `tests/test_status_error_http.py`
    (`public_routes.capture_deferral` -> `analysis_reads_routes`),
    `tests/test_profile_edit.py` (`system_routes.vehicle_reading` ->
    `read_vehicle_reading`), `tests/test_rom_backup.py` (six
    `vehicle_reading(...)` calls -> `read_vehicle_reading`). No re-export is left
    behind; `test_the_logic_r29_7_moved_is_not_defined_in_a_route_module_again`
    and `test_a_control_router_does_not_import_the_rom_library_routes` keep it so.
    **Other existing tests edited**: `tests/test_exceptions_pickle.py` (the new
    `StatusError` subclass `TuningStorageSwitchError`: a typed-args entry and a
    place in the expected `STATUS` set, so it is round-tripped like its siblings)
    and `tests/test_status_error_http.py` (besides the repoint above, `RAISERS`
    gains `set_tuning_mode` / `TuningStorageSwitchError`, so the
    handler-installed scan sees `routes/control/bus.py` as a raiser).
  - [ ] Operator question: `/control/tuning-mode` and `/control/capture` look
    like two lifecycles for one thing. Does the dashboard still use the
    former?
    Left open for the operator. Finding: no. `src/dashboard/static/src` has no
    reference to `/control/tuning-mode`, `tuningMode` or `tuning_mode`. The
    remaining callers are the MCP write tool `set_tuning_mode`
    (`src/mcp_server/write_tools/tuning_mode.py`, in `mcp_tool_snapshot.json`),
    `tests/test_control_passive.py` and the two route-snapshot rows, plus
    `docs/headless-api.md` and `docs/mcp-server.md`. The route is a shim over
    `tuning_profile.set_active` (its docstring: "Compatibility shim for the old
    boolean tuning-mode endpoint"); `/control/capture` is the lifecycle with
    session isolation (`enter_capture_mode`).

- [~] **R29.8 Four `async def` handlers do file work on the event loop, and
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
  - [x] `prepare_single_session_job(config, session_id, …)` in
    `workbench_jobs`, doing resolve, defer and budget off the loop.
    `await prepare_single_session_job(config, id, reads=…, profile_refusal=…)`
    runs resolve → `capture_deferral` → (the route's profile fact) →
    `select_sessions` → `409 analysis_refused over_budget` in one
    `asyncio.to_thread`; `get_session_frame`, `get_cell_residency` and
    `post_session_classification` call it instead of their three copies of the
    preamble (same bodies and statuses; `test_status_error_http.py` pins them,
    its patch points moved to `workbench_jobs`). Rule: plain file work in an
    `async def` goes through `asyncio.to_thread`; a whole-session read goes to
    the bounded child. `get_cell_residency` loads `load_viewer_definition` in
    `asyncio.to_thread` too. `post_replay_start` runs
    `derive_vehicle_state_from_storage` in `asyncio.to_thread`, and
    `ReplayController._start_locked`'s source reads (`is_dir`,
    `refuse_unfaithful_replay`, `read_capture_manifest`) moved into
    `_read_source`, one `to_thread` call keeping the order (not found, then
    unfaithful 422). Proof `tests/test_workbench_off_loop.py` (8): each of the
    four routes against a 0.4 s blocking read (the viewer-definition load, the
    budget read, the vehicle-state read and the replay source read) with a
    10 ms ticker on the same loop, stall < 0.2 s. With the `to_thread` removed
    the prepare tests (3) and the worker-thread test fail, and putting back the
    direct `load_viewer_definition` call and the direct `_read_source` call
    each fails its test (2 failures). Also the worker-thread-ident test, the
    profile-refusal ordering and the deferral.
  - [x] `run_off_loop` calls `capture_deferral` itself. It takes
    `sessions=` for the refusal's laptop path; the explicit calls in
    `workbench.run`, `post_session_classification` and `trip_summary_off_loop`
    are gone (`rom_editor` keeps its own first, for its own `laptop_path`).
    `test_run_off_loop_defers_while_a_capture_records_without_the_route_asking`.
  - [ ] `pooled()` in `routes/workbench.py` still calls `select_sessions`
    (opens every parquet footer of each session) and, in `get_dyno`,
    `dyno_preflight` (`count_session_rows`) on the event loop, inside the async
    handlers `get_delivery_error`, `get_trim_surface`, `get_timing`,
    `get_dyno` and `get_pull_comparison` (twice, once per revision). Move them
    into `asyncio.to_thread` (or a `prepare_pooled_job` beside
    `prepare_single_session_job`) with the same off-loop proof.

- [~] **R29.9 The dashboard polls the full `/status` every 5 s.**
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
  - [~] Time `GET /status` against `?view=compact` on the Pi and record both
    here.
    **Pi numbers not taken (no Pi reachable from the implementing session);
    the Pi timing is still owed.** Laptop numbers on a realistic state
    (2026-11-12), script `time_status.py` in the session scratchpad
    (`r299/`): the real corpus read only (`~/canary-data/sessions/nissan-xterra-2008`,
    264 directories), the repo's `models/nissan-xterra-2008` (2 manifests), the
    Xterra's tracked `tune_revisions.yaml` (10 kB, running `ZS44A-20261111-2`),
    the repo DID catalog (`data/dids.yaml`), `vehicle_state` from a stub live source as inside the
    daemon, `TestClient` over one persistent portal, median of 40 requests
    after a warm-up, HEAD (a throwaway worktree) and the working tree
    alternated. The laptop was loaded by other sessions (load average 11-15
    for the final rounds), so the absolute figures are inflated; read the
    ratios.

    | | HEAD (`5af2d779`) | working tree |
    |---|---|---|
    | `GET /status` (full), load ~5, before the capture-summary memo | 40.1 / 40.4 / 41.0 ms | 26.6 / 27.7 / 28.2 ms |
    | `GET /status` (full), load 11-15, all memos | 99.1 / 102.5 / 108.5 ms | 35.0 / 35.4 / 40.9 ms |
    | `GET /status?view=compact` | 1.9 ms (load ~5), 3.5-5.0 ms (loaded) | 1.8-1.9 ms (load ~5), 2.4-3.8 ms (loaded) |

    Per block, median over 30 calls (loaded laptop, HEAD then working tree):
    `latest_capture_summary` 50-61 -> 5-8 ms; model manifests (list, parse
    twice) 15-16 -> 0.15 ms; `calibration_status` (tune registry) 1.9-2.0 ->
    0.2-0.3 ms; `memory_verify_summary` 9.5-11.5 -> 0.07-0.1 ms;
    `build_status` without the source gather 105-107 -> 27-34 ms. **Not
    memoized, on re-measure: `latest_session`**, 8-17 ms on the loaded laptop
    (R23.2 measured 4.4 ms quiet), now the largest block of the full payload.
    `src/storage/sessions.py` stays as R23.2 left it, and a memo of "the newest
    session holding data" cannot be keyed on the root directory's mtime alone:
    the first chunk flush into the newest directory changes the answer without
    touching the root. The dashboard no longer calls it every 5 s (compact does
    not), so it is the full payload's cost to carry. Compact is the same
    before and after: the memos are in the full payload's blocks.
  - [x] The dashboard polls `view=compact` and fetches the full block only
    where a panel shows it.
    `useStatus` (`hooks/useStatus.ts`) polls `getStatusCompact`
    (`/status?view=compact`) every 5 s and the full `getStatus` every
    **30 s**, **5 s while a panel that shows a full-only moving field is
    mounted** (`useLiveFullStatus()` in `hooks/statusDemand.ts`: `SystemPanel`
    for GPS, IMU, power and detector blind modes; `DataSourcesPanel`'s GPS tab;
    `CaptureRunner` for the scheduler request table and `rows_written`;
    `ModelHealthPanel` for latency and drift), and **at once** when
    `compactChangeKey` moves (vehicle state, replay id, capture active or name,
    passive switch or ECM session owner: a replay or capture starting is seen
    within one compact poll, as before). The panels still read one
    `StatusResponse`: `lib/statusView.overlayCompact` lays the compact fields
    over the last full payload, a `null` in compact never erasing a block the
    full payload has. `status` is `null` until the first full payload lands
    (StatusStrip reads `health.inference_latency_ms.mean`, which compact
    lacks), so first paint waits as long as before. No API shape changed;
    `getStatus` is unchanged and `getStatusCompact` is new.
    **Fields and where they now come from.** Compact, fresh every 5 s:
    `vehicle_state` (DashboardShell badge, LoggingPage, CalibrationPage,
    romLibrary, romTrace), `free_mb`, `emitter_counters` (StatusStrip,
    ModelPanel), `passive` (romLibrary), `capture_profile.name` and `.active`
    (shell banner, TuningContextBar, CalibrationPage, useLiveOverlay, capture
    pages), `health.system` (SystemPanel, StatusStrip CPU), `health.can0`,
    `health.telemetry_sinks`, the `obd_scheduler` counters,
    `detectors.inference`. Full, every 30 s and on a change: `latest_session`
    (TripPanel), `storage_path`, `storage_warning_mb`, `vehicle`, `profile`
    (TuningContextBar, LoggingPage), `model_versions` and `sequence_detector`
    (ModelPanel, StatusStrip), `accepted_baseline_overrides`, `replay`
    (ReplayBanner, StatusStrip), `last_capture`, `capture_profile` beyond
    name/active (shell banner, DataSourcesPanel plan, useLiveOverlay),
    `registry_revision`, `ecu_calibration_id`, `calibration_matches_registry`,
    `ecu_calibration_changed` (AnalysisPage, TuningContextBar, LiveOverlay,
    romTrace), `tuning_readiness` (TuningContextBar), `network`,
    `health.inference_latency_ms`, `missing_feature_rate` (StatusStrip),
    `health.obd_scheduler.requests` (romTrace's "not answering" message).
    Full, every 5 s only while the panel is open: `gps` (SystemPanel,
    DataSourcesPanel GPS tab), `imu`, `health.power`,
    `detectors.isolation_forest` (SystemPanel), the scheduler table,
    `achieved_hz_capture` and `capture_profile.rows_written` (CaptureRunner),
    `health` latency and drift (ModelHealthPanel),
    `health.obd_scheduler.achieved_hz_60s` (TelemetryPanel's freshness and
    "slow" chips, mounted by DriveScreen, so the Drive screen is covered), and
    `calibration_matches_registry` / `registry_revision` / `requests` for the
    ROM trace gate (`useLiveOverlay`, mounted by `LiveOverlay`: a reflash must
    not wait 30 s to be seen, because the gate calls nothing active while the
    daemon contradicts the ROM). **Lags up to 30 s by design** (the change key
    does not move for these): StatusStrip's `inference_latency_ms.mean`,
    `missing_feature_rate` and `model_versions`; TuningContextBar's readiness
    chips (`ups_soc`, `storage_free_mb`, `memory_channels`, `calibration`) and
    `RunningCalibration` (key-on moves `vehicle_state`, which fetches the full
    payload at once, but the ECU calibration id is read a few seconds after
    that, so its chip can lag up to 30 s); TripPanel's `latest_session`;
    AnalysisPage's `registry_revision`; NetworkPanel. Pi cost of the
    dashboard's `/status` polling: 12 full polls a minute become 2 (the shell
    alone) plus 12 compact; a live panel open puts it back to 12 full for as
    long as it is open. **Per-panel cadence (operator decision, 2026-11-12,
    option b):** `useLiveFullStatus(intervalMs = 5000)` takes an optional
    cadence and the shortest one asked for by any mounted panel wins, never
    slower than the 30 s idle cadence. `TelemetryPanel`, which DriveScreen
    renders and which reads only `achieved_hz_60s` (a 60 s trailing mean) and
    the request table, asks for **15 s**, so a drive costs 4 full polls a
    minute rather than 12. Every other live panel (`SystemPanel`,
    `DataSourcesPanel` GPS tab, `CaptureRunner`, `ModelHealthPanel`) and
    `useLiveOverlay` (the ROM trace gate) keep **5 s**, and one of them
    mounted beside TelemetryPanel brings the poll back to 5 s. A cadence is
    never under 1 s (`MIN_LIVE_INTERVAL_MS`: `useLiveFullStatus(0)` would be a
    back-to-back loop), and a value that is not a number is the 5 s default.
    **A cadence change never restarts the poll** (verifier N2; the first
    version restarted `usePolling`'s effect on every `intervalMs` change,
    which aborted the in-flight full request and issued another: 41 full calls
    and 40 aborts for 20 page flips at 300 ms, and an aborted `/status` still
    computes for 1.3-2.6 s on the Pi, so they queue). `usePolling` now keeps
    the request in flight and re-arms only its timer, `delay` after the last
    fetch *settled*: a slower cadence (an unmount) fetches nothing, a faster
    one (a mount) fetches at once only when the last payload is already older
    than the new interval and otherwise waits out the remainder, and a first
    load on a live page makes one full request. Only a new `fetcher`,
    `enabled`, `refresh` (the change-key fetch) or `intervalMs` going to or
    from `null` restarts it. Every other `usePolling` caller shares this
    (`UpdateCard`'s idle/running cadence, `useControlResource`): a cadence
    change there no longer fetches at once either.
    **Behaviour a caller will notice**: `usePolling` gained an optional
    `retryMs`: a failed poll is retried after it, and `useStatus` passes the
    compact cadence, so a failed first full fetch (and the error it raised)
    is retried within 5 s rather than 30 s. `overlayCompact` semantics: a
    compact `null` replaces a scalar leaf (`rpm`, `coolant_temp`,
    `cpu_percent`, the passive disarm fields: a stale value is never shown
    in place of "unknown") but never erases a record, array or
    absent-from-compact block; a block compact carries **whole** replaces the
    full payload's copy instead of merging with it (verifier N1:
    `passive.ecm_session_claims {foreign: 5}` over a compact `{}` stayed):
    `vehicle_state`, `emitter_counters`, `passive`, `health.system`,
    `health.can0`, and the maps inside the narrowed blocks
    (`obd_scheduler.profile_start_withheld`, `.receive`,
    `detectors.inference.missed_by_reason`); the blocks it *narrows*
    (`health`, `health.obd_scheduler`, `detectors`, `detectors.inference`,
    `capture_profile`, and each telemetry sink's three fields) merge key by
    key, and the sink set itself is replaced (a sink compact no longer lists is
    gone). The two lists, `NARROWED` and `KEYED` in `lib/statusView.ts`, are
    the paths `compact_status` builds (`src/dashboard/status.py`); a block
    compact grows later is whole by default. And when the compact `capture_profile` name or
    `active` differs from the full payload's, the full payload's plan is
    dropped until the next full fetch lands (a profile switch or a capture
    ending no longer shows the previous plan, `started_at` and
    `rows_written`; CaptureRunner's plan summary may read "0 Mode 01 channels"
    for one full-fetch latency after a start).
    Tests: `src/hooks/useStatus.test.ts` (20, rewritten: compact cadence, full
    null until it lands, 30 s / 5 s cadence with a mounted panel and back
    (nothing fetched on a mount just after a fetch or on an unmount), a mount
    beside a payload older than its cadence fetching at once, TelemetryPanel
    alone at 15 s with a 5 s panel beside it dropping the poll to 5 s and its
    leaving restoring 15 s then 30 s, a panel asking 120 s held to the 30 s
    idle cadence, `useLiveFullStatus(0)` and `(NaN)` floored at 1 s, a first
    load on a live page making exactly one full request, twenty mounts and
    unmounts over an in-flight full request and after it landing with no abort
    and never two in flight, an immediate full fetch on a state change, compact
    over full, either poll's error, a failed first full fetch retried within
    one compact interval and kept retrying, no overlap, stop on unmount),
    `src/lib/statusView.test.ts` (20: the overlay, nulls over scalar leaves,
    whole blocks replacing and narrowed ones merging, a dropped telemetry sink,
    capture-profile drop on a switch or an end and kept when unchanged, and
    `compactChangeKey`). Mutations caught: the change-key refresh removed, the
    live cadence removed, `retryMs` removed (2 failed), the null and
    capture-profile rules reverted (4 failed), TelemetryPanel's 15 s request
    dropped (1 failed), shortest-wins turned into longest-wins (1 failed),
    `intervalMs` put back in the poll effect's dependencies (5 failed: the
    cadence, Telemetry, first-load and both flap tests), the 1 s floor removed
    (1 failed), every block merged again (1 failed), the sink set merged again
    (2 failed). `pnpm typecheck`, `pnpm format:check`,
    `pnpm lint` clean (no suppression changes); final run after N1-N3:
    `vitest run` 131 files / 1432 tests passed, `tsc` and `eslint
    --max-warnings 0` on hooks, lib, panels, components, api and types clean,
    `prettier --check src e2e` clean. `pnpm e2e` was last run before the
    `usePolling` re-arm change (46 passed, 3 skipped, the existing skips,
    including `emptyDaemon.spec.ts`); **not re-run since**.
  - [x] Memoize the near-static blocks on file identity
    (`(path, mtime_ns, size)`, as `dids._parsed_yaml` does). R24.3 is the
    shared helper.
    New `src/file_memo.py` (`FileStamp`, `file_stamp`, `StampedCache`, layer 0
    in `tests/test_layering.py`): `yaml_load.load_yaml_model`'s key extended
    to a set of files, with the same racy-window rule (a value computed within
    `RACY_WINDOW_NS` of its newest file's mtime is recomputed until it
    settles), so a same-size rewrite in one clock tick is never served stale.
    Model manifests: `status.model_manifest_summary` lists with `rglob` on
    every call and parses only when a manifest was added, removed or
    rewritten (it used to parse each twice), returning copies. Tune registry:
    `load_tune_revisions` now goes through `load_yaml_model` (R24.3's own
    cache), so `active_registry` / `calibration_status` parse once per edit; a
    bad file raises `YamlModelError`, still a `ValueError`. DID catalog:
    `calibration.memory_verify_summary` is a `StampedCache` over the catalog
    and the verify record (the catalog's parse was already cached by
    `load_did_map`; the count that asks every channel against every relation
    was not). Beyond the three named: `latest_capture_summary` now stats each
    directory's `capture_profile.json` (and the newest capture's quality
    sidecar) and reads manifests only on a change (50-61 -> 5-8 ms).
    `tests/test_status_memo.py` (26): parse-once for each block;
    **an uploaded manifest shows on the next call, as does a replaced
    one, a removed one, a same-size replacement inside the racy window, an
    edited flash registry, a verify record just written, a capture stopping, a
    quality verdict written after the stop**; callers cannot corrupt the
    cache; and the same through `GET /status` between two polls. Mutation
    caught: the racy-window rule removed (2 failed).
  - [x] A host sampler that owns the CPU baseline and samples on its own
    clock.
    `HostCpuSampler` (`daemon/health.py`; R29.10 moves it to `host_metrics.py`):
    a daemon thread reads `/proc/stat` every 1 s into a ring, and
    `cpu_percent()` is the busy fraction over the trailing 5 s, computed by
    *reading* the ring, never sampling, so no poller moves it. `None` for
    fewer than two samples, no `/proc/stat`, or a newest sample older than
    three intervals (a stopped sampler reads as unknown, not as its last
    value). `HealthMonitor._cpu_percent` delegates; `run_daemon` starts the
    sampler at boot (`start_host_sampler`) and the first `snapshot()` starts it
    for any other monitor; the thread holds a weak reference and ends when
    the monitor is collected; a sampler handed to the constructor is not
    started behind the caller's back. `start()` takes the sampler's lock for
    the whole decision (is a live thread there, new stop event, baseline,
    spawn), so concurrent `/status` requests cannot start more than one
    thread. `tests/test_host_sampler.py` (17):
    two back-to-back `snapshot()` calls and 50 polls return the same value and
    take no sample; the value is the busy fraction of the sampled interval
    (25 % then 100 % reads 62.5 %), forgets load older than the window, moves
    only when the sampler samples; the thread samples until stopped, one
    thread, none left after the monitor is dropped (asserted on the sampler's
    own thread, not on a global thread count another test's monitor could
    move), sixteen concurrent `start()` calls make exactly one thread (mutation:
    the lock removed gave 16 threads, test failed), and a
    `start()` after `stop()` starts a fresh one even before the old thread
    noticed. `tests/test_health.py::test_cpu_percent_needs_a_baseline_then_reports_a_fraction`
    was vacuous (it always read `None`); it now scripts the `/proc/stat`
    reader and asserts `None` with no sample, `None` with one, and 0.7 for 30
    idle of 100 after two, with a separate smoke test on the real
    `/proc/stat`. The `/status` shape is unchanged (`health.system.cpu_percent`).

- [x] **R29.10 `HealthMonitor` is five jobs in one class.** 534 lines, 31
  methods: inference latency accounting; detector-state mailboxes that stash a
  value for `snapshot` to echo (seven `observe_*` / `note_*` setters); sink,
  queue and loss-epoch accounting; OBD scheduler passthrough; stateful host
  metrics. The host readers (`system_metrics`, `power_status`,
  `can0_counters`, a `vcgencmd` shell-out) are another 170 module-level lines
  in the same file.
  - [x] `daemon/host_metrics.py` (readers and the sampler from R29.9) and
    `daemon/sink_health.py` (with `CaptureQualityInputs`, `LossEpoch`).
    `HealthMonitor` stays as the facade, so `/status` keys and every
    `health_monitor.observe_*` call site are unchanged.
    `host_metrics.py` (434 lines) holds `system_metrics`, `power_status`,
    `can0_counters`, the `vcgencmd` shell-out and `HostCpuSampler` as they
    were, plus `HostMetrics`, which owns what the monitor used to carry as
    fields: the sampler, the 5 s `vcgencmd` cache and the first can0 counters.
    `sink_health.py` (266 lines) holds `SinkHealth` (sink state, queue
    high-water, `wait_for_queues_idle`, `loss_counters`, `loss_epoch`) and
    `CaptureQualityInputs`; `LossEpoch` stays defined in
    `storage/capture_losses` (it is the storage layer's scope table) and is
    re-exported. `SinkHealth` reads no clock and no sysfs: the facade hands it
    the instant and the can0 counters. `health.py` is 581 lines (971), and
    `HealthMonitor` keeps every public sink and loss method
    (`observe_telemetry_*`, `note_telemetry_*`, `observe_receive_path`,
    `observe_collector_handoff`, `loss_epoch`, `loss_counters`,
    `capture_quality_inputs`, `wait_for_queues_idle`) as a one-line forward, so
    no call site of those moved. Importers' names stay importable from
    `src.daemon.health` (`test_every_name_importers_took_from_the_facade_is_still_there`,
    11 names) **except `system_metrics`, `power_status` and `can0_counters`**:
    their one importer was `tests/test_health.py`, which now takes them from
    `host_metrics`, and the re-export is removed so a patch on `health` cannot
    apply to nothing. `memory_limit_status`, `CGROUP_ROOT`, `HostCpuSampler`,
    `FeatureDriftReport` and `DEFAULT_CAN0_STATS_PATH` are still re-exported
    (importers exist) but nothing in `health` looks them up, so
    `test_nothing_patches_a_name_the_facade_only_re_exports` scans `src`,
    `scripts`, `tools` and `tests` for a `setattr(health…, "<name>")` or a
    `"src.daemon.health.<name>"` target (and
    `test_the_tripwire_for_a_patch_through_the_facade_can_fail` proves the
    pattern matches).
    **`/status` `health` is unchanged, key for key and in key order:**
    `test_a_driven_monitor_snapshots_exactly_as_it_did_before_the_split`
    drives one monitor through inference ticks, a blind-mode latch, swaps,
    all seven blocks (set, replaced, cleared), every sink method, two CPU
    samples, can0 growth and a capture in flight, and compares four
    `snapshot()`s plus the loss counters, a loss epoch and
    `capture_quality_inputs` with `fixtures/health_snapshot_golden.json`. The
    golden was generated by the same `drive()` against `HEAD` (e69140e8,
    before the split) through the old `observe_*` setters, in a throwaway
    worktree; the test also compares the serialized JSON, so a moved key fails.
    Mutations caught: `obd_scheduler` moved ahead of `system` (key order),
    `dropped_records` without `queue_dropped`, can0 `since_start` without the
    baseline (the last one only after the fixture's counters started nonzero).
    Monkeypatch targets that moved with their code: `health_mod._read_throttled_raw`
    (4 in `test_health.py`) and `health_mod._run_sampler` (`test_host_sampler.py`)
    now patch `host_metrics`; `test_capture_profiles.py` patched
    `health_mod.time` and now patches `sink_health.time` too (the drain
    deadline). The readers are deliberately *not* on `health`, so a stale
    `monkeypatch.setattr(health_mod, "_read_throttled_raw", ...)` raises
    `AttributeError` instead of applying to nothing
    (`test_a_host_reader_is_not_re_exported_where_a_patch_would_miss_it`, 7
    names). `HealthMonitor.host_cpu` is a read-only property (it was a plain
    attribute; assigning a fake after construction used to replace what
    `_cpu_percent` read and would now do nothing), pinned by
    `test_the_host_sampler_is_handed_to_the_constructor_not_assigned_afterwards`.
  - [x] `set_detector_block(name, value)` in place of the seven mailboxes.
    One `HealthMonitor.set_detector_block(name, value, *, key=None)` over a
    closed registry (`DETECTOR_BLOCKS`; `DetectorBlock` is the `Literal` of its
    names, and a test pins the two equal). An unknown name raises
    `ValueError` instead of a block that is stored and never shown, `None`
    clears a block, values are copied in and out. The seven are
    `model_version_mismatch`, `sequence_suppression`,
    `sequence_cadence_refusal`, `detector_roles`, `obd_scheduler`,
    `capture_profile_gate` and `segmenter_fallback`. **All seven setters are
    gone and every call site was updated** (src: `components`, `polling`,
    `runtime`, `inference` x2, `detectors/sequence` x2, `validation/harness`;
    the existing tests in `test_health`, `test_profile_capture`,
    `test_capture_profiles`, `test_daemon` and `test_role_gate`), with no
    wrapper left:
    `grep -rnE "observe_(segmenter_fallback|capture_profile_gate|model_version_mismatch|sequence_suppression|sequence_cadence_refusal|detector_roles|obd_scheduler)"`
    finds nothing outside archived roadmaps. Two of the seven were not plain
    mailboxes, and the registry says where their logic went.
    `model_version_mismatch` is per model manager (`key=`) and takes a frozen
    `VersionPair(serving, manifest)`, not a mapping: the monitor decides that a
    pair that agrees or has an unknown side clears the entry, and with a
    `.get` on a dict a misspelt key read as "unknown" and switched the
    partial-deploy detector off without a word (found by the verifier). A
    dict, or a wrong field name, is now a `TypeError` / mypy error
    (`test_the_mismatch_block_takes_a_version_pair_not_a_mapping`).
    `test_daemon.py::test_poll_reports_a_manager_whose_binary_disagrees_with_its_manifest`
    runs `poll_model_managers(..., health=HealthMonitor())` over two stub
    managers: the stale one appears under its own name in
    `snapshot()["model_version_mismatches"]` and clears once the binary lands.
    Mutations caught: `key="x"`, and `serving` read from the manifest. `segmenter_fallback` used to return its
    rising edge; `set_detector_block` returns `None` (a `bool` return breaks
    the `Callable[[X], None]` the scheduler and role-gate callbacks are typed
    as), so `LiveInferencePipeline` reads `segmenter_fallback_active` before
    setting it, and `test_run_live_inference_warns_once_when_the_segmenter_falls_back`
    asserts one warning over four held-fallback ticks (the old unit test
    asserted the monitor's return and never the log).
    Behavior changes beyond the checklist: `set_detector_block(..., None)`
    clears a block, where `observe_obd_scheduler(None)` raised `TypeError`;
    `sequence._publish_cadence_refusal` now needs `set_detector_block` on the
    monitor (it used to tolerate a monitor without `observe_sequence_cadence_refusal`
    through `getattr`; only `None` is tolerated now); an unknown block name or a
    misplaced `key` raises `ValueError`; `HealthMonitor.host_cpu` is read-only;
    and the private attributes the host and sink state moved out of
    (`_power_cache`, `_can0_baseline`, `_telemetry_sinks`, `_queues`, …) are
    gone from the monitor.

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

- [x] **R29.13 `RomBackupJob` is a 920-line class.** Lifecycle (~150 lines),
  preconditions (90), the wire protocol (~275: `_listen`, `_send`,
  `_session_step`, `_read_block`, `_reread`, …) and post-processing that needs
  no bus (~150: `_store`, `_integrity`, `_size_caveat`). A
  `counters: dict[str, Any]` is threaded through six methods and mutated by
  key. The recipe schema is another 140 lines above it.
  - [x] `rom_backup_recipe.py`, `rom_backup_wire.py` (a reader with a counters
    dataclass), `rom_backup_store.py`. `RomBackupJob` and `BackupRefusedError`
    stay importable from `src.control.rom_backup`.
    `rom_backup_recipe.py` holds the schema, `load_backup_recipe` and
    `recipe_extent`. `rom_backup_wire.py` holds `MemoryReader`, one per read:
    the frame tap, listen, session enter/exit, block read with retries, the
    re-read and the early extent stop. It owns a `ReadCounters` dataclass in
    place of the `counters` dict. `rom_backup_store.py` holds `store_image`,
    `integrity` and `size_caveat`. `rom_backup.py` is 695 lines, down from
    1,284: the job's lifecycle, preconditions, mid-read re-check and
    polling-resume wait. Every name importers use still imports from
    `src.control.rom_backup`, and `job._store` / `job._integrity` are thin
    delegates the tests patch and call.
  - This is a read path (service 0x23), not a non-volatile write. Behavior
    identical; pinned by the scripted-ECU tests.
    The existing tests in `tests/test_rom_backup.py` are unchanged and pass
    (75 with the new one). The new
    `test_the_split_job_sends_and_reports_exactly_what_the_single_class_did`
    compares four scripted reads (clean, timeout+retry, NRC abort, send
    error) with `tests/fixtures/rom_backup_wire_golden.json`, which was
    recorded from a HEAD worktree (`47662349`, identical over 3 runs). It
    checks every frame sent, in order (125 / 127 / 17 / 127), every
    `query_raw` call, each client's ids and `timeout_seconds`, the bus
    filters, and the job state, events and `read_meta`, with key order.
    The only services requested are 0x10 and 0x23. A grep of the moved code
    for 0x2E/0x31/0x34/0x36/0x3B/0x3E/0x27 finds only an NRC `"0x31"` in a
    comment.
  - [x] `brain_jobs.py` (1,168 lines) splits the same way: store and index, kind
    runners and `precheck`, scheduler.
    `brain_jobs_store.py` (295 lines) holds `BrainJob`, `BrainJobStore`,
    `BrainJobRefusedError`, `job_id_for`, the kind and state vocabularies, and
    the only `_LOCKS` / `_INDEXES` / `_JobIndex`. `brain_jobs_runners.py` (468)
    holds `canonical_params`, the candidate helpers, `enqueue_after_digest`,
    the `_run_*` kind runners, `run_brain_job` and `precheck`.
    `brain_jobs_scheduler.py` (410) holds `collection_activity`,
    `MIN_TICK_SECONDS` and `BrainScheduler`. `brain_jobs.py` is a 94-line
    facade re-exporting every name an importer in `src/`, `scripts/`, `tools/`
    or `tests/` uses (found by an AST scan of imports and attribute reads),
    plus the daemon's `importlib` use of `BrainScheduler` and
    `collection_activity`. Behavior identical: `ast.dump` of all 40 moved
    top-level definitions (5 scheduler, 19 store, 16 runners) equals
    `git show HEAD:src/control/brain_jobs.py`, 40 of 40, none defined twice,
    none new. Persisted state is dicts only (`jobs.jsonl` lines, result
    files), so old files load unchanged; `run_brain_job` pickles by its new
    module name and `test_the_real_spawn_child_runs_a_job` runs it in a real
    `spawn` child.
    Patches that would silently stop applying were repointed, not delegated,
    because the scheduler reads `any_digest_in_flight`,
    `any_workbench_job_in_flight` and `precheck` from its own module:
    `tests/test_brain_jobs.py` (three `monkeypatch.setattr` targets, and the
    `_INDEXES` read, now `brain_jobs_scheduler` / `brain_jobs_store`);
    `tests/test_child_pool.py` `_MAY_SPAWN` names
    `src/control/brain_jobs_scheduler.py` (where `spawn_pool(` now is);
    `tests/test_exceptions_pickle.py` keys `BrainJobRefusedError` by its new
    `__module__`. One behavior change: the scheduler's two
    `log.exception` records now come from logger
    `src.control.brain_jobs_scheduler` (was `src.control.brain_jobs`); nothing
    filters on the old name. Verified independently (40/40 AST, 491 passed).
    Gate: ruff and mypy clean on the new files (the whole-repo
    `ruff format --check` and `mypy` were red only on another session's files),
    491 passed over every test file mentioning `brain_jobs` plus
    `test_layering`, `test_source_rules`, `test_exceptions_pickle`,
    `test_process_state_reset`, `test_route_table_snapshot` and
    `test_mcp_tool_snapshot` (`-m "" -n 4`).

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

- [x] **R29.15 Dead code.** (2026-11-13)
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
  - [x] `companion/services.py`: deleted `companion_config`,
    `write_companion_config`, `_library_session_path`, `_dataset_row_to_dict`,
    `_require_optional_bearer`, `_unlink_path`, `feedback_list_response`,
    `submit_feedback_response`, plus `_library_session`, whose only caller was
    `_library_session_path`, and the shared-handlers comment block above the
    two feedback helpers. 421 -> 282 lines; the `yaml`, `secrets`,
    `HTTPException`, `FeedbackStore` imports went with them. `__all__` now
    lists the nine live names. Re-verified by word-grep over `src`, `scripts`,
    `tools`, `tests`, `docs` (excluding roadmap docs): 0 hits for each deleted
    name (only an archived R03 doc names `_require_optional_bearer`). Left
    alone, not on the list: `_make_job_controller` (only a test calls it).
  - [x] `control/trip_end.handle_trip_end` (and the `get_or_compute_digest`
    and `SESSION_TRIP_KEY` imports it alone used) and
    `control/twin_identity.segment_index` (and `import bisect`): deleted, 0
    hits in `src`, `scripts`, `tools`, `tests`.
  - [x] `dashboard/schemas.TripSummaryResponse`: deleted, with
    `TripSummaryFields`, whose only user it was. Adopting it as a
    `response_model` was refused: the route returns the cached sidecar dict,
    so FastAPI would re-validate it (`extra="forbid"`) and re-order its keys,
    and a sidecar written before `speed_basis`/`fuel_basis` became required
    would answer 500. The TypeScript comment in `types/tripSummary.ts` now
    says it mirrors `TripSummary.to_dict()` under the sidecar envelope.
    `tests/fixtures/route_table_snapshot.json` untouched.
  - [x] Test-only symbols, decided one by one:
    - `trip_end.digest_interrupted`: deleted; its one test line
      (`test_trip_end_digest.py`) asserts `session in trip_end._INTERRUPTED`
      instead, and the same test still pins the behavior (no second child,
      no digest).
    - `drain.reset_drop_bursts`: **kept**. `test_drop_logging_is_coalesced_into_bursts`
      needs a reset for module-level `_drop_bursts` state, which sink names
      share across cases; the docstring already says so.
    - `schemas.DtcReportResponse` (and `DtcCodeEntry`): **kept** as a contract
      model. `test_get_obd_dtc_returns_the_cached_report_shape` validates the
      live `/obd/dtc` body against it (`extra="forbid"`) and the TypeScript
      `DtcReportResponse` mirrors it; its docstring now says it is not the
      route's `response_model`.
  - Gate: `ruff check .`, `ruff format --check .`, bare `mypy` (1284 files) green;
    pytest over the touched modules' tests (`-m "" -n 4`, 789 tests) green
    after the golden update below.

- [~] **R29.16 Small items.** (2026-11-13; one bullet left open)
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
  - [x] Profile slug: `CanaryConfig.profile_slug` (`src/config/root.py`) is the
    scoped slug, else the storage path's final component. `dashboard/status.py`,
    `routes/control/system.py` and `vehicle/profiles/legacy.py` (the vehicle
    one moved there in the R26.4 split) read it; `grep storage_path.name src`
    finds none. (`validation/report.py` reads `storage_path.parent.name`, a
    different question, left.) The property is not state, so the scoring
    child's pickle is unchanged: `test_profiles_split.py` and
    `test_exceptions_pickle.py` green. `test_config_golden.py` lists a
    model's methods, so `tests/fixtures/config_golden.json` gained the one
    line `"profile_slug"` (hand-edited: `--write` also churns the unrelated
    imported-module list). One divergence from the old expression, verified:
    a scoped config whose `storage_path` is replaced after scoping
    (`validation/harness.py`, `scratch/"storage"`) now reports its real slug
    where `storage_path.name` gave `"storage"`. No test pins the property
    directly; coverage is through the status, system-route and legacy
    adoption tests.
  - [x] Upload cap: `MAX_ROM_UPLOAD` lives only in `control/rom_library.py`;
    `routes/rom_maps.read_rom_upload(request, wrong_type=, too_large=)` is the
    one octet-stream-with-a-cap block, used by `/control/rom/inspect` and
    `/control/rom/library`. The 415 and 413 messages stay each route's own, so
    responses are unchanged (`test_rom_maps_api.py`, `test_rom_library_api.py`
    green). `test_layering.py` green.
  - [x] `JobController._emit`: the `except Exception: pass` is now a
    `log.warning(..., exc_info=True)` naming the job id and status
    (`src/control/jobs.py` gained a module `log`); a raising publisher still
    never fails the job. New test
    `test_a_raising_job_event_callback_is_logged_and_never_fails_the_job`
    (`tests/test_control_api.py`).
  - [x] `_download_session_export` streams: it now returns the verified zip's
    `Path` (no `read_bytes`), `import_session_archive` takes `bytes | Path`
    and opens a path in place (no `BytesIO`), and the `/companion/sync`
    route imports from the file and deletes it in a `finally`. Same imported
    session either way: `test_import_session_archive_from_a_path_equals_importing_its_bytes`;
    the sync test asserts the export file is gone afterwards and the
    checksum tests assert a mismatched download is removed and a verified
    one on disk skips the network. `/companion/sessions/import` (base64
    body) still imports from bytes.
  - [~] `CaptureProfileStore` / `CaptureAutostart` double construction: not
    changed. The daemon passes both (`daemon/server.py` hands `profile_capture`
    and `capture_autostart` to `create_app`); the second construction is the
    fallback in `dashboard/api.py` (store) and `routes/control/__init__.py`
    (session and autostart) that 55 test files reach by calling `create_app`
    without them. Making the tests pass their own is a change to every one of
    those call sites. A cheaper route if wanted: one `CaptureProfileStore`
    factory over `config` for `runtime.py`, `collectors.py`, `harness.py` and
    the fallback.
  - [x] `docs/mcp-server.md`: the eleven table cells over 900 characters
    (`get_status`, `get_binned_analysis`, `get_trip_summary`,
    `get_session_digest`, `get_behavior_deviations`, `get_delivery_error`,
    `compare_tuning_sessions`, `preview_rom_edit`, `simulate_rom_edit`,
    `arm_tuning_capture` / `disarm_tuning_capture`, `obd_request`) now carry a
    one-sentence cell and a link to a `### ` heading under a new `## Tool
    notes` section holding the full text verbatim (`\|` unescaped). Longest
    table row is now under 900 characters (was 6,496). No test reads the
    file; `test_mcp_tool_snapshot.py` pins `server.py`, green.

- [ ] **R29.17 The legacy `/control/tuning-mode` shim restores the wrong
  profile.** Pre-existing. Found while moving it to
  `src/control/tuning_mode.set_tuning_mode` in R29.7 (the move kept the
  behavior).
  - **Disarm.** `tuning_profile.set_active(False)` installs a synthesized
    `default` (the 18 `DEFAULT_TUNING_CHANNELS`, no DIDs), not the catalog
    `default` that `collectors.py` passes to `configure_default` and that
    `ProfileCaptureController.stop` restores (`control/profile_capture.py`,
    `profile_store.load().by_name("default")`). After a legacy disarm the
    daemon therefore polls the shim's default until restart. On the truck,
    unmeasured: how far the catalog `default` differs from the synthesized one.
  - **Rollback.** `set_active` returns `is_active()`, which is `True` for
    *any* non-default profile. Two failures follow:
    - Arming over a running `/control/captures` capture replaces it with the
      `fuel-map` shim profile, and storage does not move because
      `previous == enabled`.
    - When the writer fails, the rollback calls `set_active(previous)`
      without the channel set or rate. A failed disarm therefore re-arms the
      18-channel default set at no fixed rate, not the operator's set. A
      failed arm during a real capture "restores" a synthesized `fuel-map`
      profile.
  - **Fix shape.**
    - Snapshot `active_profile()` and the session dir before the change.
    - Restore that exact profile on failure, and pass the catalog `default`
      on disarm.
    - `activate()` bumps the capture generation and restarts
      `capture_lifetime`, so a faithful restore needs an `activate(...,
      resume=True)` path, or a refusal to arm over a non-shim capture
      (`409`, naming it) instead.
  - Moot if the operator retires the route (R29.7's open question: only the
    MCP tool `set_tuning_mode`, tests and docs use it; the dashboard does not).

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
