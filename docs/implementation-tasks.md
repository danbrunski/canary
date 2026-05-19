# CANary — Implementation Plan

Derived from `docs/CANary-overview.md`. Tasks are grouped by phase, with FR/NFR
references in parentheses. Each task is sized to be roughly one PR or one
working session. Items inside a phase are listed in execution order; cross-phase
dependencies are called out explicitly.

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done.

Status (as of 2026-05-17): Phases 0–5 are shipped (dual-channel
collection, feature pipeline, Isolation Forest + LSTM, CAN RE/bindings,
behavior models, dashboard, alerts, model management, Pi service,
authenticated control plane). OBD runs the Canable-native path (ELM327
adapter is dead): auto Mode 01 polling of every decoder-backed PID plus
the Nissan `10 C0` Mode 22 DID profile. Phase 6 substantially landed —
Torque-parity telemetry, OBD coverage reporting, dataset/training/tuning
ML-ops, and the reworked dashboard. Remaining work is hardware-gated or
in-truck (live-ECU reads, write/parked actions, companion sync/publish,
acceptance matrix) plus the Phase 7 anomaly-architecture hardening from
the 2026-05-17 review. The Phase 8 storage-lifecycle/disk-safety fix from
the 2026-05-17 disk investigation is landed (8.1–8.5): the production
daemon now runs a periodic in-process retention sweeper doing age +
byte-cap (`storage_max_mb`) + emergency low-space eviction, always
protecting the active and pinned sessions; only the optional P2
operator-intent/idle-gating design task (8.6) remains. Phase 9 adds a TCN
sequence detector alongside the Isolation Forest and LSTM without changing
the current IF/LSTM v1 acceptance gate.

Completed tasks below are collapsed to their title line to keep this
plan navigable; the full original description for any `[x]` item is in
git history. `[ ]` / `[~]` items are kept in full.

---

## Phase 0 — Project Bootstrap

Goal: a runnable empty skeleton, reproducible dev env, CI green on first push.

- [x] **0.1 — Repo skeleton.** Create the directory layout from
- [x] **0.2 — Python tooling.** `pyproject.toml` (PEP 621) targeting Python
- [x] **0.3 — `requirements.txt` / extras.** Runtime deps (python-obd,
- [x] **0.4 — Config loader.** `src/config.py` — Pydantic models mirroring the
- [x] **0.5 — Logging.** Structured logging (stdlib `logging` + JSON formatter)
- [x] **0.6 — Entry point.** `src/main.py` parses `--config`, loads YAML,
- [x] **0.7 — CI.** GitHub Actions: lint, type-check, unit tests on
- [x] **0.8 — Dev container.** `Dockerfile` (python:3.12-slim, deps baked

---

## Phase 1 — Data Collection (Overview Phase 1)

Goal: dual-channel telemetry into Parquet, validated against the emulator.

### 1A. OBD-II collector (FR-01)

- [x] **1.1 — PID registry.** `src/collection/pids.py` — declarative list of
- [x] **1.2 — Async collector.** `src/collection/obd_collector.py` using
- [x] **1.3 — Reconnect logic.** Detect adapter disconnect, retry with
- [x] **1.4 — Read-only enforcement.** `src/collection/obd_collector.py` —
- [x] **1.5 — Integration tests with ELM327-emulator.** `tests/test_obd_emulator.py`

### 1B. CAN collector (FR-02)

- [x] **1.6 — SocketCAN bring-up doc.** `docs/can-setup.md` — `ip link set
- [x] **1.7 — Async CAN reader.** `src/collection/can_collector.py` —
- [x] **1.8 — DBC decode hook (reader).** `src/decode/dbc.py` —
- [x] **1.9 — Synthetic CAN replay test.** Use `python-can`'s

### 1C. Storage (FR-03, NFR-03)

- [x] **1.10 — Parquet writer.** `src/storage/parquet_writer.py` — buffer
- [x] **1.11 — Session keying.** `src/storage/parquet_writer.py` —
- [x] **1.12 — Ring buffer.** Background sweeper deletes session files
  — ⚠ **regressed/false-done:** age-only (no byte cap) and never wired
  into the production `run_daemon` path. Superseded by 8.1/8.2.
- [x] **1.13 — Disk-space monitor.** Poll free bytes on `storage_path`;
  — ⚠ **regressed/false-done:** `warn_if_storage_low` only logs; it never
  stops/rotates/deletes. Superseded by 8.3.
- [x] **1.14 — Crash-safety test.** `tests/test_parquet_writer.py` —

### 1D. Phase-1 milestone

- [x] **1.15 — End-to-end smoke run.** Run `main.py` against
- [~] **1.16 — Vehicle data collection.** Log 10+ diverse drives (cold
  start, idle, city, highway, off-road). Document each in
  `data/sessions.md` (date, conditions, notes). Inputs to Phase 2 training.
  Status: ~12 sessions captured (early parked/dev ones, the rev protocol
  2026-05-11T152858, the partial/tagged drives 2026-05-11T154648/211240,
  the 18-min drive 2026-05-11T222447, and the three 2026-05-13 ELM327
  rig-bring-up sessions: KOEO baseline, rev-with-OBD, NT510-active). Each
  documented under `data/analyses/<session>.md`; `data/sessions.md` is the
  consolidated index now. `docs/data-collection-plan.md` lays out the
  remaining captures (cold-start / idle baseline / city / highway /
  load-sweep / off-road / A/C / Mode-22 DIDs) and how much is enough to
  start the ML + behavior-model work. Parallel OBD (`record.py --obd-port`)
  is proven and the default `--obd-pids` set is the fuller ML/mapping one.
  Still gated on drive time and hardware validation: the legacy ELM327 adapter
  is dead, and the Canable-native streaming path is now the OBD-backed capture
  route that needs the next real drive validation.

---

### 1E. Operator capture tooling

Tooling for recording / tagging sessions outside the daemon path — used
for RE captures and for collecting Phase 2 training data before the
daemon is deployed on the vehicle.

- [x] **1.E.1 — `scripts/record.py`.** CLI wrapper around the CAN
- [x] **1.E.2 — `scripts/tag.py`.** Append-only tag CLI; writes one
- [x] **1.E.3 — `scripts/guided_session.py`.** Interactive protocol
- [x] **1.E.4 — `tools/log_to_parquet.py`.** Offline batch converter
- [x] **1.E.5 — `--timestamp` flag on `scripts/tag.py`.** Backfill
- [x] **1.E.6 — Canable-native OBD streaming collector + wiring.** Wrap
- [x] **1.E.7 — Canable-native Mode 22 DID reads.** Extend

---

## Phase 2 — Preprocessing & Isolation Forest (Overview Phase 2)

Goal: a reproducible feature pipeline + a working baseline detector.

- [x] **2.1 — Parquet session loader.** `src/preprocessing/loader.py` —
- [x] **2.2 — Resampler.** `src/preprocessing/resampler.py` — uniform time
- [x] **2.3 — Operating mode segmenter.** `src/preprocessing/segmenter.py`
- [x] **2.4 — Rolling features.** `src/preprocessing/features.py` —
- [x] **2.5 — Normalization.** Z-score scaler fit on the labeled clean
- [x] **2.6 — Synthetic anomaly injector.** Splice spikes, drift, dropouts,
- [x] **2.7 — Isolation Forest training.** `src/models/isolation_forest.py`
- [x] **2.8 — Synthetic anomaly evaluation.** Score injected sessions;
- [x] **2.9 — Unit tests.** Resampler edge cases (gaps, leading NaN),
- [x] **2.10 — Training CLI.** `scripts/train_anomaly_model.py` — the

---

## Phase 3 — LSTM Autoencoder (Overview Phase 3)

Goal: deep model that catches temporal anomalies the IF misses.

- [x] **3.1 — Sliding-window dataset.** `src/models/sequence_dataset.py`
- [x] **3.2 — Model.** `src/models/lstm_autoencoder.py` — encoder LSTM →
- [x] **3.3 — Training loop.** Train on clean sessions only; MAE loss;
- [x] **3.4 — Threshold calibration.** Compute per-channel and global
- [~] **3.5 — Real-time inference loop.** `src/models/inference.py` —
  consume a sliding window from the live preprocessing buffer, run a
  forward pass, emit anomaly score. Software path complete:
    * `LSTMSequenceBuffer` accepts model-ready feature rows, resets on
      session changes / missing features / non-finite values, emits a
      full `(T, F)` window only after `sequence_length` clean rows.
    * `LSTMInferenceEngine` requires calibrated thresholds, runs CPU
      inference by default, computes global and per-channel reconstruction
      errors, compares them to manifest thresholds, emits anomalous
      channels, anomaly flag, model version, window timestamps, latency.
    * Live feature source: `src/preprocessing/live.py:LiveFeatureSource`
      (Milestone B) is the row-by-row analog of `build_feature_frame`.
    * Daemon-side IF inference: `src/daemon.run_live_inference` drives
      `IsolationForestInferenceEngine` off the live source at the
      configured resample cadence.
    * Daemon-side LSTM inference: `src/daemon.run_live_inference_lstm`
      with `_lstm_observe` schema-alignment helper — NaN-fills any
      trained feature column the live source hasn't seen yet (the buffer
      resets on non-finite values during warmup, exactly the "wait for
      schema" behavior we want).
    * Both inference tasks are spawned by `run_daemon` alongside the
      collectors + writer + dashboard, all in one event loop. Records
      flow producer → `asyncio.Queue` → `drain_queue_with_broadcast`
      → Parquet + telemetry broadcaster + `live_source.observe`; per
      `resample_hz` tick the inference tasks pull a feature row and
      score → emit → log + anomalies broadcaster.
  Remaining work before marking done: run on the Pi 5 target and confirm
  the 1 Hz CPU budget is <50%. (FR-06, NFR-01)
- [x] **3.6 — False-positive benchmark.** `src/models/false_positive_benchmark.py`
- [x] **3.7 — Replay harness.** `scripts/replay.py` — feed a recorded

---

## Phase 4 — CAN Reverse Engineering & Signal Bindings

Runs in parallel with Phases 2–3. Bottlenecked on RE work, not software.

**Status note:** the project's primary signal-decode artifact is
`data/bindings.yaml` + the Pydantic-typed `Signal` model, NOT a DBC file.
This was a deliberate pivot — `bindings.yaml` carries confidence levels,
evidence trails, and plausibility bounds that a DBC can't express, which
matters more during active RE than DBC's external-tool interop does. DBC
emission stays viable as a one-way derived artifact (4.D.2 below).

### 4.A — Capture protocols & methodology

- [x] **4.1 — RE workflow doc.** `docs/can-re-workflow.md` — the rig
- [x] **4.A.1 — Physical-action capture protocol.** Codified as the

### 4.B — RE analysis tools (built)

- [x] **4.B.1 — ISO-TP / UDS decoder.** `src/analysis/iso_tp.py` +
- [x] **4.B.2 — Broadcast inventory.** `tools/inventory_broadcast.py` —
- [x] **4.B.3 — Byte-vs-tag differential.** `tools/broadcast_signals.py`
- [x] **4.B.4 — Mode 22 ↔ Mode 01 cross-reference.** `tools/cross_reference.py`
- [x] **4.B.5 — FSM signal extraction.** `tools/extract_fsm_signals.py`
- [x] **4.B.6 — FSM range-match scoring.** `src/analysis/range_match.py`

### 4.C — Bindings system (built)

- [x] **4.C.1 — Canonical bindings file.** `data/bindings.yaml` — typed,
- [x] **4.C.2 — Bindings decoder library.** `src/decode/bindings.py` —
- [x] **4.C.3 — Bindings validation reporter.** `tools/decode_bindings.py`

### 4.D — Future integration (not done)

- [x] **4.D.1 — Wire bindings decoder into preprocessing.** Decoded CAN
- [x] **4.D.2 — DBC emitter from bindings.yaml.** `tools/emit_dbc.py`
- [x] **4.D.3 — Bearing-wear validation.** `tests/test_bearing_wear_validation.py`
- [x] **4.D.4 — Bindings → captured-frame validation.** `tests/test_bindings_validation.py`

### 4.E — Behavior-model inference (not done)

Captures the engine's installed-state expected behavior — fuel surface,
ignition surface, VE proxy, coolant warmup curve, idle stability, etc. —
from runtime observation rather than from ECU flash. These models then
feed anomaly detection (deviation features) and the dashboard's
performance panel (task 5.12). Approach, scope guardrails, and rationale
documented in `docs/behavior-model-inference.md`. Strictly read-only —
no Mode 23 / Mode 27 / flash dumps / writes.

- [x] **4.E.1 — Behavior-model schema.** `src/decode/behavior_models.py`
- [x] **4.E.2 — 1D surface inferer.** `src/analysis/surface_1d.py` —
- [x] **4.E.3 — 2D surface inferer.** `src/analysis/surface_2d.py` —
- [~] **4.E.4 — Injector pulse-width DID identification.** Cross-reference
  captured Mode 22 DIDs against Mode 01 fuel-trim variations. The DID
  whose value correlates with `RPM × (1 + STFT/100)` is the injector PW.
  Adds to `data/bindings.yaml`. Tooling is fully in place:
    * DID sweep — `tools/scan_constant_dids.py` (4.E.5);
    * Mode 22 ↔ Mode 01 pairing — `tools/cross_reference.py` (4.B.4);
    * **Live DID polling** — `scripts/record.py --obd-source canable --obd-dids 1101,F190 [--obd-did-header 7E0]`
      now issues Mode 22 requests over the Canable and writes `DID_<hex>`
      readings into `pids.jsonl` exactly like Mode 01 readings. The legacy
      ELM327 path still supports `--obd-port --obd-dids` for emulator/fallback
      coverage, but it is no longer required.
  Covered by `tests/test_can_obd_client.py`, `tests/test_can_obd_collector.py`,
  `tests/test_obd_collector.py`, and `tests/test_record_script.py`.
- [x] **4.E.5 — Calibration-DID scanner.** `tools/scan_constant_dids.py`
- [x] **4.E.6 — Behavior-model decoder integration.** `src/decode/behavior_decoder.py`
- [x] **4.E.7 — Deviation features in preprocessing.** `append_deviation_columns(frame, models)`
- [x] **4.E.8 — Refit CLI.** `scripts/refit_behavior_models.py` +

  Recipes are typed dataclasses (`Surface1DRecipe`, `Surface2DRecipe`)
  carrying everything the fitter needs plus a `min_samples` floor and an
  optional `operating_mode` pre-filter. `fit_recipe` never raises: missing
  columns, too-few samples, empty-after-mode-filter, and any internal
  fitter exception all return a `RecipeResult(skipped=True, reason=...)`,
  logged + reported in the CLI summary — partial coverage beats crash.

  `refit_all(recipes, frame, existing=...)` does the versioning bookkeeping:
  successfully refitted models get their `version` bumped one above any
  prior entry of the same name; skipped recipes *inherit* the prior model
  unchanged (a refit that lost data doesn't lose previously-known
  surfaces); models in `existing` not in the current recipe set are
  dropped (catalog edit is how surfaces retire). Recipe order is preserved
  in the output for stable diffs.

  Default catalog: `coolant_warmup` (1D binned-lookup over RUN_TIME →
  COOLANT — warmup is sigmoid-ish, polys overshoot), `idle_rpm_vs_coolant`
  (1D binned-lookup, mode=idle), `timing_advance_vs_rpm` (1D poly2),
  `timing_advance_vs_rpm_load` (2D grid over RPM × ENGINE_LOAD), and
  `fuel_trim_vs_rpm_load` (2D grid → STFT B1). All use standard Mode 01
  PIDs — no Mode 22 DIDs required, so all of these are fittable today as
  soon as a drive with the expanded `--obd-pids` set is captured. Fuel /
  AFR / VE surfaces from `docs/behavior-model-inference.md` wait on
  injector-PW DID identification (4.E.4) before joining the catalog.
  2D recipes use quantile-based auto-edges (`auto_bin_count` knots) by
  default; pass `x_edges` / `y_edges` for an explicit grid (matching a
  published OEM map, e.g.).

  Covered by `tests/test_refit.py` (18 tests: recipe success paths, every
  skip path, versioning, carry-over, retirement, recipe ordering) and
  `tests/test_refit_behavior_models_script.py` (10 tests: session-age
  filter, recipe selection / validation, dry-run, write-out, version-bump
  on repeat). Smoke-tested against a converted-from-candump session to
  confirm the CLI prints actionable skip reasons against real data.
  (FR-09)
- [x] **4.E.9 — Performance panel data feed.** `GET /models/behavior`

---

## Phase 5 — Dashboard, Alerts & Deployment (Overview Phase 5)

### 5A. Alert system (FR-07)

- [x] **5.1 — Anomaly event schema.** `src/alerts/events.py` — `AnomalyEvent`
- [x] **5.2a — Inference → AnomalyEventLogger bridge.** `src/alerts/emitter.py` —
- [x] **5.2 — Event logger.** `src/alerts/event_logger.py` —

### 5B. FastAPI backend (FR-08)

- [x] **5.3 — REST endpoints.** `src/dashboard/api.py` — read-only
- [x] **5.4 — WebSocket stream.** `src/dashboard/broadcast.py` (in-memory
- [x] **5.5 — Bind-host entry point.** `scripts/serve_dashboard.py` is

### 5C. Dashboard frontend (FR-08)

- [x] **5.6 — Display strategy.** `docs/dashboard-display-strategy.md`
- [x] **5.7 — Stack: React + TypeScript + Vite.** Hand-scaffolded under
- [x] **5.8 — PWA + head-unit mirroring mode.** `public/manifest.webmanifest`
- [x] **5.9 — Live PID panel.** `panels/TelemetryPanel.tsx` +
- [x] **5.10 — Anomaly timeline.** `panels/AnomalyPanel.tsx` +
- [x] **5.11 — Operating-mode indicator.** `panels/OperatingModeIndicator.tsx`
- [x] **5.12 — Model performance panel.** `panels/ModelPanel.tsx` reads
- [x] **5.13 — TPMS panel (placeholder).** `panels/TpmsPanel.tsx` reads
- [x] **5.14 — Parked diagnostics panel (gate).** `panels/DiagnosticsPanel.tsx`
- [x] **5.14a — Model evidence event inspector.** Upgrade anomaly/model
  inspection from "score fired" to "why this fired": side-by-side cards for
  IF, LSTM, and behavior/physics residuals; score vs threshold/band; operating
  mode; model version; latency; top affected channels; and source agreement
  (`IF`, `LSTM`, `both`, `residual`). Use the existing LSTM
  `channel_scores` / `channel_thresholds` event detail first; add IF
  `feature_contributors` once 7.2a lands.
  Done: `panels/ModelEvidencePanel.tsx` renders selectable anomaly evidence
  from the Anomalies panel, including per-source detector cards, source
  agreement, LSTM channel ratios, residual placeholders, and future IF
  contributor support via event detail.
- [x] **5.14b — LSTM reconstruction visualizations.** Add a parked-only
  detailed LSTM view with top-channel reconstruction-error bars and a
  window-by-channel heatmap sorted by worst channel. When backend payloads
  include reconstruction traces, add actual-vs-reconstructed overlays for the
  top 2-3 channels in the alert window so an event can show exactly what the
  sequence model expected and where telemetry diverged.
  Done: the evidence inspector renders top-channel reconstruction bars, a
  channel heatmap from current `channel_scores`, richer heatmaps from optional
  reconstruction-series detail, and optional actual-vs-reconstructed overlays
  when event payloads provide reconstruction overlay arrays.
- [x] **5.14c — Models health/drift panel.** Replace the `models-health`
  placeholder with a real panel backed by `/status.health`: p95/max inference
  latency, missing-feature rate, data freshness, model swap/reject counts,
  feature drift vs training baseline, reconstruction-error drift, and host
  load. Keep the driving screen glanceable; this is a parked diagnostics view.
  Done: `panels/ModelHealthPanel.tsx` replaces the Workbench placeholder and
  reads `StatusResponse.health` best-effort for latency, freshness, feature
  drift, reconstruction drift, system load, memory, and swap outcomes.

Backend wire-up for the SPA: `create_app(..., static_dir=, enable_dev_cors=)`
mounts the built React bundle on `/` (404s cleanly until `pnpm build`
runs — API routes still work without the SPA) and optionally enables CORS
for `http://localhost:5173` so `pnpm dev` can talk to the backend
directly. `scripts/serve_dashboard.py` gets matching `--static-dir` and
`--dev-cors` flags. `tests/test_dashboard_api.py` covers static-mount
present / absent and the CORS dev-origin allow.

### 5D. Model management (FR-09)

- [x] **5.15 — Shared model manifest schema.** `src/models/manifest.py`
- [x] **5.16 — Hot swap (`ModelManager`).** `src/models/manager.py` —
- [x] **5.17 — Retraining CLI.** `scripts/retrain.py` orchestrates

### 5E. Deployment (NFR-02, NFR-04)

- [x] **5.18 — systemd unit.** `systemd/canary.service` runs the full
- [x] **5.19 — Install script.** `scripts/install_pi.sh` is the
- [x] **5.20 — Pi 5 image bring-up doc.** `docs/pi-setup.md` covers
- [x] **5.20a — Headless control plane.** The daemon now exposes
- [ ] **5.21 — Vehicle install.** Mount Pi, run buck converter from ACC,
  verify clean shutdown on ignition off. (Hardware task; software side
  is graceful-shutdown handling.)

---

## Phase 6 — Torque-Parity Control Plane, Nissan Diagnostics & ML Ops

Goal: turn the working Pi daemon and dashboard into a complete in-truck
operator surface. CANary must match the everyday OBD/dashboard/logging features
of Torque Pro while adding CANary-specific ML training, anomaly feedback, and
Nissan proprietary diagnostic capability. This phase directly closes the
sanity-check gaps: live public OBD telemetry, all openly available PIDs,
proprietary Nissan DIDs, dashboard trending, save-on-request training data,
real-time inference visibility, false-alarm control, parked diagnostic tests,
training/model-health visualization, and Torque Pro parity.

### 6A. Standard OBD and Torque-parity telemetry

- [x] **6.1 — Complete SAE J1979 registry.** `src/collection/obd_registry.py`
- [x] **6.2 — Adaptive real-time OBD polling scheduler.**
- [x] **6.3 — OBD capability and coverage endpoint.**
- [~] **6.4 — DTC, freeze-frame, monitor, and VIN workflow.** Decode/lookup/
  report core done: `src/decode/dtc_catalog.py` — `describe_dtc` (curated
  generic-powertrain table + a *structural* fallback so an unknown/
  manufacturer code is still explained: system, SAE-vs-OEM bit, subsystem
  group) and `build_dtc_report` (described, timestamped, JSON-exportable
  stored/pending/permanent snapshot with MIL + SAE dtc_count). Mode 02
  freeze-frame reuses the Mode 01 formulas via `obd_registry.lookup(0x02,
  pid)`; Mode 06 parsing is `parse_mode06_response`; VIN/cal/CVN decoders are
  in the 6.1 registry. The live Mode 03/07/0A/02/09 reads + `canaryctl`/
  dashboard flows that drive these (needs the truck) are the remaining piece.
  Covered by `tests/test_obd_coverage.py`. (FR-08)
- [ ] **6.5 — Parked-only clear-DTC action.** Implement optional Mode 04 behind
  the authenticated control plane, a parked interlock, explicit browser/CLI
  confirmation, audit logging, and an automatic post-clear rescan. This is a
  deliberate exception to CANary's read-only default because Torque parity
  requires it; it must never be available while driving.
- [x] **6.6 — Trip, fuel, and performance calculations.** `src/analysis/derived.py`
- [ ] **6.7 — GPS and route logging.** Support at least one GPS source for the
  Pi/truck workflow: browser Geolocation from the dashboard, USB GNSS through
  `gpsd`, or both. Store `source="gps"` telemetry rows, display route/speed on
  the dashboard, cross-check GPS speed against OBD speed, and export GPX/KML
  plus CSV/Parquet alongside the OBD/CAN data.
- [x] **6.8 — Custom PID/formula support.** `src/collection/custom_pids.py`

### 6B. Full-featured dashboard and remote control UI

- [~] **6.9 — Custom dashboard builder.** `CustomDashboardPanel`
  (Live▸Builder) — user-configurable gauge page: pick numeric channels →
  responsive big-readout tiles with a trend sparkline, editable title,
  per-browser localStorage persistence, JSON profile export/import. Drag-
  resize, dial/bar/LED widget variants, and maps are deferred (pointer-
  interaction UI is unverifiable in this env; the channel set + responsive
  grid is the durable core a richer builder layers onto). Covered by
  `tests/.../DidBuilder.test.ts`.
- [~] **6.10 — Advanced live charting.** `ChartsPanel` (Live▸Charts) +
  `MultiSeriesChart` — select up to 6 numeric channels, per-series
  normalized canvas trend, pause/resume (freezes a snapshot), time-window
  selector, min/max/avg + last value in the legend; charts any numeric
  channel uniformly (raw OBD / decoded CAN / derived — they're all ring
  buffers on the telemetry hook). Zoom, cursor readout, window export, and
  GPS series are deferred (mouse-interaction / GPS-source dependent).
  Covered by `tests/.../ChartsAlarms.test.ts`.
- [x] **6.11 — User alarms and dashboard alerts.** `AlarmsPanel`
- [~] **6.12 — HUD, themes, PWA offline, and reconnect behavior.** Done:
  the Drive context is the low-distraction glanceable view; `useTheme`
  day/night toggle (night = dimmer low-glare palette via a
  `[data-theme=night]` override of the Tailwind `@theme` tokens);
  `public/sw.js` service-worker shell caching (cache-first assets,
  network-only API/WS) + prod-guarded registration; the persistent
  StatusStrip is the per-stream stale/freshness banner; WebSocket
  reconnect is the existing `openReconnectingSocket`. Deferred: a true
  light theme (unverifiable here — honest dimming shipped instead),
  wake-lock, and a dedicated mirrored-HUD layout.
- [x] **6.13 — Control-plane frontend integration.** `ControlPanel`
- [x] **6.14 — Browser control authentication UX.** Token entry is a

### 6C. Nissan proprietary telemetry and diagnostics

- [ ] **6.15 — Live Nissan DID telemetry profile.** Make `data/dids.yaml` a
  first-class daemon input. The daemon must be able to enter UDS session
  `10 C0`, keep it alive, poll confirmed DIDs like `0x1208`, decode names and
  units through `src/decode/dids.py`, emit stable telemetry channels, and
  expose the active DID profile on the dashboard. Because `10 C0` is mutually
  exclusive with normal Mode 01 polling on the current ECM, add explicit
  profile switching or scheduled windows rather than silently mixing modes.
- [~] **6.16 — DID map dashboard/workbench.** Backend `GET /dids` serves
  the read-only `data/dids.yaml` catalog (+tests); `DidWorkbenchPanel`
  (Diagnostics▸Nissan DIDs) renders it — hex id, name, confidence
  (coloured), unit, scale/offset, plausible range, evidence notes. The
  parked-only live sweep launcher + candidate-DID capture is gated on the
  6.15 live DID profile (hardware) and documented in-panel as such rather
  than stubbed. Covered by `tests/.../DidBuilder.test.ts`.
- [ ] **6.17 — CONSULT/NT510 reverse-engineering workbench.** Add parked-only
  tools to record active NT510/CONSULT sessions, segment request/response
  exchanges, tag screen/actions, compare against `data/fsm/signals.json`, and
  generate candidate DID/control maps with evidence. This should produce data
  review artifacts, not immediately publish unreviewed commands.
- [ ] **6.18 — Safe diagnostic test executor.** Implement allowlisted,
  parked-only proprietary diagnostic tests and actuator commands with module,
  service, payload, expected response, preconditions, countdown, audit log,
  timeout, post-test restore, and post-test rescan. Do not expose an arbitrary
  raw CAN/UDS send endpoint in the dashboard or API.
- [ ] **6.19 — Proprietary protocol regression corpus.** Store known-good
  Nissan diagnostic exchanges and expected parses for ECM/ABS/BCM. Add replay
  tests so DID decoding, session handling, IO-control identification, and
  parked diagnostic command metadata cannot regress as the maps evolve.

### 6D. Data export, training operations, and anomaly tuning

- [x] **6.20 — Dataset capture and export pipeline.** `src/datasets/export.py`
- [x] **6.21 — Training run ledger and metrics artifacts.**
- [x] **6.22 — Training dashboard.** Backend `GET /control/training-runs`
- [x] **6.23 — Dataset health dashboard.**
- [x] **6.24 — False-positive feedback loop.** `src/alerts/feedback.py` —
- [x] **6.25 — Threshold tuning and alert suppression.** `src/alerts/tuning.py`
- [x] **6.26 — Label-aware retraining and online-update policy.** Retraining
  jobs should consume reviewed feedback labels, exclude known false-positive
  windows or fold them into clean baselines after review, and publish a new
  explicit model artifact/version. Do not continuously mutate live model
  weights without a reviewable artifact, manifest, and rollback path.
  Done: `scripts/retrain.py` consumes the feedback JSONL by default, keeps
  reviewed false positives as clean-baseline data, excludes true-positive /
  ignored / needs-review event windows from the model-ready frame, records the
  feedback policy summary in the training ledger, and correlates control jobs
  to explicit retrain artifacts via `--job-id`.
- [x] **6.27 — Model drift and inference health monitoring.**
- [x] **6.27a — Model comparison / detector-quality dashboard.** Surface
  training-run quality artifacts in the dashboard and companion library:
  nominal score distributions, injected-fault score distributions, false
  positives per hour, recall by synthetic fault type, old-vs-candidate model
  comparison, and publish-readiness verdict. Use the shared training ledger
  artifacts produced by IF/LSTM retrain and false-positive benchmark jobs.
  Done: `panels/TrainingPanel.tsx` now renders quality tiles, score
  distribution markers, k-sweep budget points, validation slices, and deltas
  against the prior run of the same model type from training-ledger metrics.

### 6E. Acceptance tests and parity proof

- [ ] **6.28 — Torque Pro parity acceptance suite.** Maintain a checklist and
  hardware/simulation tests comparing CANary against Torque Pro feature
  categories: live gauges, all supported public OBD PIDs, custom PIDs,
  graphing, logging, DTC/freeze-frame/clear, Mode 06/09 information, alarms,
  trip/performance calculations, GPS/map export, dashboard persistence, and
  mobile usability. Capture screenshots or short recordings for each accepted
  category.
- [ ] **6.29 — Pi-in-truck hardware-in-loop test matrix.** Add a repeatable
  validation run for the actual Pi 5 + Canable + Xterra: boot on ignition,
  service readiness, dashboard from phone, capture start/tag/stop from laptop,
  live OBD channel count, Nissan DID profile switch, real-time inference,
  export, retrain job, model publish, service restart, adapter unplug/replug,
  and clean shutdown.
- [x] **6.30 — Operator runbooks for advanced workflows.** `docs/runbooks.md`

### 6F. Standalone companion app and offline training workflow

- [x] **6.31 — Companion app architecture plan.** `docs/companion-app-plan.md`
- [x] **6.32 — Local CANary library format.** `src/companion/library.py` —
- [x] **6.33 — Companion local API.** Add a local FastAPI service that exposes
  session browsing, telemetry history, anomalies, datasets, training jobs,
  model artifacts, and sync state from the local library. It should mirror the
  Pi dashboard API shape where possible so the same React app and panels run in
  `Live Pi` and `Local Library` modes.
  Done: `src/companion/api.py` serves the local library with dashboard-shaped
  sessions, `/telemetry/latest`, `/anomalies`, dataset list/health, training
  jobs/runs, model artifacts, publish, queued actions, and sync-state endpoints.
- [ ] **6.34 — Pi session sync.** Build resumable sync from the Pi control API:
  session inventory, selected session/capture downloads, checksum validation,
  duplicate detection, pin-on-Pi, retry after interruption, and import into the
  local library.
- [ ] **6.35 — Offline dashboard/library mode.** Add a dashboard mode that runs
  without the Pi: session library search, historical telemetry charts, anomaly
  review, false-positive labeling, dataset builder, exports, training metrics,
  and model comparison. It must share the Pi dashboard's visual system,
  navigation, charts, gauges, channel naming, and terminology. The UI must
  clearly show `Live Pi`, `Local Library`, or `Queued Offline` state.
- [ ] **6.36 — Training target selector.** From the same training UI, let the
  user run IF, LSTM, combined retrain, and behavior-model refit jobs either on
  the laptop against synced sessions or on the Pi via `/control/jobs` when the
  Pi is reachable. Persist logs, metrics, manifests, plots, artifacts,
  false-positive benchmark output, compute target, and approval state in a
  shared training-run ledger.
- [ ] **6.37 — Model publish queue.** Package approved model artifacts with
  manifest, training-run record, checksums, and compatibility report; upload
  them to the Pi when reachable; confirm accepted/active status; and preserve
  local + Pi audit history. Failed uploads must be retryable.
- [ ] **6.38 — Companion packaging.** Start with a repo-runner
  `scripts/canary_companion.py` that starts the local API and serves/opens the
  built frontend. Later package as Tauri/Electron for Linux/macOS/Windows; keep
  phone/tablet as review/sync/publish surfaces unless mobile training proves
  practical.
- [ ] **6.39 — Offline workflow acceptance test.** Prove the full loop: collect
  on Pi, sync to laptop, power Pi off, browse/chart/train locally, produce an
  approved model bundle, power Pi on, publish the model, and confirm the daemon
  activates it.

---

## Phase 7 — Anomaly Architecture Hardening

Derived from the 2026-05-17 architecture review. The detector design
(per-mode Isolation Forest + LSTM autoencoder + behavior-model residuals,
offline-train / online-score, hot-swapped) is sound, but a chain of
incidents that day — live path missing `dbc:*` (`dbe3550`), `deviation:*`
silently all-NaN in both train and serve (`acd4318`), float32/float64
skew (`d500287`, `cc5b0ae`), full-corpus OOM on the Pi (`ffb5b5b`,
`cc5b0ae`) — exposed structural gaps: a train–serve feature contract
enforced only by hard-reject, no anomaly fusion/calibration, and no
feature/score validation. These tasks address the design, not the
individual bugs (already fixed). Picks up R3 and R4 from Risks below.

### 7A. Feature contract and validation

- [x] **7.1 — Unify feature extraction + versioned feature spec (P0).**
  `src/preprocessing/feature_pipeline.py:build_feature_frame` (offline)
  and `src/preprocessing/live.py:LiveFeatureSource` (online) are
  independent reimplementations that must emit byte-identical schemas,
  enforced only by `isolation_forest.py:_require_columns` hard-rejecting
  on mismatch — the root cause of nearly every 2026-05-17 fire. Extract
  one feature builder used by both (batch vs streaming param); persist a
  versioned feature spec in the model manifest (ordered columns, dtypes,
  decode sources/DBC ids, resample/window params, behavior-model set);
  replace hard-reject with schema reconciliation (align to spec, impute
  absent columns with a documented fill, log a one-time divergence
  report). Keystone — unblocks 7.2/7.3/7.5. (Effort: L)
  Started: model manifests now persist `feature_spec_version: 1`, ordered
  feature columns, dtypes, source prefixes, preprocessing knobs, sequence
  params, behavior residual columns, and a feature-health summary. Live scoring
  now reconciles rows to the persisted spec (drop extras, fill missing expected
  features with NaN, coerce numeric dtypes) before IF/LSTM inference and logs a
  one-time divergence report per model version. A fixture test now checks
  live/offline feature parity for OBD rows.
  Done: the schema-producing tail is now one implementation —
  `feature_pipeline.featurize_wide_frame(wide, config, *, behavior_models,
  feature_config) -> FeaturizedFrame` runs `segment_or_constant` →
  `append_deviation_columns` → `compute_features` and returns both the
  featured frame and the pre-feature `operating_frame`. `build_feature_frame`
  (offline batch, post read/decode/resample), `LiveFeatureSource.tick`
  (online streaming, post observe/snapshot — `latest_operating_point`
  semantics preserved via `operating_frame`), and the
  `train_anomaly_model.py` injected-fault eval all route through it, so the
  three previously-independent reimplementations of the column-deriving step
  are gone. The head asymmetry (batch read+resample vs streaming snapshot) is
  the only remaining by-necessity difference, bridged at serve time by the
  persisted feature spec + `reconcile_feature_frame_to_spec` (the already-done
  half). New `tests/test_feature_pipeline.py` locks the builder contract
  (rolling features + mode appended, `operating_frame` is pre-feature,
  `feature_config` override honored, deviation skipped without models); the
  existing live↔offline parity test stays green, now exercising the shared
  path. Keystone complete — 7.2/7.4/7.5 unblocked.

- [x] **7.3 — Feature/score validation at model-load and in CI (P1).**
  `deviation:*` was all-NaN for the model's whole existence, undetected,
  because nothing asserts features are populated. Add training-time
  assertions (no all-NaN/constant column passes silently; emit a
  feature-health summary into the manifest), a model-load gate that
  validates loaded-model feature health + schema-compat against the live
  spec and refuses-with-reason rather than scoring garbage, and a CI
  check on a fixture session (columns populated, schema stable vs a
  golden spec). Cheap; would have caught the deviation bug instantly.
  Depends on 7.1. (Effort: S–M)
  Done: IF/LSTM training emits feature-health metadata and rejects selected
  all-NaN columns before fitting; model-load and hot-swap now validate
  feature specs, score-shaping metadata, scaler/schema alignment, and LSTM
  threshold/channel compatibility before serving; CI covers a stable golden
  feature-spec fixture plus corrupt artifact cases.

### 7B. Detection quality

- [ ] **7.2 — Fusion + calibrated severity layer (P1).** IF and LSTM
  emit two independent streams deduped per-source, never combined, and
  IF's fixed `contamination=0.01` structurally flags ~1% of *nominal*
  driving. Add per-tick fusion of IF + LSTM + behavior residuals into one
  agreement-aware calibrated severity; replace fixed contamination with
  per-mode score thresholds calibrated on a held-out nominal set
  (target flag-rate, not assumed contamination); emitter dedupe/tuning
  operate on the fused event. Directly serves Done-Definition v1 #7
  (≤1 FP/hr). Depends on 7.1, 7.3. (Effort: M)

- [ ] **7.2a — Detector attribution payloads (P1).** Standardize the
  explanation/evidence data that anomaly events carry so the dashboard can
  display model "evidence" without reverse-engineering detector internals.
  For LSTM, emit compact top-channel reconstruction ratios and enough
  window-level reconstruction detail to support heatmaps/overlays without
  flooding the WebSocket. For IF, add cheap first-pass contributors by ranking
  feature distance from the model scaler/training baseline, then evaluate
  score-delta or SHAP-style attribution offline if the cheap ranking is not
  trustworthy. Fused events should preserve per-source evidence and source
  agreement. Depends on 7.1, 7.2. (Effort: M)

- [ ] **7.4 — Behavior/physics residuals as first-class detectors
  (P2).** The behavior models (coolant warmup, idle-RPM-vs-coolant,
  timing-vs-rpm/load, fuel-trim) are the most interpretable signal
  available but are demoted to NaN-prone ensemble *features*. Add a
  residual detector: per model, band-graded deviation → severity emitted
  through the fusion path with a human-readable physical cause, surfaced
  on the dashboard (expected vs actual + band). Depends on 7.2;
  `acd4318` (resolution fix) done. (Effort: M)

- [ ] **7.5 — LSTM hardening + synthetic-fault eval harness (P2).**
  LSTM uses a single global reconstruction threshold (not per-mode like
  IF; AEs tend to reconstruct anomalies too) and its training completion
  has been repeatedly unconfirmed. Make it per-mode or replace the
  reconstruction-AE with a next-step forecasting residual; build a
  synthetic-fault injection harness (stuck sensor, drift, dropout, scale
  error over nominal sessions) measuring detection vs nominal-FP rate per
  detector and fused; wire it into CI as a detector-quality regression
  guard and assert LSTM training completion. Resolves R3; makes
  Done-Definition v1 #4/#5 verifiable. Depends on 7.1, 7.2. (Effort: L)

Suggested sequence: 7.1 → (7.3 ∥ 7.2) → 7.4 → 7.5.

---

## Phase 8 — Storage Lifecycle & Disk Safety

Derived from the 2026-05-17 disk investigation; full design in
`docs/storage-lifecycle-fix.md`. The storage code has no total-size bound
and the retention that exists is never invoked by the production daemon:
`session_retention_days` is dead config, `delete_expired_sessions`
(`parquet_writer.py:199`) is called only from `run_collection`
(`main.py:204`, the non-production `--collection-only` path) and only once
in its shutdown `finally`; `run_daemon` — the `full_daemon=True` default —
never prunes. `warn_if_storage_low` (`monitor.py:27`) only logs. Left
running, the SD card fills → failed writes → the unclean state that already
corrupted the git repo. Directly threatens Done-Definition v1 #2
("continuously logged for ≥1 week without manual intervention"). Picks up
R13/R14 below; supersedes the regressed 1.12/1.13.

Invariant for every task here: a session dir with `.canary-pin`
(`canaryctl sessions pin`) and the **active** session dir are never
auto-deleted; because engine-run-time reset rotates the active dir
(`parquet_writer.py:_rotate_if_engine_run_time_reset`), the live path must
be read each tick, not snapshotted at startup.

- [x] **8.1 — Wire periodic retention into the production daemon (P0).**
  Add `run_retention_sweeper` to `src/daemon.py` modeled on
  `poll_model_managers` (same `stop_event`-timeout loop, same
  task-launch/cancel handling in `run_daemon`). Cadence is a new
  `daemon.retention_sweep_seconds` (default 3600). Each tick calls
  `delete_expired_sessions(storage_path, session_retention_days,
  protect={writer.paths.root})` — add the `protect` param to exclude the
  live dir (pinned dirs already excluded) — then logs what it reclaimed.
  Keep `run_collection`'s shutdown sweep as a final pass for parity, no
  longer the only sweep. Core fix. (Effort: S–M)
  Done: production `run_daemon` now starts a periodic age-retention sweeper
  with live `writer.paths.root` protection, structured reclaim logging, and
  config-driven cadence; collection-only shutdown cleanup uses the same
  active-session protection. Regression coverage proves the daemon wiring
  deletes an expired session while preserving the active one.

- [x] **8.2 — Byte-bounded storage cap / true ring buffer (P0).** Add
  `collection.storage_max_mb` and `enforce_storage_cap(sessions_root,
  max_mb, *, protect=...)` in `parquet_writer.py`: when total bytes under
  the root exceed the cap, delete oldest-first unpinned non-protected
  timestamp dirs until under cap or only pinned/active remain. Called from
  the 8.1 sweeper after the age sweep. This is the bounded ring buffer 1.12
  claimed but never implemented. Depends on 8.1. (Effort: S–M)
  Done: `enforce_storage_cap` measures only timestamp session dirs (scratch/
  `_archive` and other non-session entries are never counted or evicted),
  evicts oldest-first by dir name, and never removes a `.canary-pin` or
  `protect`-listed (live) dir — staying over cap is the correct outcome when
  nothing else is evictable. `run_retention_sweeper` runs it after the age
  sweep when `storage_max_mb` is set, reusing the same live-`writer.paths.root`
  protection and structured reclaim logging. `collection.storage_max_mb`
  (`int | None`, `None` = disabled — safe for pre-8.2 configs) is in the
  Pydantic model and all four `canary*.example.yaml` files. Covered by 7 new
  `enforce_storage_cap` unit tests (eviction order, pin/protect exclusion,
  stop-when-only-pinned, non-session ignore, non-positive-cap reject, missing
  root) plus a daemon sweeper byte-cap test. Remaining Phase-8 doc wording
  (`daemon-architecture.md:98`, `CANary-overview.md`) is 8.5.

- [x] **8.3 — Acting low-space guard (P1).** When the sweeper sees
  `free_space_mb(storage_path) < storage_warning_mb`, run an emergency
  oldest-first eviction (same pin/active rules, aggressive target) with
  structured before/after logging. `warn_if_storage_low` stays the cheap
  per-flush log so the hot drain path remains non-blocking and log-only;
  the acting logic lives only in the sweeper. Depends on 8.1, 8.2.
  (Effort: S)
  Done: `emergency_evict_for_free_space(sessions_root, warning_mb, *,
  protect=, free_space_mb=)` in `parquet_writer.py` deletes oldest-first
  unpinned/unprotected timestamp dirs, **re-probing free space after every
  delete** (it is a filesystem property), stopping when free space recovers
  to `warning_mb` or only pinned/active remain — aggressive relative to the
  byte cap (ignores `storage_max_mb`, will clear every evictable dir if the
  disk demands it). The free-space probe is injected (default
  `monitor.free_space_mb`) so it is unit-testable without faking the FS.
  `run_retention_sweeper` runs it after the byte cap when
  `storage_warning_mb` is set, gated on `free_space_mb < threshold`, with
  `log.warning` before/after free-MiB + reclaimed bytes; `run_daemon`
  threads `config.collection.storage_warning_mb`. `warn_if_storage_low`
  unchanged (still the log-only hot-path check). Covered by 7
  `emergency_evict_for_free_space` unit tests (no-op when healthy,
  oldest-first-until-recovered with a count-driven probe, pin/protect skip,
  non-session ignore, negative-warning reject, missing root).

- [x] **8.4 — Storage-lifecycle tests (P0/P1).** Unit: `enforce_storage_cap`
  eviction order, pin exclusion, active/protect exclusion, stop-when-only-
  pinned. Task: interval honored, age+cap both run, emergency path triggers
  below threshold, active dir survives an engine-run-time rotation.
  Regression: assert the assembled `run_daemon` task set includes the
  retention sweeper — the missing-wiring gap that let this ship. Depends on
  8.1–8.3. (Effort: S–M)
  Done (everything buildable pre-8.3): the unit suite landed with 8.2
  (`tests/test_parquet_writer.py` — eviction order, pin/protect exclusion,
  stop-when-only-pinned, non-session ignore, non-positive-cap, missing
  root). Added in `tests/test_daemon.py`: `interval honored` (no busy-loop),
  `age+cap both run in one tick`, `active dir survives an engine-run-time
  rotation` (live `active_session_path` read each tick), and the keystone
  regression `test_run_daemon_wires_retention_sweeper_task` — monkeypatches
  the sweeper and asserts `run_daemon` schedules it with the configured
  retention/cap/interval and the live `writer.paths.root` callable, closing
  the missing-wiring gap (R13). The emergency-path task test landed with
  8.3: `test_retention_sweeper_emergency_evicts_below_warning` drives the
  sweeper with an absurdly high `storage_warning_mb` (always above real
  free space) and age/byte-cap disabled, asserting the guard evicts the
  old dir while preserving pinned + active. The wiring regression spy was
  extended to also assert `storage_warning_mb` is threaded. 8.4 fully
  closed.

- [x] **8.5 — Config + doc reconciliation (P0).** Add `storage_max_mb`
  (under `collection:`) and `retention_sweep_seconds` (under `daemon:`) to
  `src/config.py` and **all** of `canary.example.yaml`,
  `canary.pi.example.yaml`, `canary.dev.example.yaml`,
  `canary.sim.example.yaml`, plus `docs/CANary-overview.md` (Configuration
  drift rule). Fix `docs/daemon-architecture.md:98`: retention is a
  periodic in-process sweep with a byte cap, not "sweeps on shutdown"
  (false on the production path). Depends on 8.1, 8.2. (Effort: S)
  Done: `retention_sweep_seconds` (8.1) and `storage_max_mb` (8.2) are
  wired through `src/config.py` and all four `canary*.example.yaml` files;
  `docs/daemon-architecture.md:98` now describes the periodic in-process
  age + byte-cap + low-space sweep (no longer "sweeps on shutdown");
  `docs/CANary-overview.md` FR-03 + the example config block document the
  dual age/byte bound and the low-space floor. No new config knob was
  needed for 8.3 (it reuses `storage_warning_mb`).
  Remaining: byte-cap wording in `docs/CANary-overview.md` (config-drift
  rule) and the `docs/daemon-architecture.md:98` "sweeps on shutdown" fix.

- [ ] **8.6 — Operator-intent / idle-gating for auto-started sessions
  (P2).** `run_daemon` opens a session and records whenever it runs;
  KOEO body/chassis CAN keeps the bus chattering so full-rate low-value
  data accumulates with no operator intent. Continuous logging is partly
  by design (Done-Definition v1 #2), so the fix is intent-gating, not
  disabling capture: an optional record-arming gate that defers a
  full-rate session until a "vehicle active" predicate (engine RPM > 0,
  vehicle speed, or configurable) is seen. Carries open questions
  (arm/disarm semantics, pre-arm buffer handling) — design + document,
  not necessarily all implemented in v1. Secondary; no truck action
  needed now. (Effort: M)

Suggested sequence: 8.1 → 8.2 → 8.4 → (8.3 ∥ 8.5) → 8.6.

---

## Phase 9 — TCN Sequence Detector (optional, LSTM-interchangeable)

Goal: add a Temporal Convolutional Network **autoencoder** as a selectable
alternative to the LSTM autoencoder in the *same* sequence-detector slot —
**not a third model**. CANary keeps two detector roles: the always-on
row-wise Isolation Forest, and one **sequence autoencoder** whose
implementation is operator/config-selectable (`lstm` or `tcn`). The LSTM
stays in-tree and fully supported; the TCN reuses the identical feature
contract, manifest/threshold/score schema, retraining ledger, alert/event
source vocabulary, dashboard surface, and hot-swap path so the two are
drop-in interchangeable. Motivation (stateless fixed windows — no
cross-mode hidden-state reset logic; ~3–5× faster training; clean Conv1D
export for a possible future NPU offload) and the broader compute/LLM
deployment rationale are captured in
`docs/llm-assist-and-compute-roadmap.md`. Depends on 7.1/7.3 for
feature-spec safety; for 7.2 fusion the TCN occupies the single
sequence-detector source — it does **not** add a fusion source or a
parallel alert stream.

- [ ] **9.1 — Sequence-AE contract + TCN shape (P1).** Lock a shared
  "sequence autoencoder" contract that both the existing LSTM-AE and the new
  TCN-AE satisfy: `model_type` slot semantics, artifact layout
  (`models/tcn/...`), manifest fields, threshold metrics, and score/evidence
  payload — identical shape to the LSTM-AE so picking a detector is a config
  switch, not a schema change. The first TCN shape is a windowed
  **reconstruction autoencoder** (same anomaly principle as the LSTM-AE)
  precisely for this interchangeability; a causal-forecasting variant is
  recorded as a deferred alternative form, not v1. Document how the sequence
  detector (LSTM-or-TCN) differs from IF (per-row boundary) so
  dashboard/event labels stay accurate regardless of which is active.
  (Effort: S)

- [ ] **9.2 — PyTorch TCN-AE module + sequence dataset reuse (P1).**
  Implement `src/models/tcn.py` with residual/dilated causal blocks,
  configurable channels/kernel/dropout/receptive field, deterministic CPU
  inference, and unit tests for I/O shapes, receptive-field padding,
  serialization, missing-Torch behavior, and finite loss. Reuse
  `src/models/sequence_dataset.py` as-is (same windowing as the LSTM-AE);
  add only what the reconstruction objective needs. Keep the module
  export-clean (standard Conv1D, no exotic ops) so the deferred NPU-offload
  contingency in the roadmap doc stays open without being built now.
  (Effort: M)

- [ ] **9.3 — TCN training, calibration, manifest (P1).** Add
  `scripts/train_tcn.py` and `src/models/tcn_training.py` mirroring the LSTM
  trainer's normal-only path: shared feature-frame builder, scaler
  compatibility, validation split, global + per-channel residual
  distributions, per-mode calibration when sample counts support it,
  feature-health metadata, and a `model.manifest.json` whose threshold/score
  fields are **schema-compatible with the LSTM-AE manifest** so the inference
  and dashboard layers need no per-implementation branching. Fail on
  all-NaN/constant selected features; record enough metrics for direct
  LSTM-vs-TCN comparison. (Effort: M–L)

- [ ] **9.4 — Retrain orchestration + ledger (P1).** Extend
  `scripts/retrain.py` to train IF + the *configured* sequence model
  (`--sequence-model {lstm,tcn}`, default from config; `--skip-sequence`),
  not IF+LSTM+TCN unconditionally. Shared cached feature frames,
  partial-success semantics, label-aware feedback filtering, ledger
  ingestion. Add control-plane job kinds (`retrain_tcn`; `retrain_all`
  trains IF + the selected sequence model) and companion-library actions so
  either sequence model trains locally or on the Pi via the existing
  workflow. (Effort: M)

- [ ] **9.5 — Live inference + selectable hot-swap (P1).** Add a
  `TCNInferenceEngine` that fills the **same sequence-detector slot** the
  LSTM engine fills — only one sequence engine active at a time, chosen by
  config/hot-swap. Reuse the LSTM sequence-buffer/reset semantics, emit one
  score frame per tick with the shared version/latency/threshold/top-residual
  payload, and degrade cleanly when Torch or the artifact is absent. Wire
  `ModelManager` selection through `build_components`, `run_daemon`, config
  examples, status snapshots, and `/ws/scores` so switching `lstm`↔`tcn`
  never disturbs IF and produces no second stream. (Effort: M–L)

- [ ] **9.6 — Dashboard, evidence, operator selection (P1/P2).** The TCN
  reuses the existing sequence-AE source vocabulary (same `AnomalyEvent`
  source, score normalization, suppression/tuning rules, evidence parsing,
  live-score panels, health/drift summaries, training-run comparison) —
  distinguished only by `model_version`: **no new source, no duplicate alert
  stream**. Add a model-management control to pick the active sequence
  detector (LSTM or TCN); the dashboard shows which is live; the LSTM is
  never removed. For 7.2 fusion, IF + sequence-detector is unchanged
  regardless of backing implementation. (Effort: M)

- [ ] **9.7 — Parity evaluation + Pi/FP proof (P1).** Before the live and
  retrain integration, run an offline spike: train the TCN-AE on recorded
  sessions and compare it head-to-head with the LSTM-AE on the same
  nominal/anomaly splits — per-fault recall, alert rate, detection latency,
  score correlation, drift, CPU, memory, artifact size. Gate: the TCN-AE
  must **match or beat the LSTM-AE at ≤1 FP/hr within the Pi budget** to
  become the recommended default; if it does not, it ships as a selectable
  alternative (disabled by default) and the LSTM stays default — it is not
  removed either way. If the NPU-offload contingency is later taken,
  re-validate this gate **after int8 quantization** (quantization perturbs
  reconstruction-error distributions and can move the FP rate — see the
  roadmap doc). (Effort: M)

- [ ] **9.8 — Config/docs/tests (P1).** Add the `sequence_detector:
  lstm | tcn` selector and both artifact paths to `src/config.py`, every
  `canary*.yaml` example, `docs/headless-api.md`, `docs/runbooks.md`,
  `docs/daemon-architecture.md`, README model summaries, and a pointer to
  `docs/llm-assist-and-compute-roadmap.md`. Tests: config parsing, selector
  round-trip, manifest validation, model-manager model-type rejection, swap
  with both/one/neither artifact present, retrain skip/partial-success,
  dashboard source typing, no-artifact/no-Torch startup. (Effort: S–M)

Suggested sequence: 9.1 → 9.2 → 9.3 → 9.7 offline spike → (9.4 ∥ 9.5) →
9.6 → 9.8. The 9.7 parity spike runs *before* the live/retrain integration
so a fail keeps the LSTM as default at low cost.

---

## Cross-Cutting Concerns

These get touched in multiple phases — keep them in mind, don't defer to the end.

- **Configuration drift.** Every new tunable goes through `canary.yaml` +
  the Pydantic model + `canary.example.yaml`. No magic constants in code.
  (NFR-04)
- **Time semantics.** All timestamps are UTC `datetime` with timezone;
  Parquet stores them as `timestamp[us, UTC]`. Decide once, enforce in
  the writer. Avoids the "is this local or UTC?" debugging tax later.
- **Schema evolution.** When a new PID or CAN signal is added, downstream
  pipelines must not crash on older Parquet files missing that column.
  Loader fills missing columns with NaN.
- **Test data fixtures.** Commit a small (~1 MB) anonymized Parquet
  session under `tests/fixtures/`; most preprocessing/model tests should
  use it instead of regenerating synthetic data each run.
- **CPU budget.** Profile after each phase; NFR-01 says <50% Pi 5 CPU
  steady-state. Sequence detectors (LSTM now, TCN in Phase 9) are the likely
  pinch point — measure before optimizing or enabling another live model.
- **OBD session scheduling.** Standard Mode 01 polling, Nissan `10 C0`
  DID polling, and parked diagnostic tests may not be simultaneously
  compatible. Treat the active diagnostic profile as explicit state and show
  it in the dashboard.
- **Safety boundary.** Public OBD reads stay safe by default. Clear-DTC and
  proprietary diagnostic/control actions must be allowlisted, parked-only,
  authenticated, audited, and recoverable.
- **Feedback lineage.** False-positive labels, suppressions, threshold changes,
  and retraining inputs are model data, not UI preferences. Preserve their
  provenance so a later model version can be explained or rolled back.

---

## Risks & Open Questions

| # | Risk / Question | Mitigation / When to resolve |
|---|---|---|
| R1 | Active Canable-native OBD polling and passive broadcast sniffing share one adapter/bus. | `can_obd_collector.py` owns one Bus and multiplexes both paths; keep request rates bounded and validate with long captures before deployment. |
| R2 | Nissan Consult III RE is open-ended; could slip Phase 4 indefinitely. | Phase 4 is parallel and additive — Phases 1–3 don't depend on it. Ship without it if needed. |
| R3 | LSTM may be too heavy for the Pi 5 at the target window length. | Benchmark in 3.5; fall back to smaller `latent_dim` or shorter window. CNN-LSTM is explicitly Phase 3+ in overview. |
| R4 | Per-mode IF models could overfit sparse modes (off-road, WOT). | Pool with global model when per-mode sample count is below a threshold. Decide in 2.8. |
| R5 | Dashboard auth deferred to v2 — anyone on the LAN can see live data. | Document in 5.5; revisit once a hotspot/AP design is chosen. |
| R6 | No remote access path for alerts when phone is off the vehicle's LAN. | Out of scope for v1; "optional push notification" in FR-07 is a v2 feature. |
| R7 | Public OBD coverage can look complete while unsupported/undecoded PIDs are silently hidden. | Phase 6.3 requires explicit capability/coverage reporting, including "supported but not decoded" and backed-off states. |
| R8 | Standard Mode 01 polling and Nissan `10 C0` DID polling are mutually exclusive on the current ECM. | Phase 6.15 makes diagnostic profile state explicit and prevents silent mode mixing. |
| R9 | Parked diagnostic tests and clear-DTC actions introduce write/control risk. | Phase 6.5 and 6.18 require allowlists, parked interlocks, authentication, confirmation, audit logs, and no arbitrary raw-send endpoint. |
| R10 | Torque Pro parity can grow without a finish line. | Phase 6.28 defines the parity checklist and evidence requirements. |
| R11 | False-positive tuning can accidentally hide real anomalies. | Phase 6.24–6.26 require feedback labels, versioned threshold/suppression changes, before/after estimates, and rollback. |
| R12 | Training data exports can become untraceable once copied off the Pi. | Phase 6.20 requires manifests with config hash, channel schema, sessions, tags, and quality warnings for every export. |
| R13 | Retention/disk bounding is unwired in the production daemon; the SD card fills unattended → failed writes → corrupt unclean state. | **Resolved (8.1–8.5).** `run_daemon` runs a periodic in-process sweeper: age + byte cap (`storage_max_mb`) + emergency low-space eviction, always protecting active/pinned dirs. The system is now self-correcting; 8.4's `test_run_daemon_wires_retention_sweeper_task` regression guards the wiring gap that originally let this ship. |
| R14 | Sessions auto-start with no operator intent; KOEO body/chassis CAN accumulates full-rate low-value data. | Phase 8.6 designs an optional record-arming gate without breaking Done-Definition v1 #2 continuous logging. Secondary to R13. |
| R15 | Adding TCN creates a third detector that can duplicate alerts and exceed the Pi CPU budget if treated as "just another neural model." | Phase 9 keeps TCN disabled until calibrated, benchmarked, and wired through the same feature spec, evidence, fusion, hot-swap, and performance gates as IF/LSTM. |

---

## Done Definition (v1)

v1 ships when, on the Pi 5 in the Xterra:

1. systemd-managed service starts on boot, reconnects on adapter drop.
2. OBD + CAN data continuously logged to Parquet for ≥1 week without
   manual intervention.
3. Canable auto OBD shows the supported public Mode 01 channel set on the
   dashboard, with no raw-CAN-only rows counted as telemetry channels.
4. Both Isolation Forest and LSTM Autoencoder are running inference live.
5. Synthetic anomaly injected via replay produces a dashboard alert
   within 5 s. (NFR-01)
6. Dashboard reachable from a phone on the vehicle's hotspot.
7. False-positive rate on a held-out clean drive ≤ 1 alert / hour.
8. Authenticated control API can start/stop/tag/export a capture from a
   laptop without SSH.

CAN-decoded signals (Phase 4) are a stretch goal for v1 and a hard
requirement for v1.1.

## Done Definition (v1.1 / Torque-Parity)

v1.1 ships when:

1. CANary exposes every supported public OBD PID/service it can safely read,
   and the dashboard reports unsupported, unsupported-by-vehicle, and
   supported-but-not-decoded cases explicitly.
2. The dashboard provides Torque-class live gauges, custom layouts, trend
   charts, alarms, DTC/freeze-frame/Mode 06/Mode 09 workflows, trip/fuel/
   performance calculations, GPS route logging, custom PIDs, and exports.
3. The browser dashboard can operate the full authenticated control plane:
   preflight, captures, tags, sessions, exports, jobs, model upload, and audit.
4. Training-data exports include reproducible manifests and can be used by the
   Isolation Forest/LSTM training jobs without manual cleanup.
5. Live inference health, training run history, dataset health, model metrics,
   false-positive labels, and threshold/suppression state are visible in the
   dashboard.
6. Nissan `10 C0` DID telemetry is available as an explicit live profile with
   decoded names/units from `data/dids.yaml`, and profile switching is visible.
7. Parked-only proprietary diagnostic tests and clear-DTC actions are
   allowlisted, authenticated, audited, and covered by regression tests.
8. The Phase 6.28 Torque Pro parity checklist passes on the Pi 5 + Canable +
   Xterra hardware path, not only in simulation.
