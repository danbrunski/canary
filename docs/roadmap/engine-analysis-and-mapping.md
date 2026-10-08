# Roadmap — engine performance analysis & mapping

Status: **planned** (2026-07-18). Straight to `main`, no feature branches.

## Why this exists

Goal: rich engine diagnostic and analysis information using every data
source the vehicle offers — all PIDs, DIDs, physics models, learned
maps. A three-part capability review (derived/physics layer, PID/DID
coverage, analysis surfacing) on 2026-07-18 found the foundations are
substantially **built but unwired**:

- `src/signals/derived.py` already computes fuel flow (3-method
  fallback chain), instant/average economy, CO₂, boost/vacuum,
  estimated HP/torque, and a `TripSummary` with 0–60 and quarter-mile
  metrics — **called only from tests**. Nothing streams the derived
  channels live and nothing surfaces the trip summary.
- The Mode 01 registry decodes ~90 PIDs but the collector polls only
  the ~38 in `PID_DECODERS`. The VQ40DE's **upstream wideband A/F λ
  PIDs (0x24/0x25) are decoded-but-never-polled** — true measured AFR
  is currently approximated from a raw-count DID voltage.
- A complete **Mode 06 parser** (per-cylinder misfire counts, monitor
  margins) exists with zero callers. A complete **custom-PID engine**
  (sandboxed formula eval, Torque-CSV import) exists with no daemon
  wiring and zero PIDs defined. 9 of 13 identified Nissan DIDs are
  unscheduled.
- The dashboard renders learned 2D surfaces as heatmaps with live
  overlay (genuinely good), but charting is live-only with no axes/
  zoom/export, there is no historical-session charting anywhere
  (including the companion app), no binned-analysis endpoint, and no
  A/B session comparison.

This roadmap closes those gaps in leverage order: wire what exists →
collect what the vehicle offers → compute the missing physics → build
the analysis UX.

## Cross-links

- `archive/eval-longitudinal-and-dtc.md` — its Phase 5 digest is **fed by**
  Phase 1 here (`TripSummary` is the digest's engine content); its
  Phase 1 DTC scheduler work (service-request polling) is the same
  mechanism Phase 3 here reuses for Mode 06.
- `archive/ml-correctness-hardening.md` — its Phase 1 (stale-data visibility)
  changes ffill semantics that derived integrals consume; land its 1.1
  before trusting long-session trip integrals across dropouts.
- `imu-gps-integration.md` — grade channel makes Phase-4 power runs
  honest on non-flat roads; baro backs the boost/vacuum default.
- Task 4.E.4 (`docs/implementation-tasks.md`) — injector-PW scale
  correlation capture; Phase 2.5 here depends on it.

## Guiding constraints

- Whole-repo gate before every commit: `ruff check . && ruff format
  --check . && mypy src && pytest` (+ `pnpm test` for FE work).
- Everything in this roadmap is Mode 01/02/06/09/22 reads — no actuation,
  no Mode 04. That is this work's own scope, not a platform restriction.
- New physics channels follow `derived.py` conventions: pure functions,
  gated on inputs (absent → NaN/None, never fake zeros), engine-
  performance channels in `compute_derived_channels`, residuals in
  `compute_anomaly_signals` — keep the split.
- Adding channels to the live stream changes what models *could* see:
  keep new `derived:` performance channels out of training features
  unless deliberately added (the `NON_TRAINABLE_COUNTER_CHANNELS`
  precedent).
- Mode-22 DID polling costs Mode 01 blackout windows (session `10 C0`
  is mutually exclusive with Mode 01) — schedule DIDs consciously.
- Deploy: config changes need Pi `canary.yaml` updates + restart.

---

## Phase 1 — Wire what's already built (days of work, biggest felt gain)

### 1A. Derived channels into the live stream

- [x] **1.1 — Stream `compute_derived_channels` live.** The live path
  imports only `compute_anomaly_signals`
  (`src/preprocessing/feature_pipeline.py:26,461`). Add the
  performance channels (`derived.py:216-282`: fuel rate, instant/avg
  economy, trip distance/fuel/CO₂, idle time, `BOOST_VACUUM_KPA`,
  `ESTIMATED_POWER_HP`/`TORQUE_NM`) to the live tick so they stream
  over `/ws/telemetry` and land in Parquet. Cumulative channels stay
  excluded from model features (`NON_TRAINABLE_COUNTER_CHANNELS`,
  `derived.py`). — landed: `compute_instantaneous_channels` split out
  and wired into `featurize_wide_frame` (shared by batch + live);
  streams over `/ws/telemetry` via `_publish_derived_channels` and
  lands in Parquet via `AsyncParquetTelemetryWriter.write_many`
  (best-effort, same writer object replay-mode redirection already
  covers). Initial pass shipped WS-only; review caught the missing
  Parquet persistence against the task's explicit wording before this
  was checked off, fixed with real end-to-end coverage (read-back from
  a written Parquet file, not a mock).
- [x] **1.2 — Chart them.** Verify ChartsPanel + CustomDashboardPanel
  pick up the new `derived:` channels automatically (they chart any
  streaming numeric channel); add them to the channel-picker grouping
  if sources are grouped. Boost/vacuum, MPG, and estimated HP become
  live gauges for free. — verified, zero code changes needed: both
  panels build their channel picker generically off the live channel
  map with no source allow-list or grouping to extend.
- [x] **1.3 — Session-persist the integrals.** Confirm trip integrals
  reset per session (they are cumsum over the frame — verify behavior
  across the live ring buffer vs batch; integrals may need daemon-side
  accumulators rather than window cumsum). Unit-test parity between
  batch `compute_derived_channels` and the live accumulation. — landed:
  `LiveFeatureSource` gained real wall-clock-`dt` session-scoped
  accumulators (not a windowed cumsum, which would have reset on every
  ring-buffer eviction); zeroed by the same Phase-7.1 `reset()` used for
  replay-boundary isolation. Parity test confirms non-degenerate,
  matching final values against the batch path.

### 1B. Trip summary surfaced

- [x] **1.4 — `summarize_trip` at session close.** Call
  `summarize_trip` (`derived.py:379-517`) when a session ends (the
  trip-end hook being added in `archive/eval-longitudinal-and-dtc.md` Phase
  5.4; until that lands, compute on demand from the stored session).
  Persist `trip_summary.json` into the session dir (atomic, like
  `control.json`). — landed: `get_or_compute_trip_summary`
  (compute-once-then-cache, atomic tmp+rename sidecar, same pattern as
  `classification.json`/`fingerprint.json`); confirmed Phase 5.4's
  session-close hook genuinely doesn't exist yet, so on-demand-only is
  correct, not a shortcut.
- [x] **1.5 — Endpoints + MCP.** `GET /sessions/{id}/trip-summary`
  (public read) + `get_trip_summary` MCP tool; include the summary in
  the Phase-5 digest payload. Document in `docs/mcp-server.md` +
  `docs/headless-api.md`. — landed: public route + MCP tool exactly as
  specified; Phase-5 digest integration skipped since that payload
  doesn't exist yet. Also filled a pre-existing gap: `/obd/dtc` was
  missing from `headless-api.md`'s REST table.
- [x] **1.6 — Dashboard trip panel.** New `TripPanel.tsx` (nav group
  `live` or `data`): current-drive economy/fuel/CO₂/idle fraction from
  the streamed cumulative channels, plus last-drive summary from 1.5.
  Replace the odometer-only rows in `SystemPanel.tsx:96,125-126` with
  a pointer. Add `avg_speed` to `TripSummary` while in there (noted
  absent in review). — landed: reads the live `derived:*` WS channels
  directly (no re-fetch/recompute), verified against the exact channel
  names `LiveFeatureSource` emits; `avg_speed_kph = distance/duration`
  (the defensible trip-computer definition); `SystemPanel.tsx`'s
  odometer rows + dead formatting helpers cleanly removed.
- [x] **1.7 — CLI.** `scripts/trip_report.py <session>` printing the
  summary table for any stored session (works off 1.4's on-demand
  path) — the "drive report" CLI that doesn't exist today. — landed:
  works end-to-end against a real stored session; `--rebuild` verified
  to genuinely force recomputation, not silently serve cached data.

## Phase 2 — Collect what the vehicle offers (Mode 01 + DIDs)

- [x] **2.1 — Poll wideband λ.** Add `0x24`/`0x25` (and `0x34`/`0x35`
  variants — probe which the ECM answers) to `PID_DECODERS`
  (`src/collection/can_obd_client.py:517-556`) using the existing
  `decode_wide_lambda` (`obd_registry.py:186-195`). Tier: fast. This
  makes upstream AFR *measured*; `_instant_fuel_g_s` method 2
  (`derived.py:172-176`) and the AFR surfaces upgrade automatically. —
  landed: all four documented variants added at fast tier (real probe
  of which the ECM answers is 2.2, still blocked-overnight). Review
  found this doc's own "upgrades automatically" claim was actually
  FALSE in code — `_instant_fuel_g_s` method 2 only ever read
  `obd:COMMANDED_EQUIV_RATIO`, no wideband lookup existed — fixed by
  widening the column preference to measured λ first, commanded as
  fallback.
- [x] **2.2 — One-time PID probe capture.** Landed 2026-07-22: real
  KOEO + engine-running probe via `tools/can_obd_probe.py`
  (`data/nissan-xterra-2008/pid-probe-2026-07-22.md`) — identical
  41-PID supported set in both states. None of the fuel-rate/
  injection-timing/torque/rail-pressure/secondary-O2 targets are
  supported (expected — all DI/diesel-oriented on a port-injected V6);
  the ECM's bitmask tops out at `0x4E`, below `0x55` onward entirely.
  Of the throttle/pedal family, `0x47`/`0x49`/`0x4A`/`0x4C` ARE
  answered (`0x48`/`0x4B` confirmed not) — decoders already existed in
  `MODE01_REGISTRY` but weren't in the active poll set; added to
  `can_obd_client.PID_DECODERS` (tiers flow automatically from the
  registry via `_pid_channel_and_tier`'s `lookup()`).
- [x] **2.3 — Fix `0x43` ABSOLUTE_LOAD decode.** Registry says 2-byte
  (`obd_registry.py`), collector decodes single-byte percent
  (`can_obd_client.py:551`) — wrong above 100%. Use
  `(A*256+B)*100/255`. — landed: `decode_absolute_load`; regression
  test confirms a real above-100% value (raw 0x0190 ≈ 156.9%) that the
  old single-byte decode would have wrapped to ≤100%.
- [ ] **2.4 — Schedule the identified DIDs.** Add the unscheduled
  identified DIDs to the Pi window config
  (`canary.pi.example.yaml:33-37` currently polls 4 of 13):
  at minimum `ECM_EVAP_PURGE_CMD` (0x1110; commanded EVAP purge, renamed
  2026-10-04 from a wrong "intake VVT solenoid duty" identification),
  `ECM_INJECTOR_PULSE_1/2` pair (0x1206/0x1207 — injected pulse; once
  mislabelled intake pressure),
  `ECM_THROTTLE_POS_SENSOR_2` (0x1210), `ECM_AF_SENSOR_HEATER_DUTY_B1/B2`
  (0x114E/0x114F; 0x1107 was catalogued as heater duty until 2026-10-07 and
  is ignition timing, D03.40). Watch Mode 01 blackout cost (window knobs at
  `src/config.py:108-121`); tier the additions medium.
- [ ] **2.5 — Pin the injector-PW scale (task 4.E.4).** *Retargeted
  2026-09-18: 0x1208 is air charge (`did:ECM_AIR_CHARGE`), not injector PW;
  the injected pulse is 0x1206/0x1207 (`did:ECM_INJECTOR_PULSE_1/2`), see
  D03.9.* Correlation
  capture: `did:ECM_INJECTOR_PULSE_1` vs computed fuel mass per cycle at
  steady states → resolve `scale` in `data/dids.yaml:99-119` so the
  channel reads in ms. Unblocks duty cycle (4.2) and the PW map in ms.
- [x] **2.6 — Wire the custom-PID engine.** `custom_pids.py` is
  complete but unloaded (docstring `:22-24`, Tasks 6.2/6.13): load
  `data/custom_pids.yaml` at daemon start, merge into
  `build_poll_requests`, decode via the sandboxed evaluator into
  `custom:` channels. Ship an empty-but-documented
  `custom_pids.example.yaml`. — landed: loaded once at startup
  (canable branch only — elm327 has no scheduler concept to thread
  into), merged into `build_poll_requests` as `kind="custom"`, decoded
  via the existing (untouched, unweakened) sandboxed evaluator. One
  disclosed, bounded gap: a Mode-22 custom PID on a session-gated ECU
  isn't yet integrated with the DID-window controller — it gets sent
  outside the session window and cleanly no-ops (short response →
  `[]`, no crash, no DID-window interference), not silently broken.
- [ ] **2.7 — DID scale-pinning workbench entries.** For the still-raw
  DIDs (0x1201, 0x1206/07, 0x1110), plan physical-action-tagged
  captures (per the binding protocol) to pin scales; track in
  `data/dids.yaml` notes.
- [x] **2.8 — UI: surface the new data.** (a) Verify
  `DataSourcesPanel.tsx` picks up the new PIDs/DIDs in its coverage
  tables with live values (it is registry-driven — confirm the new
  entries appear, add source grouping if the OBD tab gets crowded).
  (b) Add an "AFR / fueling" preset group to `CustomDashboardPanel`
  channel picker (λ B1/B2, trims, fuel rate) so the wideband data is
  one tap away. (c) Custom-PID management UI: a small editor in the
  Data group — list/add/delete custom PIDs + **Torque CSV import**
  (backend exists: `torque_csv_to_custom_pids`,
  `custom_pids.py:363`), POSTing to a new authenticated
  `/control/custom-pids` endpoint; FE test per panel convention. —
  landed: (a) wideband PIDs needed zero backend changes (already
  registry-driven); found and fixed a real gap where `custom:*`
  channels were invisible (`obd_coverage.py` is deliberately scoped to
  the J1979 registry) — surfaced as a separate subsection, not merged
  into the main coverage ratio. (b) `CustomDashboardPanel` had no
  preset-group mechanism at all yet — built it plus the first group,
  channel names verified against the real decoders. (c) new
  authenticated CRUD (`GET`/`POST`/`DELETE`/`import`) — confirmed all
  four routes genuinely require auth, not just some; edits take effect
  on next daemon restart only (`custom_pids.yaml` has no hot-reload
  path, unlike `behavior_models.yaml`, confirmed by reading both).

## Phase 3 — Mode 06 / 02 / 09 (rides the DTC reader mechanism)

Depends on `archive/eval-longitudinal-and-dtc.md` Phase 1.3's service-request
scheduling (`kind="dtc"` poll requests generalize to `kind="service"`).

- [x] **3.1 — Wire Mode 06.** Landed: new `kind="service"` scheduler
  poll (`src/collection/obd_scheduler.py`) rate-limited by its own
  `service_poll_interval_seconds` knob, fully decoupled from real-time
  PID bus budget. `parse_mode06_response`/`MonitorTestResult` now
  called from `src/control/monitor_state.py::MonitorState`, which
  caches the sweep and emits `source="misfire"` on per-cylinder count
  *increase* only (a counter reset — e.g. after key-cycle — becomes
  the new baseline, not a false decrease-anomaly). `GET /obd/monitors`
  + `get_monitors` MCP tool wired end-to-end (not stubs), documented
  in `docs/mcp-server.md`/`docs/headless-api.md`.
- [x] **3.2 — Misfire trend.** Landed:
  `src/analysis/fingerprint.py::_add_misfire_stats` adds session-max
  per-cylinder misfire count (counters are cumulative within a drive)
  to the fingerprint stat set, scanning `obd:MISFIRE_COUNT_CYLn`
  columns with no hardcoded cylinder count.
- [x] **3.3 — Mode 02 freeze frame.** Landed: `FreezeFrameTrigger`
  fires once when a DTC status transitions to stored for the first
  time this session (`src/control/dtc_state.py`), reads the Mode 02
  frame, and persists it via `DtcReport.freeze_frame` through the
  sidecar's existing atomic tmp+rename write — no separate/bolted-on
  writer.
- [x] **3.4 — Mode 09 cal-ID/CVN at startup.** Landed:
  `src/profiles.py::probe_vehicle_info`/`VehicleInfoProbe` reads
  InfoTypes 0x04/0x06 alongside the VIN probe; stored as
  `VehicleProfile.calibration_id`/`cvn`. A changed cal-ID on an
  already-registered profile appends a `kind="mod",
  corpus_boundary=True` `VehicleLedgerStore` entry; a brand-new
  profile's first-ever probe correctly appends nothing (no prior value
  to compare against).
- [x] **3.5 — UI: Monitors panel.** Landed: new `MonitorsPanel.tsx`
  (diagnostics nav group) — per-cylinder misfire bars with
  delta-since-baseline, monitor test value/min/max margin bars,
  staleness indicator, real `/obd/monitors` data (not mocked-only).
  Freeze-frame (3.3) renders inside the existing `DtcPanel.tsx`.
  Cal-ID/CVN (3.4) shown in `ProfilesPanel`. FE tests per panel
  convention. All live-truck verification blocked-overnight per
  OVERNIGHT-QUEUE.md Q14 — code + fixture/mocked tests only.

## Phase 4 — Missing physics + new maps

New channels go in `compute_derived_channels` (performance) or
`compute_anomaly_signals` (residuals); new maps are `refit.py` recipes
(gated per `archive/ml-correctness-hardening.md` Phase 4 publish gates).

- [x] **4.1 — True VE table.** Landed: `derived:VE_PERCENT` = measured
  air mass/cycle ÷ theoretical 100%-VE fill (MAP-or-baro/IAT/
  `vehicle.displacement_l`, MAP tried first, baro only as fallback when
  MAP is genuinely absent); gated on absent MAF/RPM/vehicle/pressure.
  Recipe `ve_vs_rpm_load` added through the existing gated recipe
  pipeline. DID 0x1206/07 cross-check deliberately deferred — those
  DIDs are still scale-unpinned (task 2.7) and unscheduled (task 2.4).
- [ ] **4.2 — Injector duty cycle.** `derived:INJECTOR_DUTY_PCT` =
  PW_ms × rpm / 1200 (4-stroke). Depends on 2.5 scale pin. Alert-worthy
  above ~85% sustained; add to fingerprints.
- [x] **4.3 — Total fuel trim + map.** Landed: `derived:TOTAL_TRIM_B1/B2`
  = STFT+LTFT per bank (each independently gated on both inputs
  present). Recipes `total_trim_vs_rpm_load_b1`/`_b2` added through the
  same gated pipeline.
- [x] **4.4 — Catalyst efficiency.** Landed: `derived:CAT_EFFICIENCY_B1/B2`
  = `1 - clip(downstream_switches / upstream_switches, upper=1.0)` over
  a rolling window — genuinely bounded [0,1], documented as a heuristic
  index, not literal efficiency. Trended in fingerprint stats. Mode 06
  catalyst-monitor-margin cross-check (3.1) deliberately deferred — that
  margin is a pass/fail result, not a continuous series, and doesn't
  align with a rolling trend without forcing something fragile.
- [x] **4.5 — O2/AF sensor health.** Landed: fingerprint-only stats
  (no new live channel, per the doc's explicit instruction) —
  downstream switching rate + rich/lean dwell asymmetry, upstream
  wideband λ noise/bias restricted to steady idle.
- [x] **4.6 — Gear detection (manual trans).** Landed:
  `derived:GEAR_ESTIMATE` via nearest-ratio clustering of rpm/speed
  against a new `VehicleParameters.gear_ratios` config table (the
  vehicle-profile ratio table the task asked for — `VehicleProfile.
  parameters` already is a `VehicleParameters`), with a low-speed floor
  masking numerically-unstable near-zero-speed ratios and a clean gate
  on an empty/unset table. Excluded from ML training as a categorical
  cluster label (continuous physics channels above stay trainable).
  Clutch-slip stretch goal (ratio drift within a gear) not built —
  doc marks it optional, deferred to a later task.
- [ ] **4.7 — BSFC / efficiency estimate.** At steady cruise cells:
  fuel g/s ÷ estimated crank kW → `derived:BSFC_G_PER_KWH`; map vs
  rpm×load. Flag it clearly as estimate-on-estimate (both operands are
  modeled); value is in *trend*, not absolute number.
- [ ] **4.8 — Grade-corrected power runs.** When
  `imu-gps-integration.md` Phase 3 lands grade: correct
  `_accel_metrics` (`derived.py:400-447`) and `ESTIMATED_POWER` for
  road grade + note density altitude (baro + IAT) in the trip summary.
- [ ] **4.9 — UI: Engine Health panel + widgets.** (a) New
  `EngineHealthPanel.tsx` (diagnostics group): tiles for injector duty
  %, catalyst-efficiency index per bank, total trim per bank with an
  idle-vs-load split readout, O2-health stats, each with a sparkline
  and reference band. (b) Gear indicator widget (4.6) available to
  `CustomDashboardPanel` and the Drive screen. (c) New maps (VE,
  total-trim, BSFC) appear in `BehaviorModelsPanel` automatically once
  their recipes are fitted — verify axis labels/units render sensibly
  for the new outputs and add unit formatting where missing.
  (d) Power-run view: after a detected launch, show the 0-60 /
  quarter-mile result as a dismissible card on the Drive screen (data
  from 1.4's summary path, live-computed at trip end).

## Phase 5 — Analysis UX (stored data becomes explorable)

- [x] **5.1 — Binned-analysis endpoint.** Landed: `GET /analysis/binned`
  + MCP `get_binned_analysis`, session/date scope pushed to the DuckDB
  query itself (not fetch-then-filter). Quantile-spaced bins with
  mean|median|p95|count agg; degenerate (all-identical-value) channels
  gate to a clean single bin rather than crashing.
- [x] **5.2 — Ad-hoc map view.** Landed: new `ExplorePanel.tsx`
  (diagnostics group) — value/x/y/bins/agg/session/date-range form,
  reuses `Heatmap.tsx`, display-only, backed by 5.1.
- [x] **5.3 — Historical session charting.** Landed: new time-series
  read endpoint (native per-channel timestamps by default, one shared
  resampled grid only when `resample_hz` requested — doesn't force
  multi-rate channels onto one grid unasked) + `ChartsPanel` session
  mode. Also lands the companion-app route its docstring already
  promised but that never existed.
- [x] **5.4 — A/B session comparison.** Landed:
  `compare_sessions(a, b)` grades both sessions against the identical
  fitted surface's own bin edges, so cell (i,j) means the same
  rpm×load bin in both sessions — genuinely comparable, not
  independently rebinned per session. Consistent after-minus-before
  delta sign throughout. CLI `--against` flag + MCP param.
- [x] **5.5 — Chart ergonomics.** Landed: `MultiSeriesChart.tsx` gets
  `yAxisMode` (independent/shared — shared genuinely puts every series
  on one real scale), hover crosshair, X-axis time labels, PNG/CSV
  export. First pass shipped the shared-axis logic without a reachable
  UI control; fixed same task — `ChartsPanel.tsx` now has a toggle
  wired through to both live and session mode (one shared call site).
- [x] **5.6 — Exports.** Landed: GPX/KML session GPS-track export
  (hand-written XML, no new dependency; correct GPX lat/lon-attribute
  vs. KML lon,lat,alt-comma coordinate order — verified, this is a
  classic easy-to-invert bug). Dashboard export placeholder replaced
  with a real `ExportPanel.tsx` wired to the existing
  `scripts/export_dataset.py`/`src/datasets/export.py` backend via a
  new `export_dataset` job kind.

---

## Cross-cutting acceptance

- [ ] Whole-repo `ruff` + `mypy` + `pytest` green per commit; `pnpm
  test` for FE phases.
- [ ] Every new channel: unit test with absent-input gating (NaN/None,
  no fake zeros), and an explicit decision recorded on whether it is
  model-trainable or excluded.
- [ ] Every new endpoint/MCP tool documented in `docs/mcp-server.md` /
  `docs/headless-api.md`.
- [ ] **Every new data source or derived channel ships with a UI
  surface** — at minimum visible in the channel pickers
  (Charts/CustomDashboard) and the relevant coverage/health panel; no
  backend-only features (the review found a pattern of built-but-
  invisible capability — this rule is the guard against repeating it).
- [ ] New maps go through the `archive/ml-correctness-hardening.md` Phase-4
  publish gates (R²/coverage, σ-floor) once those land.
- [ ] Bus-impact check after Phase 2/3 changes: verify Mode 01
  freshness (`obd_freshness_seconds`) hasn't degraded from added
  polling / DID windows.
- [ ] Each phase verified live on the Pi before the next.

## Code anchor index (from the 2026-07-18 capability review)

| Area | Anchor |
|---|---|
| Derived perf channels | `src/signals/derived.py:216-282` (`compute_derived_channels`); fuel chain `:157-188`; power `:191-213` |
| TripSummary (unwired) | `derived.py:379-517`; accel metrics `:400-447`; callers: tests only |
| Live pipeline import gap | `src/preprocessing/feature_pipeline.py:26,461` (only `compute_anomaly_signals`) |
| Poll set vs registry | `can_obd_client.py:517-556 PID_DECODERS` (~38 polled) vs `obd_registry.py:373-1150` (~90 decoded) |
| Wideband λ decoder | `obd_registry.py:186-195 decode_wide_lambda`; PIDs 0x24-0x2B/0x34-0x3B specs `:539-772` |
| auto PID discovery | `obd_scheduler.py:398-459 build_poll_requests` (`:422-430`); tiers `:24-29` |
| 0x43 decode bug | `can_obd_client.py:551` (single-byte) vs registry 2-byte |
| Mode 06 parser (unwired) | `obd_registry.py:300-342`; service metadata `:1209-1216` |
| Mode 09 decoders (VIN-only use) | `obd_registry.py:1157-1186`; probe `src/collection/vehicle_probe.py` |
| DID catalog | `data/dids.yaml` (13 identified; access `:24-37`); decode `src/decode/dids.py:110-201` |
| DID window scheduler | `obd_scheduler.py:194-328 DidWindowController`; knobs `src/config.py:108-121`; Pi set `canary.pi.example.yaml:33-37` |
| Custom-PID engine (unwired) | `src/collection/custom_pids.py` (legacy permitted-service set `:59`; deferred note `:22-24`) |
| Surface recipes (14) | `src/analysis/refit.py:133-473 default_recipes` |
| Heatmap/curve viewer | `panels/BehaviorModelsPanel.tsx:47`; `components/Heatmap.tsx:88-129`; `Curve.tsx` |
| Live-only charting | `panels/ChartsPanel.tsx:20-56`; `components/MultiSeriesChart.tsx:53-77` (self-normalized series) |
| Session deviation analyzer | `src/analysis/session_deviations.py`; CLI `scripts/analyze_session.py`; MCP `get_behavior_deviations` |
| DuckDB query layer | `src/mcp_server/parquet_query.py`; MCP `query_telemetry` |
| Dataset export backend | `src/datasets/export.py:54`; CLI `scripts/export_dataset.py`; UI placeholder `Workbench.tsx:173` |
| Trip odometer (only surfaced trip stat) | `src/analysis/gps_tracker.py:114`; `SystemPanel.tsx:96,125-126` |
