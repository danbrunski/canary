# Roadmap — MCP tooling & UPS/power hardening

Status: **implemented and deployed** (2026-07-18/19). All 4 phases landed,
pushed, and live-verified on the bench Pi. Straight to `main`, no feature
branches.

## Why this exists

A long in-vehicle debugging session (dashboard down → daemon crash-loop →
profile-migration gap → X1202 UPS false-shutdown saga → mod-effect drive
analysis) surfaced a consistent set of gaps: MCP sessions dropped and forced
raw SSH/HTTP fallbacks, `get_behavior_live` overflowed the token limit, the
whole UPS/power subsystem had no programmatic surface, and the project's core
question — *"did my change move behavior?"* — had to be answered by hand with
DuckDB over SSH.

Landed already in that session (context for this roadmap):

- `773e728` — fixed `LogRecord.name` crash-loop.
- `8987673` — UPS watchdog discharge-gate (bench-safe).
- `bdfb22f` — UPS watchdog PLD polarity/pull fixed to match vendor `pld.py`.
- `11c800b` — UPS watchdog 5-min grace + keep-alive *read* side.

This roadmap is the follow-through. Four phases, sequenced by freshness of
context and ROI.

## Guiding constraints

- Whole-repo gate before every commit: `ruff check . && ruff format --check . && mypy src && pytest` (+ `pnpm test` in `src/dashboard/static` for FE changes).
- Not safety-critical, read-only on CAN — size effort accordingly; don't
  over-engineer failure modes the system can't have.
- Pi deploy: `git pull` + `sudo systemctl restart …`; unit/tmpfiles changes go
  through `scripts/install_pi.sh`. Model binaries are gitignored (scp, not pull).

---

## Phase 1 — Power / UPS subsystem

**Goal.** Finish the keep-alive toggle end-to-end (write side) and expose a real
power view (SoC, VCELL, PLD level, watchdog state) so the UPS is inspectable
without SSH. Completes the feature the operator chose (5-min grace + dashboard
keep-alive).

### 1A. Shared runtime plumbing

- [x] **1.1 — `/run/canary` runtime dir.** Add `systemd/tmpfiles-canary.conf`
  with `d /run/canary 0775 canary canary -` so both the `canary` daemon (writes
  the keep-alive file) and the root `ups-shutdown` watchdog (writes a status
  file, reads keep-alive) share it. Install + `systemd-tmpfiles --create` in
  `scripts/install_pi.sh` (near the ups-shutdown unit install, ~line 92).
- [x] **1.2 — Watchdog status heartbeat.** In `scripts/ups_shutdown_watch.py`,
  write JSON `{armed,input_lost,on_battery,soc,vcell,seconds_lost,grace,
  keepalive_remaining,ts}` to `UPS_STATUS_FILE` (default
  `/run/canary/ups_status`) each poll (atomic temp+rename). Add
  `UPS_STATUS_FILE` to the env docstring + unit. Pure-format the record via a
  small tested helper. This lets the daemon report watchdog state without
  duplicating GPIO logic.

### 1B. Daemon-side power reader + endpoints

- [x] **1.3 — Power reader module.** New `src/dashboard/ups.py`. **Design
  change from the original plan:** no direct GPIO/I2C reads from the daemon
  process — two unprivileged processes independently claiming the same GPIO6
  line via `lgpio` risks an EBUSY conflict, and task 1.2's heartbeat already
  publishes everything needed (soc/vcell/armed/input_lost/on_battery/
  seconds_lost/grace_s). So it's purely file-backed: `read_watchdog_status(path)`
  (parses `/run/canary/ups_status`, `None` if absent/malformed),
  `read_keepalive_remaining(path, now=...)`, `write_keepalive(minutes, path,
  now=...)` (atomic temp+rename), `build_power_snapshot(status,
  keepalive_remaining_s, now=..., stale_after_s=10.0)` (assembles the
  `GET /control/power` payload, flags a stale/dead watchdog). 14 unit tests in
  `tests/test_dashboard_ups.py`.
- [x] **1.4 — `UpsKeepaliveRequest` schema.** Added to `src/dashboard/schemas.py`
  (`extra="forbid"`): `minutes: float = Field(ge=0.0, le=720.0)` (0 = cancel).
  Exported in `__all__`, imported in `control.py`.
- [x] **1.5 — Control endpoints.** In `src/dashboard/routes/control.py`
  `register_control_routes`: `GET /control/power` (via
  `build_power_snapshot` — watchdog_running/armed/input_lost/on_battery/soc/
  vcell/seconds_lost/grace_s/keepalive_remaining_s/status_age_s) and
  `POST /control/power/keepalive` (writes the expiry via `write_keepalive`,
  503 on `OSError` i.e. `/run/canary` missing, `publish_control_event`). Both
  gated on `require_control_auth`. `ups_status_path`/`ups_keepalive_path` are
  now threaded through `register_control_routes` and `create_app` (defaulting
  to the `/run/canary/...` constants, overridable — same pattern as
  `behavior_models_path` etc.) so tests point at `tmp_path` instead of the
  real runtime dir. 8 endpoint tests in `tests/test_control_power.py`.
- [x] **1.6 — MCP tools.** `get_power_status` (`src/mcp_server/tools/power.py`,
  GET `/control/power`) and `set_ups_keepalive`
  (`src/mcp_server/write_tools/power.py`, POST + audited via `MCPAuditLogger`).
  Registered in `server.py` `_register_read_tools` / `_register_write_tools`.
  `docs/mcp-server.md` updated. Tests in `tests/test_mcp_tools.py`
  (passthrough + audit-success + audit-failure).

### 1C. Dashboard

- [x] **1.7 — `PowerPanel.tsx`.** New panel under
  `src/dashboard/static/src/panels/`, modeled on `ProfilesPanel.tsx`: polls
  `GET /control/power` via `useControlResource` (5s interval), shows
  watchdog state/power source/SoC/cell voltage/shutdown countdown (while
  on-battery)/keep-alive hold, and a "keep alive N min" + "Cancel hold"
  control via `useAsyncAction`. Registered in `nav.ts`
  (`control-power`/"Power / UPS"), the `WorkbenchItem` union
  (`types/navigation.ts`), and the `renderItem` switch in `Workbench.tsx`.
- [x] **1.8 — FE API client.** `getPowerStatus(token, signal)` and
  `setKeepAlive(token, minutes)` added to
  `src/dashboard/static/src/api/http.ts` (`controlJson`); response types
  `PowerSnapshot`/`PowerStatusResponse`/`PowerKeepaliveResponse` added to
  `src/dashboard/static/src/types/control.ts` (mirrors the `/control/power`
  backend contract, not `types/live.ts` — that file is for the WS-streamed
  live payload, this is a polled control-plane read).
- [x] **1.9 — FE test.** `PowerPanel.test.ts` mirroring `ProfilesPanel.test.ts`
  (mock `../api/http`, dynamic import); 7 cases: token gate, watchdog
  state+SoC+vcell display, not-running notice, on-battery countdown,
  unknown-on_battery fallback label, set + cancel keep-alive. Full FE suite
  (20 files / 98 tests, incl. the historically-flaky `TuningFeedback.test.ts`)
  and `pnpm typecheck` both clean.
- [x] **1.10 — Deploy.** `install_pi.sh` (tmpfiles), rebuild FE, pull + restart
  `canary` and `ups-shutdown`. Verify: `GET /control/power` returns live SoC;
  keep-alive button pins the watchdog (check `journalctl -u ups-shutdown`).
  Fully done in stages: `pnpm build` on the Pi (98 modules), `canary`
  restarted and confirmed serving the new bundle ("Power / UPS"/"Keep-alive
  minutes" present in the served JS); operator ran `sudo bash
  scripts/install_pi.sh` (creates `/run/canary` via the new tmpfiles.d
  config) and separately `sudo systemctl restart ups-shutdown`. Final live
  state confirmed via `GET /control/power`: `watchdog_running: true`,
  `armed: true`, `soc: 98.3%`, `vcell: 4.17V`, `grace_s: 300.0`,
  `status_age_s` sub-second (fresh heartbeat). The keep-alive POST round-trip
  itself was verified via `tests/test_control_power.py` against a real
  FastAPI app rather than a live browser click-through — the underlying
  write path is the same code now confirmed serving real data end to end.

---

## Phase 2 — MCP quick wins

**Goal.** Kill the recurring per-session friction. Small, high-ROI.

- [x] **2.1 — `get_behavior_live` payload control.** `include_series: bool =
  True` added to `BehaviorLiveState.snapshot()` — omits the per-model
  `series` key when false (rather than an empty list, so the size savings are
  real, not just zeroed). `?series=` query param on `GET
  /models/behavior/live` (default `true`, unchanged FE behavior — the FE
  caller passes no param). MCP tool `get_behavior_live(include_series: bool =
  False)` forwards `series=` to the daemon; `server.py`'s closure exposes the
  same param. Tests: 2 state-level + 2 endpoint-level in
  `test_behavior_live.py`, 2 passthrough/query-forwarding in
  `test_mcp_tools.py`.
- [x] **2.2 — `get_service_health` read tool.** New
  `src/mcp_server/tools/service_health.py`: `systemctl show` (structured
  key=value, not `is-active` + text-scraping) for `canary`/`canary-mcp`/
  `ups-shutdown` (default set, overridable) → `is_active`/`active_state`/
  `sub_state`/`restart_count`/`uptime_seconds`. **Design choice:** used
  `NRestarts` as the crash-loop signal instead of a journald error-text grep
  — it's authoritative and immune to false positives/negatives from log
  wording (the UPS watchdog's own benign heartbeat-write WARNING lines would
  have polluted a text grep), and it's exactly what would have shown the
  2026-07-18 crash-loop (canary.service restarting every ~30s) on the very
  first call instead of ad-hoc `journalctl` scraping over SSH.
  `uptime_seconds` computed from `/proc/uptime` vs. systemd's
  `ActiveEnterTimestampMonotonic` — sidesteps timezone parsing entirely.
  `available: false` when `systemctl` isn't on PATH (dev machine), matching
  `tail_daemon_log`'s fallback pattern. 7 tests in
  `tests/test_mcp_service_health.py`. Documented in `docs/mcp-server.md`.
- [x] **2.3 — Watchdog log tail (doc-only).** Confirmed `tail_daemon_log`
  already accepts `unit`, so `tail_daemon_log(unit="ups-shutdown")` works
  today with no code change. Documented in `docs/mcp-server.md` alongside
  the new `get_service_health` entry.
- [x] **2.4 — `get_status` power block (optional) — SKIPPED, superseded.**
  This task predates Phase 1's `PowerPanel` decision. Folding UPS SoC into
  `SystemPanel.tsx`'s existing power row would either muddy `PowerStatus`'s
  "Pi throttle flags" semantics or duplicate `GET /control/power`'s data
  through a second path for no real benefit — the dedicated Power/UPS panel
  built in 1.7 already shows richer live watchdog state than a single status
  row could. Marked optional in the original plan; judged not worth the
  duplication now that 1.7 exists.

---

## Phase 3 — "Did my change move behavior?" analysis

**Goal.** One call that reproduces the manual mod-effect comparison. De-risked:
the surface evaluator is fully separable — `featurize_wide_frame` +
`append_deviation_columns` run offline, identically to the live path.

- [x] **3.1 — Session deviation analyzer.** New
  `src/analysis/session_deviations.py`: `analyze_session(session_dir, config,
  models)` → `_decode_session_to_resampled` (same per-session decode/resample
  `build_feature_frame` uses) → `featurize_wide_frame(...).operating_frame`.
  **Design change from the sketch:** severity is graded via the *scalar*
  `behavior_decoder.deviation()` per row (not by reading the vectorized
  `deviation:<name>` column, which only carries the raw delta with no
  severity) — keeps this byte-identical to the live/daemon path by
  construction. `SurfaceDeviationSummary`: `n_observed` (mode-gated rows),
  `n_in_domain` (model actually spoke), mean actual/expected/deviation,
  `warning_fraction`/`critical_fraction`. `unknown_operating_modes(models,
  config.preprocessing.operating_modes.keys())` flags surfaces gated on a
  mode this session's config can't emit.
- [x] **3.2 — rpm×load binning (2D surfaces).** `_bin_2d_deviations`: for
  `surface_2d` models, bins each in-domain observation into the model's own
  `Axis2D.bin_edges` (same half-open/closed-last-bin rule as
  `surface_2d._bin_index`, reimplemented locally rather than importing the
  private helper) and reports per-cell `{x_bin, y_bin, n, mean_deviation}` —
  only cells with ≥1 observation, no synthetic zero rows. 7 tests in
  `tests/test_session_deviations.py`, including an injected +5 offset
  recovered exactly and localized to the one visited cell (the task 3.6
  synthetic-offset test, done alongside 3.1/3.2 rather than deferred).
- [x] **3.3 — CLI.** `scripts/analyze_session.py --session <id> [--mode <m>]
  [--profile <slug>] [--behavior-models <path>]`. Uses
  `resolve_profile_for_tooling` + `scope_config` for path scoping (offline,
  no VIN probe) and `src.control.sessions.resolve_session` to look up the
  session id safely. Prints a per-surface table plus a "flagged" section
  (nonzero warning/critical fraction, with the 2D cell breakdown) and any
  `gated_on_missing_mode` entries. `--mode` filters displayed surfaces by
  `operating_mode` (added that field to `SurfaceDeviationSummary`). Fixed a
  cosmetic bug found while eyeballing real output: numpy>=2.0 reprs
  `float64` as `np.float64(0.0)`, which leaked into `cells[].x_bin`/`y_bin` —
  now cast to plain Python `float`. 3 end-to-end tests in
  `tests/test_analyze_session_script.py` (table output, `--mode` filtering,
  unknown-session error path) plus a manual real-fixture run to confirm the
  printed table reads cleanly.
- [x] **3.4 — MCP tool.** `get_behavior_deviations(session, mode?)` in
  `src/mcp_server/tools/behavior.py`, running the analyzer locally (pandas/
  numpy only — no torch), like `query_telemetry`. Added `config_path: Path |
  None = None` to `MCPConfig` (populated from `$CANARY_CONFIG`, which the
  module already read for `storage_path`/`anomalies_dir` but didn't expose
  verbatim) since the tool needs the full typed `CanaryConfig`, not just
  those two derived fields. Conditionally registered in `server.py`
  (mirrors `query_telemetry`'s gate) only when `CANARY_CONFIG` resolves to a
  readable file. Returns `{"ok": False, "error": ...}` on a bad session id
  or missing `behavior_models.yaml`, matching `query_telemetry`'s
  convention. Documented in `docs/mcp-server.md`. 4 tool tests + 2 server
  registration-gating tests + 1 `MCPConfig` round-trip test.
- [x] **3.5 — `query_telemetry` scoping.** Added `session`/`since`/`until` to
  the MCP tool, `telemetry_tool.query_telemetry`, and
  `TelemetryQuery.run`/`_refresh_view`/`_matching_files`. The `rglob` walk
  itself is unavoidable (cheap — filesystem metadata only) but the *result
  list* is filtered before it reaches DuckDB's `parquet_scan`, so excluded
  sessions' parquet content is never opened/parsed. `session` matches the
  immediate parent directory name (works regardless of profile-scoping depth
  between the storage root and session dirs — verified with a test). `since`/
  `until` parse as ISO-8601 or a bare session id and bound each session's own
  timestamp (from its directory name) inclusively; an unparseable session
  directory is excluded under a date filter but kept on an unscoped query
  (preserves existing behavior when no filter is set).

  **Found and fixed a real production bug while testing this:** `duckdb`'s
  Python client lazily imports `pytz` to convert `TIMESTAMPTZ` columns on
  `fetchall()` — `pytz` was never a declared dependency, so any
  `query_telemetry` call returning the raw `timestamp` column would raise
  `InvalidInputException`, in production on the Pi, not just here. Added
  `pytz` to `pyproject.toml`'s base dependencies.
- [x] **3.6 — Tests.** The synthetic-offset test was done alongside 3.1/3.2
  (see there) rather than deferred. Query scoping: 9 new tests in
  `tests/test_mcp_parquet_query.py` — session scoping, nesting-depth
  independence, inclusive since/until ranges, open-ended since, bare-session-id
  bounds, bad-bound validation error, and the unparseable-directory edge case
  that caught the `pytz` gap.

---

## Phase 4 — MCP session resilience (investigate-first)

**Goal.** Stop the transport-session death that forces HTTP/SSH fallbacks. Lower
certainty — investigate before committing an approach.

- [x] **4.1 — Root-cause the session death.** Confirmed server-side by reading
  the installed `mcp` package source (not by reproducing live — the bench Pi
  session for that would need a real client mid-conversation, out of scope
  for a solo debugging pass). `mcp.server.streamable_http_manager.
  StreamableHTTPSessionManager` is stateful by default: `stateless=False`
  tracks each connection's session id in an in-memory
  `_server_instances: dict[str, StreamableHTTPServerTransport]` on the
  server process. Restarting `canary-mcp` (redeploy, crash, systemd flap)
  wipes that dict; any client still holding the old session id gets
  rejected by the *new* process, which never heard of it. Nothing the
  client can recover from alone — this is why `restart claude` (get a fresh
  session) was the only clean fix found live.
- [x] **4.2 — Server-side fix.** `StreamableHTTPSessionManager` already ships
  a `stateless=True` mode ("creates a completely fresh transport for each
  request with no session tracking") — exactly what's needed, already in the
  installed dependency, no new code required beyond wiring it through.
  Exposed as `MCPConfig.mcp_stateless` / `CANARY_MCP_STATELESS=1` → 
  `FastMCP(..., stateless_http=cfg.mcp_stateless)`. **Deliberately opt-in, not
  the new default:** it's a real trade-off (loses SSE resumable-stream/
  server-push, which this codebase's tools don't currently use, but "don't
  currently use" isn't the same guarantee as "verified safe under load") —
  flipping a transport-level default for every MCP client (Claude Code *and*
  Qwen-Agent per this doc) without a runway to notice a regression is exactly
  the kind of change the project's own risk-matching guidance says to pause
  on. Documented in `docs/mcp-server.md` under "Session resilience:
  surviving a `canary-mcp` restart", with a recommendation to run it for a
  while before considering it the default. 4 tests (`MCPConfig` round-trip +
  `build_server` reaching `FastMCP.settings.stateless_http`).
- [x] **4.3 — Fallback documentation.** Root cause turned out server-side
  (4.1), so this became "document the real fix" rather than "hedge on a
  client-side workaround" — done as part of 4.2's doc section, which also
  explains *why* `restart claude` was the only recovery (a fresh client
  session, since the old one is unrecoverable) so a future operator
  understands the mechanism, not just the incantation.

---

## Cross-cutting acceptance

- [x] Whole-repo `ruff` + `mypy` + `pytest` green on every commit; `pnpm test`
  for FE phases (re-run `TuningFeedback.test.ts` on flake before treating as a
  regression). Held throughout — 1848 tests passing at the end (up from 1759
  at the start of this roadmap), 98 FE tests, zero ruff/mypy findings.
- [x] `docs/mcp-server.md` updated for every new/changed tool.
- [x] Each phase deployed to the Pi and verified live. Phase 1 verified
  incrementally during development. Phases 2-4 pulled + `canary`/`canary-mcp`
  restarted at the end (`10d9e4c`); confirmed live: `GET /control/power`
  returns real SoC/vcell/watchdog state (98.4%, 4.17V, armed, on external
  power), an authenticated MCP `initialize` round-trip succeeds against the
  redeployed server.
  Found the running `ups-shutdown` process was still on the stale 30s grace
  (started before the unit file was last updated to `UPS_SHUTDOWN_DELAY=300`)
  — `restart ups-shutdown` isn't in the passwordless sudoers list, so the
  operator ran it manually; confirmed live afterward: `grace_s: 300.0`.

## Code anchor index (from the 2026-07-18 code map)

| Area | Anchor |
|---|---|
| MCP tool registration | `src/mcp_server/server.py` `_register_read_tools` / `_register_write_tools`; bodies in `tools/` + `write_tools/`; `DaemonClient` in `client.py` |
| Control routes | `src/dashboard/routes/control.py` `register_control_routes` (l.76); schemas in `src/dashboard/schemas.py` (`extra="forbid"`) |
| Status payload | `src/dashboard/routes/public.py:47` → `src/dashboard/status.py:327 build_status` (`StatusSnapshot`) |
| behavior live | `src/dashboard/behavior_live.py:101 snapshot`; route `public.py:99` |
| Surface evaluator (offline-capable) | `src/preprocessing/feature_pipeline.py:430 featurize_wide_frame`; `src/decode/behavior_decoder.py` (`deviation`, `append_deviation_columns`); derived channels `src/analysis/derived.py:285`; loader `src/decode/behavior_models.py` |
| Telemetry query | `src/mcp_server/parquet_query.py` (`_matching_files:93`, view rglobs ALL sessions); tool `tools/telemetry.py:37` + `server.py:392` |
| Log tail | `src/mcp_server/tools/logs.py:58` (already `unit`-parameterized) |
| FE panels | `src/dashboard/static/src/panels/ProfilesPanel.tsx` (template); `nav.ts`; `Workbench.tsx renderItem`; `api/http.ts` + `api/core.ts`; hooks `useControlResource`/`useAsyncAction` |
| UPS watchdog | `scripts/ups_shutdown_watch.py`; `systemd/ups-shutdown.service`; `tests/test_ups_shutdown.py` |
