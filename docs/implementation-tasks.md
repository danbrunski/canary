# CANary — Implementation Plan

Derived from `docs/CANary-overview.md`. Tasks are grouped by phase, with FR/NFR
references in parentheses. Each task is sized to be roughly one PR or one
working session. Items inside a phase are listed in execution order; cross-phase
dependencies are called out explicitly.

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done.

---

## Phase 0 — Project Bootstrap

Goal: a runnable empty skeleton, reproducible dev env, CI green on first push.

- [x] **0.1 — Repo skeleton.** Create the directory layout from
  `CANary-overview.md` §"Project Structure" (`src/`, `tests/`, `data/dbc/`,
  `models/`, `systemd/`). Add `__init__.py` placeholders so packages import.
- [x] **0.2 — Python tooling.** `pyproject.toml` (PEP 621) targeting Python
  3.11+, with `ruff` (lint+format), `mypy` (strict on `src/`), `pytest`,
  `pytest-asyncio`. Lockfile deferred until deps stabilize. (NFR-04)
- [x] **0.3 — `requirements.txt` / extras.** Runtime deps (python-obd,
  python-can, cantools, pandas, pyarrow, scikit-learn, fastapi, uvicorn,
  pyyaml, pydantic) live in `[project.dependencies]`; `[project.optional-dependencies.dev]`
  carries tooling (ruff, mypy, pytest, pytest-asyncio, types-pyyaml,
  pandas-stubs). Torch deferred to a future `[ml]` extra (Phase 3).
  ELM327-emulator deferred to a future `[emulator]` extra (Phase 1.5) —
  its sdist needs `--no-build-isolation`, not worth solving until we
  actually wire the emulator test.
- [x] **0.4 — Config loader.** `src/config.py` — Pydantic models mirroring the
  `canary.yaml` schema in the overview; load+validate at startup; fail fast on
  unknown keys. Ship a checked-in `canary.example.yaml`. (NFR-04)
- [x] **0.5 — Logging.** Structured logging (stdlib `logging` + JSON formatter)
  with per-module loggers. Single entry point in `src/log.py`.
- [x] **0.6 — Entry point.** `src/main.py` parses `--config`, loads YAML,
  starts an asyncio event loop, wires module lifecycles (collectors → writer
  → inference → dashboard — wire-up TODO until Phase 1), and handles
  SIGTERM/SIGINT for clean shutdown.
- [x] **0.7 — CI.** GitHub Actions: lint, type-check, unit tests on
  `ubuntu-latest` with Python 3.11 and 3.12. No hardware-dependent jobs yet.
- [ ] **0.8 — Dev container.** `Dockerfile` + `docker-compose.yml` for
  non-Pi development; mounts repo, runs `main.py` in simulation mode.
  (NFR-04, NFR-05)

---

## Phase 1 — Data Collection (Overview Phase 1)

Goal: dual-channel telemetry into Parquet, validated against the emulator.

### 1A. OBD-II collector (FR-01)

- [x] **1.1 — PID registry.** `src/collection/pids.py` — declarative list of
  required PIDs (RPM, Speed, Engine Load, Coolant, IAT, MAF, Throttle, STFT/LTFT
  B1, O2 voltages 0x14–0x1B, Baro, Run Time) with units and `obd.commands`
  references. Single source of truth for downstream code.
- [ ] **1.2 — Async collector.** `src/collection/obd_collector.py` using
  `obd.Async`. Poll loop driven by `obd_poll_rate_hz`; emit
  `(timestamp, pid_name, value, unit)` records onto an `asyncio.Queue`.
  Log warning + skip on unsupported PIDs; keep running. (FR-01)
- [ ] **1.3 — Reconnect logic.** Detect adapter disconnect, retry with
  configurable interval (default 10 s), exponential backoff up to a cap.
  (NFR-02)
- [ ] **1.4 — Read-only enforcement.** Wrap the OBD interface so that only
  query commands are issuable; assert no `Mode 04`/write paths reachable.
  Unit-test that an attempted write raises. (NFR-06)
- [ ] **1.5 — Unit tests with emulator.** `tests/test_obd_collector.py` —
  spin up `ELM327-emulator`, run collector for N seconds, assert expected
  PIDs appeared and unsupported PIDs produced warnings, not crashes.
  (NFR-05)

### 1B. CAN collector (FR-02)

- [ ] **1.6 — SocketCAN bring-up doc.** `docs/can-setup.md` — `ip link set
  can0 up type can bitrate 500000 listen-only on` + udev rule. Reference,
  not code.
- [ ] **1.7 — Async CAN reader.** `src/collection/can_collector.py` —
  `python-can` `AsyncBufferedReader` on `can0` in listen-only; emit
  `(timestamp, arbitration_id, dlc, payload_bytes)` onto the same queue
  shape as OBD. (FR-02)
- [ ] **1.8 — DBC decode hook.** Optional `cantools` decoder; if a DBC path
  is configured, decode known frames into named-channel records and pass
  through unknown frames as raw. DBC is empty in Phase 1 — wire the hook,
  defer signal definitions to Phase 4. (FR-02)
- [ ] **1.9 — Synthetic CAN replay test.** Use `python-can`'s
  `VirtualBus` to inject canned frames; assert collector emits expected
  records. (NFR-05)

### 1C. Storage (FR-03, NFR-03)

- [ ] **1.10 — Parquet writer.** `src/storage/parquet_writer.py` — buffer
  records in memory, flush every N seconds or M rows to a Parquet file
  partitioned per ignition session. Schema: timestamp, channel, value,
  unit, source (`obd`|`can`). Use `pyarrow` writer with snappy compression.
- [ ] **1.11 — Session keying.** Detect new ignition by Engine Run Time
  reset (PID 0x1F decreases or starts near 0). New session ⇒ rotate file.
  (FR-03)
- [ ] **1.12 — Ring buffer.** Background sweeper deletes session files
  older than `session_retention_days`. Configurable, default 30. (FR-03)
- [ ] **1.13 — Disk-space monitor.** Poll free bytes on `storage_path`;
  emit warning event when below `storage_warning_mb`. Surface to
  dashboard once it exists. (NFR-03)
- [ ] **1.14 — Crash-safety test.** Kill the writer mid-flush; on restart,
  verify no corrupt files (Parquet footer present) and that data prior to
  the last flush is intact. (NFR-02)

### 1D. Phase-1 milestone

- [ ] **1.15 — End-to-end smoke run.** Run `main.py` against
  `ELM327-emulator` + `VirtualBus` for 5 minutes; verify Parquet files
  open in `pandas` and contain both OBD and CAN rows.
- [ ] **1.16 — Vehicle data collection.** Log 10+ diverse drives (cold
  start, idle, city, highway, off-road). Document each in
  `data/sessions.md` (date, conditions, notes). Inputs to Phase 2 training.

---

## Phase 2 — Preprocessing & Isolation Forest (Overview Phase 2)

Goal: a reproducible feature pipeline + a working baseline detector.

- [x] **2.1 — Parquet session loader.** `src/preprocessing/loader.py` —
  read a session (or list of sessions) into a long-format DataFrame; pivot
  to wide (one column per channel) on demand.
- [x] **2.2 — Resampler.** `src/preprocessing/resampler.py` — uniform time
  grid at `resample_hz` (default 1). Forward-fill slow sensors (coolant,
  baro). Gap-flag fast sensors (RPM, MAF) when gap exceeds 2× expected
  period. (FR-04)
- [x] **2.3 — Operating mode segmenter.** `src/preprocessing/segmenter.py`
  — rule-based classifier driven by the YAML thresholds (idle, city,
  highway, WOT, off-road). Returns a per-row mode label. User-extensible:
  unknown modes from config flow through without code changes. (FR-04)
- [ ] **2.4 — Rolling features.** `src/preprocessing/features.py` —
  windowed mean, std, and rate-of-change over `rolling_window_seconds`.
  Vectorized with pandas `.rolling()`. (FR-04)
- [ ] **2.5 — Normalization.** Z-score scaler fit on the labeled clean
  baseline only; persisted alongside the model in the manifest. Reject
  inference-time data whose channels don't match the fit schema. (FR-04,
  FR-09)
- [ ] **2.6 — Synthetic anomaly injector.** `tests/anomaly_injector.py` —
  utilities to splice spikes, drift, dropouts, and stuck-sensor patterns
  into a clean session. Used by 2.8 and Phase 3 evaluation. (NFR-05)
- [ ] **2.7 — Isolation Forest training.** `src/models/isolation_forest.py`
  — fit per-mode IF using `n_estimators` and `contamination` from config;
  serialize with `joblib`; emit a manifest (training data hash, date
  range, hyperparams, scaler params, schema). (FR-05, FR-09)
- [ ] **2.8 — Synthetic anomaly evaluation.** Score injected sessions;
  report per-mode precision/recall and the anomaly-score distribution.
  Tune `contamination` per mode based on results. (FR-05)
- [ ] **2.9 — Unit tests.** Resampler edge cases (gaps, leading NaN),
  segmenter boundary conditions, feature-window correctness on
  hand-crafted inputs. (NFR-05)

---

## Phase 3 — LSTM Autoencoder (Overview Phase 3)

Goal: deep model that catches temporal anomalies the IF misses.

- [ ] **3.1 — Sliding-window dataset.** `src/models/sequence_dataset.py`
  — PyTorch `Dataset` yielding `(B, T, F)` windows from Parquet sessions
  with stride 1. Filters out sequences that span session boundaries or
  contain gap flags. (FR-06)
- [ ] **3.2 — Model.** `src/models/lstm_autoencoder.py` — encoder LSTM →
  latent (`latent_dim`) → decoder LSTM. Configurable depth/hidden size.
- [ ] **3.3 — Training loop.** Train on clean sessions only; MAE loss;
  early stop on validation loss; checkpoint best weights; save manifest
  (FR-06, FR-09).
- [ ] **3.4 — Threshold calibration.** Compute per-channel and global
  reconstruction-error distributions on the training set; set threshold
  to `μ + k·σ` with configurable `k` (default 3). Persist in manifest.
  (FR-06)
- [ ] **3.5 — Real-time inference loop.** `src/models/inference.py` —
  consume a sliding window from the live preprocessing buffer, run a
  forward pass, emit anomaly score. Run on CPU; benchmark and confirm
  <50% Pi 5 CPU at 1 Hz. (FR-06, NFR-01)
- [ ] **3.6 — False-positive benchmark.** Held-out clean sessions: report
  FPR at the calibrated threshold; iterate on `k` if needed. (FR-06)
- [ ] **3.7 — Replay harness.** `scripts/replay.py` — feed a recorded
  Parquet session through preprocessing + inference end-to-end, no
  hardware needed. Used in CI. (NFR-05)

---

## Phase 4 — CAN Reverse Engineering Integration (Overview Phase 4)

Runs in parallel with Phases 2–3. Bottlenecked on RE work, not software.

- [ ] **4.1 — RE workflow doc.** `docs/can-re-workflow.md` — Z1 + Canable
  + SavvyCAN capture → diff against known-state changes → candidate
  signal IDs → DBC entry. Reference, not code.
- [ ] **4.2 — DBC scaffolding.** `data/dbc/xterra_2008.dbc` — start with
  ABS wheel speeds, transfer-case state, steering angle, IPDM relay
  states. Add as RE confirms each.
- [ ] **4.3 — DBC unit tests.** `tests/test_dbc.py` — load with
  `cantools`, decode known captured frames, assert decoded values match
  expected ranges. Parametrize over each new signal. (NFR-05)
- [ ] **4.4 — Wire decoded channels into preprocessing.** Once a signal
  lands in the DBC, it should appear in the wide-format DataFrame
  automatically via the registry — no per-signal code changes. Verify
  with replay.
- [ ] **4.5 — Bearing-wear validation.** Score a session with known
  uneven wheel-speed behavior (synthetic if no real example exists);
  confirm anomaly fires before a DTC would. (Overview Phase 4 goal)

---

## Phase 5 — Dashboard, Alerts & Deployment (Overview Phase 5)

### 5A. Alert system (FR-07)

- [ ] **5.1 — Anomaly event schema.** `src/alerts/events.py` — Pydantic
  model with timestamp, source (`isolation_forest`|`lstm`),
  affected_channels, anomaly_score, operating_mode, model_version.
- [ ] **5.2 — Event logger.** `src/alerts/event_logger.py` — append-only
  JSONL file at `storage_path/anomalies.jsonl`. Rotated daily.

### 5B. FastAPI backend (FR-08)

- [ ] **5.3 — REST endpoints.** `GET /telemetry/latest`,
  `GET /anomalies?since=...`, `GET /status` (health, storage, model
  version), `GET /config` (read-only echo).
- [ ] **5.4 — WebSocket stream.** `/ws/telemetry` pushes live PID values;
  `/ws/anomalies` pushes anomaly events as they fire.
- [ ] **5.5 — Bind to LAN only by default.** Honor `host`/`port` from
  config; default `0.0.0.0:8000` but document that the Pi should be
  firewalled / hotspot-only. No auth in v1; document that. (NFR-06)

### 5C. Dashboard frontend (FR-08)

- [ ] **5.6 — Stack decision.** Pick React + Vite (or Svelte if simpler);
  document choice in `src/dashboard/static/README.md`. Single-page app,
  built artifacts checked in or built at deploy.
- [ ] **5.7 — Live PID panel.** Subscribe to `/ws/telemetry`, render a
  grid of current values + sparklines for last 60 s.
- [ ] **5.8 — Anomaly timeline.** Anomaly score chart; recent-events list
  with filter by source and mode.
- [ ] **5.9 — Operating-mode indicator.** Current mode badge + recent
  transitions.

### 5D. Model management (FR-09)

- [ ] **5.10 — Model manifest format.** YAML alongside each artifact:
  version, training data hash + date range, hyperparams, scaler params,
  threshold, schema. (FR-09)
- [ ] **5.11 — Hot swap.** `ModelManager` watches the models directory;
  on a new manifest, validate schema match and atomically swap the
  in-memory model without restarting the process. (FR-09)
- [ ] **5.12 — Retraining CLI.** `scripts/retrain.py` — pulls clean
  sessions from the last N days, retrains both models, writes new
  artifacts + manifests. Manual trigger in v1; cron later. (FR-09)

### 5E. Deployment (NFR-02, NFR-04)

- [ ] **5.13 — systemd unit.** `systemd/canary.service` — Type=notify,
  Restart=on-failure, runs as a non-root `canary` user, reads config
  from `/etc/canary/canary.yaml`, writes to `/data/canary`.
- [ ] **5.14 — Install script.** `scripts/install.sh` — creates user,
  installs venv, copies unit file, enables service. Idempotent.
- [ ] **5.15 — Pi 5 image bring-up doc.** `docs/pi-setup.md` — Bookworm
  install, SocketCAN modules, USB device permissions, fstab for the SSD.
- [ ] **5.16 — Vehicle install.** Mount Pi, run buck converter from ACC,
  verify clean shutdown on ignition off. (Hardware task; software side
  is graceful-shutdown handling.)

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
  steady-state. LSTM inference is the likely pinch point — measure
  before optimizing.

---

## Risks & Open Questions

| # | Risk / Question | Mitigation / When to resolve |
|---|---|---|
| R1 | ELM327 + Canable on the same OBD-II port may conflict if Canable isn't truly listen-only. | Validate listen-only with `candump` showing no TX before connecting ELM327. Phase 1. |
| R2 | Nissan Consult III RE is open-ended; could slip Phase 4 indefinitely. | Phase 4 is parallel and additive — Phases 1–3 don't depend on it. Ship without it if needed. |
| R3 | LSTM may be too heavy for the Pi 5 at the target window length. | Benchmark in 3.5; fall back to smaller `latent_dim` or shorter window. CNN-LSTM is explicitly Phase 3+ in overview. |
| R4 | Per-mode IF models could overfit sparse modes (off-road, WOT). | Pool with global model when per-mode sample count is below a threshold. Decide in 2.8. |
| R5 | Dashboard auth deferred to v2 — anyone on the LAN can see live data. | Document in 5.5; revisit once a hotspot/AP design is chosen. |
| R6 | No remote access path for alerts when phone is off the vehicle's LAN. | Out of scope for v1; "optional push notification" in FR-07 is a v2 feature. |

---

## Done Definition (v1)

v1 ships when, on the Pi 5 in the Xterra:

1. systemd-managed service starts on boot, reconnects on adapter drop.
2. OBD + CAN data continuously logged to Parquet for ≥1 week without
   manual intervention.
3. Both Isolation Forest and LSTM Autoencoder are running inference live.
4. Synthetic anomaly injected via replay produces a dashboard alert
   within 5 s. (NFR-01)
5. Dashboard reachable from a phone on the vehicle's hotspot.
6. False-positive rate on a held-out clean drive ≤ 1 alert / hour.

CAN-decoded signals (Phase 4) are a stretch goal for v1 and a hard
requirement for v1.1.
