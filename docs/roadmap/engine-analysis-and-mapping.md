# Roadmap — engine performance analysis & mapping

Status: **planned** (2026-07-18). Straight to `main`, no feature branches.

## Why this exists

Goal: rich engine diagnostic and analysis information using every data
source the vehicle offers — all PIDs, DIDs, physics models, learned
maps. A three-part capability review (derived/physics layer, PID/DID
coverage, analysis surfacing) on 2026-07-18 found the foundations are
substantially **built but unwired**:

- `src/analysis/derived.py` already computes fuel flow (3-method
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

- `eval-longitudinal-and-dtc.md` — its Phase 5 digest is **fed by**
  Phase 1 here (`TripSummary` is the digest's engine content); its
  Phase 1 DTC scheduler work (service-request polling) is the same
  mechanism Phase 3 here reuses for Mode 06.
- `ml-correctness-hardening.md` — its Phase 1 (stale-data visibility)
  changes ffill semantics that derived integrals consume; land its 1.1
  before trusting long-session trip integrals across dropouts.
- `imu-gps-integration.md` — grade channel makes Phase-4 power runs
  honest on non-flat roads; baro backs the boost/vacuum default.
- Task 4.E.4 (`docs/implementation-tasks.md`) — injector-PW scale
  correlation capture; Phase 2.5 here depends on it.

## Guiding constraints

- Whole-repo gate before every commit: `ruff check . && ruff format
  --check . && mypy src && pytest` (+ `pnpm test` for FE work).
- Read-only on the bus (NFR-06): everything here is Mode 01/02/06/09/22
  reads; no actuation, no Mode 04.
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
  trip-end hook being added in `eval-longitudinal-and-dtc.md` Phase
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

- [ ] **2.1 — Poll wideband λ.** Add `0x24`/`0x25` (and `0x34`/`0x35`
  variants — probe which the ECM answers) to `PID_DECODERS`
  (`src/collection/can_obd_client.py:517-556`) using the existing
  `decode_wide_lambda` (`obd_registry.py:186-195`). Tier: fast. This
  makes upstream AFR *measured*; `_instant_fuel_g_s` method 2
  (`derived.py:172-176`) and the AFR surfaces upgrade automatically.
- [ ] **2.2 — One-time PID probe capture.** KOEO + engine-running
  capture requesting each registry-decoded-but-unpolled PID once:
  `0x5E` fuel rate, `0x5D` injection timing, `0x61-0x63` torque,
  `0x55-0x58` secondary O2 trims, `0x22/0x23/0x59` rail pressure,
  `0x45/0x47-0x4C/0x5A` throttle/pedal family, `0x66-0x68` (absent
  from registry — add specs first). Add every PID the ECM answers to
  `PID_DECODERS` at an appropriate tier; record the outcome in
  `data/nissan-xterra-2008/` notes.
- [ ] **2.3 — Fix `0x43` ABSOLUTE_LOAD decode.** Registry says 2-byte
  (`obd_registry.py`), collector decodes single-byte percent
  (`can_obd_client.py:551`) — wrong above 100%. Use
  `(A*256+B)*100/255`.
- [ ] **2.4 — Schedule the identified DIDs.** Add the unscheduled
  identified DIDs to the Pi window config
  (`canary.pi.example.yaml:33-37` currently polls 4 of 13):
  at minimum `ECM_INTAKE_VVT_SOLENOID_DUTY` (0x1110),
  `ECM_INTAKE_PRESSURE` pair (0x1206/0x1207),
  `ECM_THROTTLE_POS_SENSOR_2` (0x1210), `ECM_AF_SENSOR_HEATER_DUTY`
  (0x1107). Watch Mode 01 blackout cost (window knobs at
  `src/config.py:108-121`); tier the additions medium.
- [ ] **2.5 — Pin the injector-PW scale (task 4.E.4).** Correlation
  capture: `did:ECM_INJECTOR_PW_B1` vs computed fuel mass per cycle at
  steady states → resolve `scale` in `data/dids.yaml:99-119` so the
  channel reads in ms. Unblocks duty cycle (4.2) and the PW map in ms.
- [ ] **2.6 — Wire the custom-PID engine.** `custom_pids.py` is
  complete but unloaded (docstring `:22-24`, Tasks 6.2/6.13): load
  `data/custom_pids.yaml` at daemon start, merge into
  `build_poll_requests`, decode via the sandboxed evaluator into
  `custom:` channels. Ship an empty-but-documented
  `custom_pids.example.yaml`.
- [ ] **2.7 — DID scale-pinning workbench entries.** For the still-raw
  DIDs (0x1201, 0x1206/07, 0x1110), plan physical-action-tagged
  captures (per the binding protocol) to pin scales; track in
  `data/dids.yaml` notes.
- [ ] **2.8 — UI: surface the new data.** (a) Verify
  `DataSourcesPanel.tsx` picks up the new PIDs/DIDs in its coverage
  tables with live values (it is registry-driven — confirm the new
  entries appear, add source grouping if the OBD tab gets crowded).
  (b) Add an "AFR / fueling" preset group to `CustomDashboardPanel`
  channel picker (λ B1/B2, trims, fuel rate) so the wideband data is
  one tap away. (c) Custom-PID management UI: a small editor in the
  Data group — list/add/delete custom PIDs + **Torque CSV import**
  (backend exists: `torque_csv_to_custom_pids`,
  `custom_pids.py:363`), POSTing to a new authenticated
  `/control/custom-pids` endpoint; FE test per panel convention.

## Phase 3 — Mode 06 / 02 / 09 (rides the DTC reader mechanism)

Depends on `eval-longitudinal-and-dtc.md` Phase 1.3's service-request
scheduling (`kind="dtc"` poll requests generalize to `kind="service"`).

- [ ] **3.1 — Wire Mode 06.** `parse_mode06_response` +
  `MonitorTestResult` (`obd_registry.py:300-342`) have zero callers.
  Add a slow-tier service poll (CAN Mode 06 TID sweep), decode
  per-cylinder **misfire counts** and monitor margins, cache like the
  DTC report, expose via `GET /obd/monitors` + `get_monitors` MCP
  tool. Emit a `source="misfire"` anomaly event on count *increase*
  (ECM-debounced; no persistence gate needed).
- [ ] **3.2 — Misfire trend.** Add per-cylinder misfire counts to the
  session fingerprint stat set (`eval-longitudinal-and-dtc.md` 4.1) —
  slow-building misfire trends are a canonical early-warning signal.
- [ ] **3.3 — Mode 02 freeze frame.** When the DTC reader (Phase 1
  there) sees a stored DTC, read the Mode 02 freeze frame once
  (`SERVICE_REGISTRY[0x02]` metadata + `lookup()` remap exist,
  `obd_registry.py:1194-1252`) and persist it into `dtc.json`.
- [ ] **3.4 — Mode 09 cal-ID/CVN at startup.** Extend the VIN probe
  (`src/profiles.py:193`) to also read InfoTypes 0x04/0x06 (decoders
  exist, `obd_registry.py:1157-1186`); store in the profile registry.
  A changed calibration ID = ECU reflash = corpus boundary candidate
  (ledger integration).
- [ ] **3.5 — UI: Monitors panel.** New `MonitorsPanel.tsx` in the
  diagnostics nav group beside the DTC panel: per-cylinder misfire
  counts (bar per cylinder, delta-since-drive-start highlighted),
  monitor test results with value/min/max margin bars, staleness
  indicator. Freeze-frame data (3.3) renders inside the DTC panel on
  the code it belongs to. Cal-ID/CVN shown in `ProfilesPanel`.
  FE tests per panel convention.

## Phase 4 — Missing physics + new maps

New channels go in `compute_derived_channels` (performance) or
`compute_anomaly_signals` (residuals); new maps are `refit.py` recipes
(gated per `ml-correctness-hardening.md` Phase 4 publish gates).

- [ ] **4.1 — True VE table.** `derived:VE_PERCENT` = measured air
  mass per intake event ÷ theoretical fill (MAP or baro, IAT,
  displacement from `config.py:37` block) — upgrade of the existing
  proxy (`AIR_MASS_PER_CYCLE`, `derived.py:333-339`). Recipe:
  `ve_vs_rpm_load` (2D). Needs MAP (Mode 01 0x0B polled today; DID
  0x1206/07 as cross-check).
- [ ] **4.2 — Injector duty cycle.** `derived:INJECTOR_DUTY_PCT` =
  PW_ms × rpm / 1200 (4-stroke). Depends on 2.5 scale pin. Alert-worthy
  above ~85% sustained; add to fingerprints.
- [ ] **4.3 — Total fuel trim + map.** `derived:TOTAL_TRIM_B1/B2` =
  LTFT+STFT per bank; recipe `total_trim_vs_rpm_load` (2D, per bank).
  This is the vacuum-leak-vs-MAF-skew discriminator (high at idle only
  → unmetered air; high everywhere → MAF/fuel pressure). Review noted
  only STFT-B1 is mapped today (`refit.py:252`).
- [ ] **4.4 — Catalyst efficiency.** Per bank: upstream λ activity vs
  downstream O2 switching frequency/amplitude over a rolling window
  (all four sensors already polled). `derived:CAT_EFFICIENCY_B1/B2`
  (bounded 0-1 index, not a claimed literal efficiency); trend it in
  fingerprints. Cross-check against Mode 06 catalyst monitor margin
  (3.1).
- [ ] **4.5 — O2/AF sensor health.** Downstream sensor switching rate
  + response asymmetry; upstream (wideband) λ noise/bias at steady
  idle. Session-level stats → fingerprints, not live channels.
- [ ] **4.6 — Gear detection (manual trans).** `derived:GEAR_ESTIMATE`
  from rpm/speed ratio clustered to the known ratios (config table in
  the vehicle profile). Enables per-gear analysis, clutch-slip
  detection (ratio drift within a gear), and a better `city`/`highway`
  segmentation input. No TCM on this bus — this is the only gear
  source.
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

- [ ] **5.1 — Binned-analysis endpoint.** `GET /analysis/binned`
  (params: value channel, x channel, y channel optional, bin counts,
  session/date scope, agg = mean|median|p95|count) over the existing
  DuckDB layer (`parquet_query.py`). MCP `get_binned_analysis`. This
  answers "trims by rpm×load" without hand-written SQL or a refit.
- [ ] **5.2 — Ad-hoc map view.** Dashboard "Explore" panel: pick
  value/x/y → heatmap via the existing `Heatmap.tsx` component
  (`BehaviorModelsPanel` already proves the rendering). Read-only,
  backed by 5.1.
- [ ] **5.3 — Historical session charting.** Time-series read endpoint
  (session + channels + optional resample → arrays; DuckDB-backed) +
  ChartsPanel "session mode" (load a stored session instead of the
  live stream). This also fixes the companion-app gap — its docstring
  promises "chart" but no endpoint exists (`src/companion/routes.py`).
- [ ] **5.4 — A/B session comparison.** Extend
  `src/analysis/session_deviations.py` with
  `compare_sessions(a, b)`: per-surface and per-binned-cell deltas
  between two sessions (before-mod vs after-mod), reusing the 5.1
  binning. CLI flag `scripts/analyze_session.py --against <session>`
  + MCP param. This is the mod-effect question asked directly,
  session-to-session, instead of each-vs-model.
- [ ] **5.5 — Chart ergonomics.** `MultiSeriesChart.tsx`: optional
  shared/dual Y axis (per-series self-normalization prevents real
  comparison today), hover cursor with values, X-axis time labels,
  PNG/CSV export of the plotted window. (Zoom can wait; task 6.10
  already tracks it.)
- [ ] **5.6 — Exports.** (a) GPX/KML export of a session's GPS track
  (data captured, no export path exists — grep-confirmed);
  (b) wire the dashboard export panel placeholder (`Workbench.tsx:173`,
  task 6.20) to the existing `scripts/export_dataset.py` backend
  (`src/datasets/export.py:54`) for the CSV→Excel/MegaLogViewer path.

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
- [ ] New maps go through the `ml-correctness-hardening.md` Phase-4
  publish gates (R²/coverage, σ-floor) once those land.
- [ ] Bus-impact check after Phase 2/3 changes: verify Mode 01
  freshness (`obd_freshness_seconds`) hasn't degraded from added
  polling / DID windows.
- [ ] Each phase verified live on the Pi before the next.

## Code anchor index (from the 2026-07-18 capability review)

| Area | Anchor |
|---|---|
| Derived perf channels | `src/analysis/derived.py:216-282` (`compute_derived_channels`); fuel chain `:157-188`; power `:191-213` |
| TripSummary (unwired) | `derived.py:379-517`; accel metrics `:400-447`; callers: tests only |
| Live pipeline import gap | `src/preprocessing/feature_pipeline.py:26,461` (only `compute_anomaly_signals`) |
| Poll set vs registry | `can_obd_client.py:517-556 PID_DECODERS` (~38 polled) vs `obd_registry.py:373-1150` (~90 decoded) |
| Wideband λ decoder | `obd_registry.py:186-195 decode_wide_lambda`; PIDs 0x24-0x2B/0x34-0x3B specs `:539-772` |
| auto PID discovery | `obd_scheduler.py:398-459 build_poll_requests` (`:422-430`); tiers `:24-29` |
| 0x43 decode bug | `can_obd_client.py:551` (single-byte) vs registry 2-byte |
| Mode 06 parser (unwired) | `obd_registry.py:300-342`; service metadata `:1209-1216` |
| Mode 09 decoders (VIN-only use) | `obd_registry.py:1157-1186`; probe `src/profiles.py:193` |
| DID catalog | `data/dids.yaml` (13 identified; access `:24-37`); decode `src/decode/dids.py:110-201` |
| DID window scheduler | `obd_scheduler.py:194-328 DidWindowController`; knobs `src/config.py:108-121`; Pi set `canary.pi.example.yaml:33-37` |
| Custom-PID engine (unwired) | `src/collection/custom_pids.py` (read-only modes `:59`; deferred note `:22-24`) |
| Surface recipes (14) | `src/analysis/refit.py:133-473 default_recipes` |
| Heatmap/curve viewer | `panels/BehaviorModelsPanel.tsx:47`; `components/Heatmap.tsx:88-129`; `Curve.tsx` |
| Live-only charting | `panels/ChartsPanel.tsx:20-56`; `components/MultiSeriesChart.tsx:53-77` (self-normalized series) |
| Session deviation analyzer | `src/analysis/session_deviations.py`; CLI `scripts/analyze_session.py`; MCP `get_behavior_deviations` |
| DuckDB query layer | `src/mcp_server/parquet_query.py`; MCP `query_telemetry` |
| Dataset export backend | `src/datasets/export.py:54`; CLI `scripts/export_dataset.py`; UI placeholder `Workbench.tsx:173` |
| Trip odometer (only surfaced trip stat) | `src/analysis/gps_tracker.py:114`; `SystemPanel.tsx:96,125-126` |
