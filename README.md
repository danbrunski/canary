# CANary

> Early anomaly detection for vehicle telemetry via OBD-II and CAN bus monitoring.

CANary is an on-vehicle software system that continuously reads telemetry from a 2008
Nissan Xterra (and broadly any OBD-II compliant vehicle) via two data channels — the
standardized OBD-II PID interface (ELM327) and the raw CAN bus (Canable / SocketCAN) —
and uses unsupervised machine learning to learn normal vehicle behavior and flag
anomalous conditions in real time.

It runs headlessly on a Raspberry Pi 5 mounted in-vehicle, logs all telemetry to local
storage, and surfaces anomaly alerts via a local network dashboard.

The name references both the **CAN** bus and the *"canary in a coal mine"* idiom —
detecting subtle signs of trouble before they become failures.

---

## Features

- **Dual-channel ingestion** — OBD-II PIDs via `python-obd` (async) and raw CAN frames
  via `python-can` + SocketCAN (listen-only, never writes to the bus).
- **Parquet ring buffer** — timestamped, columnar, rotated per ignition session, with
  configurable retention.
- **Operating-mode segmentation** — Idle / City / Highway / WOT / Off-road, so models
  see context-appropriate signal ranges.
- **Layered models** — Isolation Forest baseline → LSTM Autoencoder → CNN-LSTM hybrid.
- **Local dashboard** — FastAPI + WebSocket, accessible only over the vehicle's LAN.
- **Read-only by design** — the system never issues write or actuation commands to the
  vehicle bus.

See [`docs/CANary-overview.md`](docs/CANary-overview.md) for the full architecture,
requirements, and development phases.

---

## Quick start

### Prerequisites

- Python 3.11+
- For on-vehicle use: Raspberry Pi 5 (8 GB), USB ELM327, Canable USB, OBD-II Y-splitter

### Install (development)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### Configure

```bash
cp canary.example.yaml canary.yaml
# edit canary.yaml — vehicle, hardware ports, storage path, model settings
```

`canary.yaml` is gitignored; commit changes to `canary.example.yaml` instead.

### Run

```bash
.venv/bin/python -m src.main
```

### Test / lint / typecheck

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

---

## Project layout

```
canary/
├── canary.example.yaml       # Template config (copy → canary.yaml)
├── pyproject.toml
├── docs/
│   ├── CANary-overview.md    # Architecture, requirements, phases
│   └── implementation-tasks.md
├── src/
│   ├── main.py               # Entry point
│   ├── config.py             # YAML → Pydantic
│   ├── log.py
│   ├── collection/           # OBD + CAN ingestion
│   ├── storage/              # Parquet ring buffer
│   ├── preprocessing/        # Resample, segment, features
│   ├── models/               # Isolation Forest, LSTM AE
│   ├── alerts/
│   └── dashboard/            # FastAPI + WebSocket
└── tests/
```

---

## Status

Phase 1 — Data Collection. See
[`docs/implementation-tasks.md`](docs/implementation-tasks.md) for the current task
breakdown.

---

## Hardware

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
```

---

## License

TBD.
