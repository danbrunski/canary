# CANary

> Early anomaly detection for vehicle telemetry via OBD-II and CAN bus monitoring.

---

## Overview

CANary is an on-vehicle software system that continuously reads telemetry from a 2008 Nissan Xterra (and broadly any OBD-II compliant vehicle) via two data channels — the standardized OBD-II PID interface (ELM327) and the raw CAN bus (Canable/SocketCAN) — and uses unsupervised machine learning to learn normal vehicle behavior and flag anomalous conditions in real time.

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
│  ELM327 (USB)          Canable (USB / SocketCAN)           │
│  python-obd Async      python-can listen-only              │
│  OBD-II PIDs @ 1-10Hz  Raw CAN frames @ 100-500Hz         │
│        │                        │                           │
│        └──────────┬─────────────┘                           │
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
- The system SHALL connect to the vehicle via a USB ELM327 adapter using `python-obd`
- The system SHALL poll the following PIDs at a minimum: RPM, Vehicle Speed, Engine Load, Coolant Temp, Intake Air Temp, MAF Air Flow Rate, Throttle Position, Short-Term Fuel Trim Bank 1, Long-Term Fuel Trim Bank 1, O2 Sensor Voltages, Barometric Pressure, Engine Run Time
- The system SHALL use asynchronous polling (`obd.Async`) to avoid blocking the main processing loop
- The system SHALL log a warning and continue operation if a PID is unsupported by the vehicle ECU

#### FR-02 — Raw CAN Bus Collection
- The system SHALL connect to a Canable USB adapter via SocketCAN (`can0`)
- The system SHALL operate the CAN interface in **listen-only mode** to prevent bus collisions with the ELM327
- The system SHALL capture all CAN frame IDs present on the bus at full bus rate
- The system SHALL apply a DBC filter file (once reverse engineered) to decode proprietary Nissan Consult III signals into named channels

#### FR-03 — Data Storage
- The system SHALL write all collected telemetry to timestamped Parquet files on local storage (microSD or USB SSD)
- The system SHALL implement a rolling ring buffer that retains the last N days of raw data (configurable, default 30 days)
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
- The system SHALL handle ELM327 connection drops with automatic reconnect (configurable retry interval, default 10 seconds)

#### NFR-03 — Storage
- All raw telemetry SHALL be stored in Apache Parquet format for efficient columnar access
- The system SHALL alert (log warning + dashboard notification) when available storage drops below a configurable threshold (default 500 MB)

#### NFR-04 — Portability
- The system SHALL be deployable on any Raspberry Pi 5 (arm64, Raspberry Pi OS Bookworm or later)
- The system SHALL be fully configurable via a single YAML configuration file with no hardcoded vehicle- or hardware-specific values
- The system SHALL be containerizable (Docker/Podman) for development and testing on non-Pi hardware

#### NFR-05 — Testability
- The system SHALL support a simulation mode using the `ELM327-emulator` package in place of a physical adapter
- The system SHALL support replay of recorded Parquet sessions through the preprocessing and inference pipeline for offline testing
- All core modules SHALL have unit test coverage for preprocessing transforms, feature engineering, and threshold logic

#### NFR-06 — Security
- The ELM327 adapter SHALL be operated in read-only mode; the system SHALL never issue write or actuation commands to the vehicle bus
- The dashboard SHALL be accessible on localhost / LAN only; no external internet exposure by default

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| OBD-II interface | `python-obd` |
| CAN bus interface | `python-can` + SocketCAN (`can0`) |
| Data storage | Apache Parquet via `pandas` / `pyarrow` |
| ML baseline | `scikit-learn` (IsolationForest, OneClassSVM) |
| ML deep learning | `PyTorch` (LSTM Autoencoder) |
| API / dashboard backend | `FastAPI` + `uvicorn` |
| Feature engineering | `pandas`, `tsfresh` (optional) |
| Configuration | YAML via `pyyaml` |
| Testing | `pytest`, `ELM327-emulator` |
| Deployment | `systemd` service, optional Docker |
| Hardware | Raspberry Pi 5 (8 GB), USB ELM327, Canable USB, OBD-II Y-splitter |

---

## Hardware Setup

```
Vehicle OBD-II Port (J1962)
         │
    Y-Splitter
    ┌─────┴─────┐
    │           │
ELM327 USB   Canable USB
(python-obd)  (python-can, listen-only)
    │           │
    └─────┬─────┘
          │
    Raspberry Pi 5 (8 GB)
    - Raspberry Pi OS Bookworm (arm64)
    - USB SSD or high-endurance microSD
    - 12V→5V buck converter from ACC circuit
```

---

## Data Channels

### OBD-II PIDs (via ELM327)

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

### CAN Bus Channels (via Canable — pending DBC)

- ABS individual wheel speeds (FL, FR, RL, RR)
- Transfer case engagement state (2H / 4H / 4Lo)
- Steering angle sensor
- IPDM relay states
- BCM door/switch events
- Nissan Consult III proprietary module data (decoded post-RE)

---

## Development Phases

### Phase 1 — Data Collection (Weeks 1–2)
- [ ] Install and verify `python-obd` + ELM327 connection on Pi 5
- [ ] Install and verify `python-can` + Canable on Pi 5
- [ ] Implement dual-channel async data collection with Parquet logging
- [ ] Log 10+ diverse drive sessions (city, highway, cold start, idle, off-road)
- [ ] Validate offline using `ELM327-emulator`

### Phase 2 — Preprocessing & Baseline Model (Weeks 3–4)
- [ ] Build resampling and normalization pipeline
- [ ] Implement operating mode segmentation
- [ ] Train Isolation Forest on clean sessions
- [ ] Validate with synthetic anomaly injection
- [ ] Tune `contamination` parameter per operating mode

### Phase 3 — LSTM Autoencoder (Weeks 5–8)
- [ ] Implement sliding window dataset loader
- [ ] Build and train LSTM Autoencoder in PyTorch
- [ ] Implement real-time sliding window inference loop
- [ ] Calibrate detection threshold from training MAE distribution
- [ ] Benchmark false positive rate on held-out normal sessions

### Phase 4 — CAN Reverse Engineering Integration (Weeks 6–10, parallel)
- [ ] Complete CAN RE sessions using Z1 tool + Canable + SavvyCAN
- [ ] Build Xterra DBC file for decoded Consult III signals
- [ ] Integrate decoded CAN channels as additional ML features
- [ ] Validate ABS wheel speed anomaly detection (bearing wear scenario)

### Phase 5 — Dashboard & Deployment (Weeks 9–12)
- [ ] Implement FastAPI backend with WebSocket telemetry stream
- [ ] Build web dashboard for live visualization
- [ ] Package as systemd service with auto-start
- [ ] Mount hardware permanently in vehicle

---

## Configuration (canary.yaml — example)

```yaml
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""  # optional

hardware:
  elm327_port: /dev/ttyUSB0
  can_interface: can0
  can_bitrate: 500000

collection:
  obd_poll_rate_hz: 1
  storage_path: /data/canary
  session_retention_days: 30
  storage_warning_mb: 500

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
  lstm_autoencoder:
    enabled: true
    sequence_length: 30
    latent_dim: 32
    threshold_k: 3.0

dashboard:
  host: 0.0.0.0
  port: 8000
```

---

## Project Structure

```
canary/
├── canary.yaml                  # Main configuration
├── README.md
├── requirements.txt
├── systemd/
│   └── canary.service
├── src/
│   ├── __init__.py
│   ├── main.py                  # Entry point
│   ├── collection/
│   │   ├── obd_collector.py     # python-obd async collection
│   │   └── can_collector.py     # python-can SocketCAN collection
│   ├── storage/
│   │   └── parquet_writer.py    # Timestamped Parquet ring buffer
│   ├── preprocessing/
│   │   ├── resampler.py
│   │   ├── features.py          # Rolling features, rate-of-change
│   │   └── segmenter.py         # Operating mode classification
│   ├── models/
│   │   ├── isolation_forest.py
│   │   ├── lstm_autoencoder.py
│   │   └── model_manager.py     # Load / version / swap models
│   ├── alerts/
│   │   └── event_logger.py
│   └── dashboard/
│       ├── api.py               # FastAPI + WebSocket
│       └── static/              # Web dashboard frontend
├── data/
│   └── dbc/
│       └── xterra_2008.dbc      # CAN signal definitions (WIP)
├── models/
│   └── .gitkeep                 # Trained model artifacts (not committed)
└── tests/
    ├── test_preprocessing.py
    ├── test_isolation_forest.py
    └── test_lstm_autoencoder.py
```
