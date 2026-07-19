# Roadmap — MCP tooling & UPS/power hardening

Status: **planned** (2026-07-18). Straight to `main`, no feature branches.

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
- [ ] **1.10 — Deploy.** `install_pi.sh` (tmpfiles), rebuild FE, pull + restart
  `canary` and `ups-shutdown`. Verify: `GET /control/power` returns live SoC;
  keep-alive button pins the watchdog (check `journalctl -u ups-shutdown`).
  **Done except the one interactive-sudo step:** pulled `4630b55` on the Pi,
  `pnpm build` succeeded (98 modules, dist rebuilt), `canary` restarted
  (passwordless) and confirmed serving the new bundle — "Power / UPS" and
  "Keep-alive minutes" verified present in the served JS. `GET /control/power`
  live-tested: 200 OK, graceful `watchdog_running: false` while `/run/canary`
  is absent. `ups-shutdown` is running the new grace/discharge-gate code
  (operator restarted it) but can't write its heartbeat yet.
  **Remaining, needs the operator (only `sudo bash scripts/install_pi.sh` or
  equivalent manual tmpfiles+restart, run interactively — not something I can
  do over this SSH session):** create `/run/canary` (via the tmpfiles.d config
  this phase added) so the watchdog heartbeat starts writing, then click
  through the dashboard keep-alive button once live SoC is flowing.

---

## Phase 2 — MCP quick wins

**Goal.** Kill the recurring per-session friction. Small, high-ROI.

- [ ] **2.1 — `get_behavior_live` payload control.** Add `include_series: bool`
  to `BehaviorLiveState.snapshot()` (`src/dashboard/behavior_live.py:101`) —
  empties the per-model `series` when false. Add `?series=` query param on
  `GET /models/behavior/live` (`src/dashboard/routes/public.py:99`). MCP tool
  `get_behavior_live` gains `include_series: bool = False` (default off — this
  is what overflowed the 275 KB token limit) threaded via `client.get_json(...,
  params=…)`. FE keeps `series=true` (default) for the rolling trail.
- [ ] **2.2 — `get_service_health` read tool.** New read tool returning
  `is-active` + uptime + recent-error count for `canary`, `canary-mcp`,
  `ups-shutdown` (via `systemctl is-active`/`show` — no sudo needed — and a
  journald error grep). Register in `server.py`.
- [ ] **2.3 — Watchdog log tail (doc-only).** `tail_daemon_log` already accepts
  `unit`, so `tail_daemon_log(unit="ups-shutdown")` works today. Document it in
  `docs/mcp-server.md` and add `ups-shutdown` to the tool's known-units hint.
- [ ] **2.4 — `get_status` power block (optional).** Once 1.3 lands, inject SoC
  into the status payload (`src/dashboard/status.py` `build_status`, or appended
  in `public.py:57-60` like `replay`/`sequence_detector`) so `SystemPanel.tsx`'s
  existing power row can show battery state alongside Pi throttle flags.

---

## Phase 3 — "Did my change move behavior?" analysis

**Goal.** One call that reproduces the manual mod-effect comparison. De-risked:
the surface evaluator is fully separable — `featurize_wide_frame` +
`append_deviation_columns` run offline, identically to the live path.

- [ ] **3.1 — Session deviation analyzer.** New
  `src/analysis/session_deviations.py`: `analyze_session(session_dir, config,
  models)` → load the session wide frame (reuse `build_feature_frame` /
  `loader.read_session`) → `result = featurize_wide_frame(wide, config,
  behavior_models=models)` → read `result.operating_frame["deviation:<name>"]`
  columns. Aggregate per surface: `n`, mean actual/expected/deviation,
  `%warning`, `%critical`, optionally grouped by `operating_mode`. Flag
  never-observed gated modes via `unknown_operating_modes(models, config_modes)`.
- [ ] **3.2 — rpm×load binning (2D surfaces).** For `surface_2d` models, also
  return per-cell (rpm-bin × load-bin) mean deviation so a VE/airflow shift
  shows *where* in the operating envelope it lives (idle vs load), not just an
  average. Reuse the model's own `Axis2D.bin_edges`.
- [ ] **3.3 — CLI.** `scripts/analyze_session.py --session <id> [--mode <m>]
  [--profile <slug>]` printing a per-surface deviation table + flagged surfaces.
  Reuse `resolve_profile_for_tooling` for path scoping.
- [ ] **3.4 — MCP tool.** `get_behavior_deviations(session, mode?)` running the
  analyzer locally in the MCP process (pandas/numpy only — no torch), like
  `query_telemetry`. Register in `server.py`; document in `docs/mcp-server.md`.
- [ ] **3.5 — `query_telemetry` scoping.** Add `session: str | None` and
  `since`/`until` params to the MCP tool (`server.py:392`,
  `tools/telemetry.py:37`) and push down into `TelemetryQuery._matching_files`
  (`src/mcp_server/parquet_query.py:93`) — filter the `rglob` result by the
  directory-name UTC timestamp so a scoped query doesn't scan every session.
- [ ] **3.6 — Tests.** Unit-test `analyze_session` against a small synthetic
  session with a known injected offset on one surface (assert the deviation is
  recovered and localized to the right rpm×load cells); test the query
  session/date push-down.

---

## Phase 4 — MCP session resilience (investigate-first)

**Goal.** Stop the transport-session death that forces HTTP/SSH fallbacks. Lower
certainty — investigate before committing an approach.

- [ ] **4.1 — Root-cause the session death.** Confirm it's the FastMCP transport
  session invalidating on `canary-mcp` restart (server side) vs. the Claude Code
  MCP client not reconnecting. Reproduce by restarting `canary-mcp` mid-session.
- [ ] **4.2 — Server-side fix.** If server-side: evaluate stateless-HTTP mode or
  durable/resumable session ids for the MCP server so a daemon restart doesn't
  invalidate live client sessions.
- [ ] **4.3 — Fallback documentation.** If client-side: harden the documented
  HTTP-fallback path (already in `CLAUDE.md`) and add a `canary-mcp` health/
  reconnect note. Keep `restart claude` as the clean recovery.

---

## Cross-cutting acceptance

- [ ] Whole-repo `ruff` + `mypy` + `pytest` green on every commit; `pnpm test`
  for FE phases (re-run `TuningFeedback.test.ts` on flake before treating as a
  regression).
- [ ] `docs/mcp-server.md` updated for every new/changed tool.
- [ ] Each phase deployed to the Pi and verified live before starting the next.

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
