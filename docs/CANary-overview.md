# CANary

> Early anomaly detection for vehicle telemetry via OBD-II and CAN bus monitoring.

---

## Overview

CANary is an on-vehicle software system that continuously reads telemetry from
a 2008 Nissan Xterra (and broadly any OBD-II compliant vehicle) via raw CAN bus
capture and standardized OBD-II diagnostics. The hardware interface is a
single SocketCAN device (`can0`) used for both broadcast CAN capture and
active ISO-TP OBD/UDS requests. Two SocketCAN adapters are supported
first-class: the CANary-HAT (MCP2518FD/SPI, the current in-vehicle rig — see
`hardware/canary-hat-rev2.0/`, with rev 2.1 in `hardware/canary-hat-rev2.1/`), the Waveshare 2-CH CAN HAT (MCP2515/SPI) and the
Canable USB (gs_usb/candleLight). The original ELM327 path
remains in the repo as a legacy fallback.

The system runs headlessly on a Raspberry Pi 5 mounted in-vehicle, logs all telemetry to local storage, and surfaces anomaly alerts via a local network dashboard or notification mechanism.

The name references both the CAN bus and the "canary in a coal mine" idiom — detecting subtle signs of trouble before they become failures.

---

## Goals

- **Learn** what normal looks like for this specific vehicle across all operating modes (cold start, idle, city, highway, towing, off-road)
- **Detect** anomalous sensor conditions early — before a DTC is thrown or a failure occurs
- **Log** all telemetry for historical analysis and model retraining
- **Extend** to proprietary Nissan Consult III signals once CAN reverse engineering is complete

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Data Ingestion Layer                    │
│                                                             │
│  Canable (USB / SocketCAN)                                  │
│  python-can broadcast capture + ISO-TP OBD requests         │
│  Raw CAN frames @ 100-500Hz; OBD Mode 01/03/09 on demand   │
│                              │                              │
│                              ▼                              │
│                   ▼                                         │
│         Timestamped Parquet ring buffer                     │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│                  Preprocessing Pipeline                      │
│  • Resample all signals to uniform time grid (1 Hz default) │
│  • Forward-fill slow sensors; gap-flag fast sensors         │
│  • Segment by operating mode (idle / city / highway / WOT)  │
│  • Rolling window features: mean, std, rate-of-change       │
│  • Z-score normalization (fit on clean baseline only)       │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│                    Model Layer                               │
│                                                             │
│  Phase 1: Isolation Forest (scikit-learn)                   │
│  Phase 2: LSTM Autoencoder (PyTorch)                        │
│  Phase 3: CNN-LSTM Hybrid (future)                          │
│                                                             │
│  Anomaly score → threshold → alert event                    │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│                  Alert & Dashboard Layer                     │
│  FastAPI local endpoint / WebSocket                         │
│  Web dashboard (React or similar)                           │
│  Optional: push notification on anomaly                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Requirements

### Functional Requirements

#### FR-01 — OBD-II Data Collection
- The system SHALL collect standardized OBD-II data over the Canable-native
  ISO-TP client when running on current hardware
- The legacy ELM327 / `python-obd` path MAY be used as a fallback for
  development and emulator testing
- The system SHALL poll the following PIDs at a minimum: RPM, Vehicle Speed, Engine Load, Coolant Temp, Intake Air Temp, MAF Air Flow Rate, Throttle Position, Short-Term Fuel Trim Bank 1, Long-Term Fuel Trim Bank 1, O2 Sensor Voltages, Barometric Pressure, Engine Run Time
- The system SHALL use non-blocking/supervised polling so OBD request-response
  traffic does not block CAN capture or inference
- The system SHALL log a warning and continue operation if a PID is unsupported by the vehicle ECU

#### FR-02 — Raw CAN Bus Collection
- The system SHALL connect to a Canable USB adapter via SocketCAN (`can0`)
- The system SHALL capture broadcast CAN frames continuously; the broadcast
  capture path itself is a sniffer and never transmits
- The system SHALL capture all CAN frame IDs present on the bus at full bus rate
- The system SHALL apply `data/bindings.yaml` to decode proprietary Nissan
  signals into named channels; generated DBC export is optional interop

#### FR-03 — Data Storage
- The system SHALL write all collected telemetry to timestamped Parquet files on local storage (microSD or USB SSD)
- The system SHALL implement a rolling ring buffer bounded by **both** age (`session_retention_days`, default 30) **and** total bytes (`collection.storage_max_mb`), plus an emergency low-space eviction when filesystem free space falls below `collection.storage_warning_mb`; all three run in the periodic in-process `run_retention_sweeper` and never delete the active or a pinned session
- The system SHALL write a new file per ignition session (keyed on engine run time reset)

#### FR-04 — Preprocessing
- The system SHALL resample all signals to a configurable uniform time grid (default: 1 Hz)
- The system SHALL segment data by operating mode: Idle, City, Highway, Hard Acceleration, Off-Road (user-extensible)
- The system SHALL compute rolling statistical features (mean, std, rate-of-change) over configurable windows
- The system SHALL fit normalization parameters exclusively on clean baseline data; these parameters SHALL be versioned alongside the model

#### FR-05 — Anomaly Detection: Isolation Forest
- The system SHALL implement an Isolation Forest baseline model (scikit-learn) trained on clean session data
- The system SHALL produce a per-sample anomaly score and binary label (-1 anomaly / 1 normal)
- The system SHALL support per-operating-mode models to reduce false positives from context-dependent signal ranges

#### FR-06 — Anomaly Detection: LSTM Autoencoder
- The system SHALL implement an LSTM Autoencoder (PyTorch) trained exclusively on normal-drive data
- The system SHALL use a sliding window of configurable length (default: 30 seconds) as the inference input
- The system SHALL compute mean absolute error (MAE) between input and reconstruction as the anomaly score
- The system SHALL flag an anomaly when MAE exceeds a threshold of μ + k·σ of the training loss distribution (default k=3, configurable)

#### FR-07 — Alert System
- The system SHALL emit a structured anomaly event when any active model exceeds its detection threshold
- Each anomaly event SHALL include: timestamp, affected PID(s) / CAN ID(s), model used, anomaly score, operating mode at time of detection
- The system SHALL persist all anomaly events to a separate log file for post-hoc analysis

> **Detector roles — decided by the operator, 2026-09-23 (A06).** Each
> learned detector has a configured role (`models.roles`: `isolation_forest`,
> `sequence`), either `alert` or `evidence`. An `evidence`-role detector still
> scores and stores its events, flagged `role: evidence`, and still counts
> toward fusion agreement. It never sends an ntfy push, never shows as an
> active anomaly on the dashboard, and cannot be the reason a fused event
> fires: that needs an `alert`-role source among the agreeing voices. The
> `agreement >= 2` gate is unchanged. Both learned detectors default to
> `evidence` ([A06](roadmap/tasks/archive/A06-detector-roles.md)), on A02.11's
> held-out table (`data/analyses/2026-09-23-a02-detector-baseline.md`: 14
> drives in the artifacts' era, false alarms per engine-hour against a budget
> of 1, 95 % drive-bootstrap intervals):
>
> - **Isolation Forest — evidence only (A06.1).** 1.18 [0.00, 3.94] false
>   alarms per engine-hour with 81 % of rows withheld, and blind to a
>   single-quantity shift of any size (A04.1). A05's deterministic plausibility
>   checks own the gross single-channel faults it was meant to catch.
> - **Sequence detector — a non-alerting novelty screen (A06.2).** As served
>   it raised 45.35 [24.51, 61.28] false alarms per engine-hour; its global
>   score alone, 2.96 [1.66, 5.04]. On one vehicle, "unusual" has mostly meant
>   "outside the training envelope", so that is what it now reports: each
>   event's `detail.envelope` and `detail.novelty_summary` say whether the
>   window was outside the artifact's recorded training envelope (A03.8:
>   per-mode baro, IAT and speed ranges) and on which quantity, then name the
>   largest deviations. `/status` `detectors.inference.sequence_envelope`
>   counts scored windows outside the envelope by mode and quantity, the
>   corpus-coverage meter that will show where the post-supercharger normal
>   lacks data. An artifact without an envelope reports it as not recorded,
>   never as inside.
> - **Primary detector (A06.4):** the behavior models and the second brain's
>   deterministic layer, with A05's plausibility checks for gross faults.
> - **The retrain (A06.3).** The naturally aspirated retrain on the current
>   calibration (`data/analyses/2026-09-23-a06-3-retrain-evaluation.md`, one
>   held-out drive, 1.06 engine-hours) quieted the sequence global score (0
>   episodes against the deployed model's 29 on live sessions), but its
>   per-channel path still raised 54.9 [41.7, 70.9] false alarms per
>   engine-hour (Poisson). It stays evidence.
> - **The alert budget (A06.5)** is a refusal, not a report: `scripts/retrain.py`
>   trains an `alert`-role artifact into a `_staging-*` directory and publishes
>   it only if the upper bound of its false alarms per engine-hour on the
>   evaluation drives is at most 1 (exit 4 and a `_refused-alert-budget-*`
>   directory otherwise; `--accept-alert-budget "<reason>"` overrides the
>   refusal). Evidence roles are never gated. The daemon enforces it too
>   (operator decision 2026-09-23): an `alert` role takes effect only for an
>   artifact with a passing verdict for its sha256 beside it
>   (`<artifact>.alert-budget.json`, written by retrain, travels with the
>   binary). Otherwise it serves as evidence, and `/status`
>   `health.detector_roles` shows the configured and effective role and why.
>   A config flip to `alert` is gated the same way.
>
> The FR-05/FR-06 text above describes what the detectors compute, not whether
> they alert.

#### FR-08 — Dashboard
- The system SHALL expose a local HTTP/WebSocket API (FastAPI) serving real-time telemetry and anomaly events
- The system SHALL provide a web-based dashboard accessible over the vehicle's local network (hotspot or onboard AP)
- The dashboard SHALL display: live PID values, anomaly score over time, recent anomaly events, operating mode

#### FR-09 — Model Management
- The system SHALL support loading, versioning, and swapping of trained model artifacts without a full restart
- The system SHALL support scheduled or manual retraining from accumulated clean session data
- The system SHALL log model version, training data date range, and detection threshold in a model manifest file

---

### Non-Functional Requirements

#### NFR-01 — Performance
- End-to-end latency from sensor sample to anomaly alert SHALL not exceed 5 seconds at default 1 Hz sampling
- The inference pipeline SHALL consume no more than 50% of available Pi 5 CPU under steady-state operation, leaving headroom for data logging and dashboard

#### NFR-02 — Reliability
- The system SHALL start automatically on boot (systemd service)
- The system SHALL resume data collection after a crash or unexpected shutdown without data loss beyond the current write buffer
- The system SHALL handle adapter connection drops with automatic reconnect
  (configurable retry interval, default 10 seconds)

#### NFR-03 — Storage
- All raw telemetry SHALL be stored in Apache Parquet format for efficient columnar access
- The system SHALL alert (log warning + dashboard notification) when available storage drops below a configurable threshold (default 500 MB)

#### NFR-04 — Portability
- The system SHALL be deployable on any Raspberry Pi 5 (arm64, Raspberry Pi OS Bookworm or later)
- The system SHALL be fully configurable via a single YAML configuration file with no hardcoded vehicle- or hardware-specific values
- The system SHALL be containerizable (Docker/Podman) for development and testing on non-Pi hardware

#### NFR-05 — Testability
- The system SHALL support simulation/replay using virtual CAN buses,
  recorded Parquet sessions, and the legacy `ELM327-emulator` integration tests
- The system SHALL support replay of recorded Parquet sessions through the preprocessing and inference pipeline for offline testing
- All core modules SHALL have unit test coverage for preprocessing transforms, feature engineering, and threshold logic

#### NFR-06 — Security
- The system MAY send any diagnostic service required to read vehicle data —
  telemetry polling, session control, security access, and memory reads are all
  in scope. Transmitting on the bus is inherent to diagnostics, not an exception
- The system SHALL NOT write the ECU's non-volatile memory (reflash,
  calibration/ROM write) without the operator's explicit permission for that
  operation. Permission SHALL NOT be inferred from a broader instruction or
  carried from one write to the next
- The system SHALL refuse a non-volatile write unless the vehicle is
  stationary with the engine off (`vehicle_state == ignition_only`). It SHALL
  refuse under `engine_running` and under any state it cannot classify
- Reads of any depth and volatile, reversible writes (clear DTC, actuation
  tests, session control) need neither condition. Replaces the earlier
  blanket prohibition (operator decision 2026-09-21); bench groundwork is in
  `docs/skunkworks/ecu-rw-bench-research.md`
- The dashboard SHALL be accessible on localhost / LAN only; no external internet exposure by default

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| OBD-II interface | `python-can` ISO-TP client over Canable; legacy `python-obd` fallback |
| CAN bus interface | `python-can` + SocketCAN (`can0`) |
| Data storage | Apache Parquet via `pandas` / `pyarrow` |
| ML baseline | `scikit-learn` (IsolationForest, OneClassSVM) |
| ML deep learning | `PyTorch` (LSTM Autoencoder) |
| API / dashboard backend | `FastAPI` + `uvicorn` |
| Feature engineering | `pandas`, `tsfresh` (optional) |
| Configuration | YAML via `pyyaml` |
| Testing | `pytest`, virtual CAN buses, `ELM327-emulator` for legacy path |
| Deployment | `systemd` service, optional Docker |
| Hardware | Raspberry Pi 5 (8 GB); SocketCAN adapter — CANary-HAT (MCP2518FD/SPI, current), Waveshare 2-CH CAN HAT (MCP2515/SPI) or Canable USB (gs_usb); ELM327 optional legacy fallback |

---

## Hardware Setup

```
Vehicle OBD-II Port (J1962)
         │
SocketCAN adapter (can0)
CANary-HAT (MCP2518FD/SPI, current),
Waveshare 2-CH CAN HAT (MCP2515/SPI)
or Canable USB (gs_usb)
(broadcast CAN + OBD ISO-TP)
         │
    Raspberry Pi 5 (8 GB)
    - Raspberry Pi OS Bookworm (arm64)
    - USB SSD or high-endurance microSD
    - 12V→5V buck converter from ACC circuit
```

---

## Data Channels

### OBD-II PIDs (via Canable-native ISO-TP; legacy ELM327 fallback)

| PID | Name | Unit |
|-----|------|------|
| 0x0C | Engine RPM | rpm |
| 0x0D | Vehicle Speed | km/h |
| 0x04 | Engine Load | % |
| 0x05 | Coolant Temperature | °C |
| 0x0F | Intake Air Temperature | °C |
| 0x10 | MAF Air Flow Rate | g/s |
| 0x11 | Throttle Position | % |
| 0x06 | Short-Term Fuel Trim B1 | % |
| 0x07 | Long-Term Fuel Trim B1 | % |
| 0x14–0x1B | O2 Sensor Voltages | V |
| 0x33 | Barometric Pressure | kPa |
| 0x1F | Engine Run Time | s |

### CAN Bus Channels (via Canable + `bindings.yaml`)

- ABS-published aggregate vehicle-speed variants; individual FL/FR/RL/RR
  speeds are visible on the Z1 scan tool but are not yet decoded from CANary
  captures (the Z1 either uses the separate pin-7 DDL2 K-line diagnostic path
  or decodes an as-yet-unidentified passive CAN encoding)
- Transfer case engagement state (2H / 4H / 4Lo)
- Steering angle sensor
- IPDM relay states
- BCM door/switch events
- Nissan Consult III proprietary module data (decoded post-RE)

---

## Development Phases

All five original phases have shipped; the system is deployed and live
in-vehicle. `docs/implementation-tasks.md` is the maintained task index;
`docs/roadmap/` holds the open plans. Historical checklist:

### Phase 1 — Data Collection
- [x] Implement legacy `python-obd` + ELM327 path and emulator tests
- [x] Move OBD collection onto the SocketCAN-native client
- [x] Install and verify `python-can` + SocketCAN adapter on Pi 5
- [x] Implement dual-channel async data collection with Parquet logging
- [x] Log 10+ drive sessions — diversity gaps (cold start, load sweep,
  off-road) still open; see `docs/roadmap/archive/data-collection-plan.md`
- [x] Validate offline using `ELM327-emulator`

### Phase 2 — Preprocessing & Baseline Model
- [x] Build resampling and normalization pipeline
- [x] Implement operating mode segmentation
- [x] Train Isolation Forest on clean sessions
- [x] Validate with synthetic anomaly injection
- [x] Tune `contamination` parameter per operating mode

### Phase 3 — LSTM Autoencoder
- [x] Implement sliding window dataset loader
- [x] Build and train LSTM Autoencoder in PyTorch (GRU-D masking-aware;
  a TCN backbone is also available via `models.sequence_detector`)
- [x] Implement real-time sliding window inference loop
- [x] Calibrate detection threshold (held-out, per-mode)
- [x] Benchmark false positive rate on held-out normal sessions

### Phase 4 — CAN Reverse Engineering Integration (ongoing)
- [x] CAN RE workflow + tooling (`docs/can-re-workflow.md`); sessions ongoing
- [x] Emit Xterra DBC for decoded signals (`data/dbc/xterra_2008.dbc`)
- [x] Integrate decoded CAN channels as additional ML features
- [ ] Identify/collect individual ABS wheel speeds, then validate the bearing
  wear scenario (blocked until the pin-7 K-line vs passive-CAN question is
  resolved)

### Phase 5 — Dashboard & Deployment
- [x] Implement FastAPI backend with WebSocket telemetry stream
- [x] Build React/TypeScript PWA dashboard for live visualization
- [x] Support phone/head-unit mirroring as the primary in-vehicle display
- [ ] Keep official Android Auto, if pursued, as a limited status/alert
  companion rather than the full diagnostic dashboard
- [x] Package as systemd service with auto-start
- [x] Mount hardware permanently in vehicle

---

## Configuration (canary.yaml — example)

```yaml
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""  # optional

hardware:
  # `elm327_port` is the legacy fallback path. Current hardware uses the
  # Canable-native client configured under `can`.
  elm327_port: /dev/ttyUSB0
  can:
    interface: socketcan
    channel: can0
    bitrate: 500000

collection:
  obd_poll_rate_hz: 1
  storage_path: /data/canary
  session_retention_days: 30
  storage_warning_mb: 500       # also the acting low-space eviction floor
  storage_max_mb: 8000          # byte-cap ring buffer; null disables the cap

preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes:
    idle: { rpm_max: 900, speed_max: 0 }
    city: { speed_max: 50 }
    highway: { speed_min: 80 }
    wot: { throttle_min: 80 }

models:
  isolation_forest:
    enabled: true
    n_estimators: 200
    contamination: 0.01
    calibration_holdout_fraction: 0.2   # Phase 7.2 held-out calibration split
    target_flag_rate: 0.02              # per-mode threshold = 1-this quantile
  lstm_autoencoder:
    enabled: true
    sequence_length: 30
    latent_dim: 32
    threshold_k: 3.0
  fusion:                               # Phase 7.2 IF+LSTM+behavior fusion
    enabled: true
    severity_threshold: 0.5
    agreement_boost: 0.25
    behavior_warning_severity: 0.5
    behavior_critical_severity: 1.0
    min_agreement: 2                    # 6.2: fused is_anomaly needs >=2 independent sources
  roles:                                # A06.5: alert | evidence (operator decision 2026-09-23, A06.1/A06.2)
    isolation_forest: evidence
    sequence: evidence

alerts:                                 # 6.1: M-of-N persistence, e.g. 3-of-5
  min_consecutive_ticks: 3
  persistence_window_ticks: 5
  persistence_window_seconds: 8.0

dashboard:
  host: 0.0.0.0
  port: 8000

daemon:
  retention_sweep_seconds: 3600.0
```

---

## Project Structure

```
canary/
├── canary*.yaml                 # Configurations (dev / Pi / bench variants)
├── systemd/                     # canary, canary-mcp, ups-shutdown, can0 units
├── scripts/                     # Operational CLIs: record, retrain, refit,
│                                #   canaryctl, install_pi.sh, bake-offs, …
├── tools/                       # RE + analysis one-shots (job runners)
├── src/
│   ├── main.py                  # Entry point
│   ├── config/                  # Pydantic config, one module per section group (src.config re-exports)
│   ├── telemetry.py             # TelemetryRecord + sources (obd/can/decoded/gps)
│   ├── session.py               # Session dirs
│   ├── vehicle/                 # VIN-keyed profiles, tune revisions, ledger
│   ├── collection/              # OBD/CAN/GPS collectors, scheduler, replay
│   ├── storage/                 # Parquet ring buffer + retention
│   ├── preprocessing/           # Loader, resampling, segmentation, features,
│   │                            #   shared featurize tail, anomaly injection
│   ├── decode/                  # bindings/DBC/DID decode, behavior models,
│   │                            #   DTC catalog
│   ├── analysis/                # Derived signals, committees, surfaces,
│   │                            #   session classifier, refit, replay window
│   ├── models/                  # IF, LSTM/GRU-D, TCN, training, manifests,
│   │                            #   manager (hot-swap), benchmarks
│   ├── alerts/                  # Events, emitter, fusion, feedback
│   ├── daemon/                  # Runtime assembly, drain, inference,
│   │                            #   retention sweeper
│   ├── control/                 # Sessions, jobs, replay, classification,
│   │                            #   tuning (authenticated control plane)
│   ├── dashboard/               # FastAPI app, routes/, schemas, status,
│   │   └── static/              #   React/TS PWA (Vite)
│   ├── mcp_server/              # canary-mcp: tools/, write_tools/, audit
│   ├── companion/               # Laptop-side library + sync/publish API
│   └── datasets/                # Dataset export / health
├── data/                        # bindings.yaml, dids.yaml, dbc/, per-vehicle
│                                #   trees (e.g. nissan-xterra-2008/)
├── models/                      # Trained artifacts + manifests (binaries
│                                #   gitignored; deployed via scp)
├── docs/                        # References (this file); docs/roadmap/ for
│                                #   open plans
└── tests/                       # pytest suite (FE tests live in
                                 #   src/dashboard/static)
```
