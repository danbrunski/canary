# CANary

> Early anomaly detection for vehicle telemetry via OBD-II and CAN bus monitoring.

CANary is an on-vehicle software system that continuously reads telemetry from a 2008
Nissan Xterra (and broadly any OBD-II compliant vehicle) via raw CAN bus capture
(Canable / SocketCAN) and standardized OBD-II PID reads. It uses unsupervised
machine learning to learn normal vehicle behavior and flag anomalous conditions in
real time.

It runs headlessly on a Raspberry Pi 5 mounted in-vehicle, logs all telemetry to local
storage, and surfaces anomaly alerts via a local network dashboard.

The name references both the **CAN** bus and the *"canary in a coal mine"* idiom —
detecting subtle signs of trouble before they become failures.

---

## Features

- **Dual-channel ingestion** — raw CAN frames plus OBD-II Mode 01 / selected
  Mode 22 reads through one Canable-native `python-can` path; the legacy ELM327
  path remains for emulator/fallback coverage.
- **Parquet ring buffer** — timestamped, columnar, rotated per ignition session, with
  configurable retention.
- **Operating-mode segmentation** — Idle / City / Highway / WOT / Off-road, so models
  see context-appropriate signal ranges.
- **Layered models** — Isolation Forest baseline → LSTM Autoencoder → CNN-LSTM hybrid.
- **Local dashboard** — FastAPI + WebSocket, accessible only over the vehicle's LAN.
- **Read-only by design** — active OBD polling may transmit diagnostic read
  requests, but the system never issues write or actuation commands.

See [`docs/CANary-overview.md`](docs/CANary-overview.md) for the full architecture,
requirements, and development phases.

---

## Quick start

### Prerequisites

- Python 3.11+
- For on-vehicle use: Raspberry Pi 5 (8 GB), Canable USB / SocketCAN adapter.
  OBD reads run through the Canable-native client; no ELM327 is required.

### Install (development)

```bash
python3 scripts/install_deps.py
```

The installer creates `.venv`, installs the development dependencies, detects
the local NVIDIA driver's CUDA capability with `nvidia-smi`, and installs Torch
from the matching PyTorch wheel index. Use `--dry-run` to inspect the selected
commands, or override detection explicitly, for example:

```bash
python3 scripts/install_deps.py --torch cu126
python3 scripts/install_deps.py --torch cpu
```

See [`docs/install.md`](docs/install.md) for the CUDA selection table,
overrides, and verification commands.

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

### Raspberry Pi / headless

For the Pi, use the Pi-specific config and systemd install path:

```bash
sudo git clone <repo-url> /opt/canary
cd /opt/canary
sudo scripts/install_pi.sh
sudo nano /etc/canary/canary.yaml
sudo systemctl start canary
```

The daemon serves the read-only API and browser dashboard on the configured
LAN address, defaulting to `http://<pi-hostname-or-ip>:8000/`. See
[`docs/pi-setup.md`](docs/pi-setup.md) and
[`docs/headless-api.md`](docs/headless-api.md).

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
├── canary.pi.example.yaml    # Pi/systemd deployment template
├── pyproject.toml
├── docs/
│   ├── CANary-overview.md    # Architecture, requirements, phases
│   ├── pi-setup.md           # Raspberry Pi 5 bring-up
│   ├── headless-api.md       # REST/WebSocket/browser access
│   └── implementation-tasks.md
├── systemd/
│   └── canary.service        # Headless daemon unit
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

The software stack is past the original Phase 1 collection milestone:
preprocessing, IF/LSTM training and inference, bindings decode, behavior-model
surfaces, dashboard, alert logging, and model hot-swap are implemented. The
Canable-native OBD collector is now the default hardware path. The remaining v1
work is mostly operational: real drive captures for training/validation, Pi
deployment, and long-run vehicle validation.

See [`docs/implementation-tasks.md`](docs/implementation-tasks.md) for the current
task breakdown and [`docs/data-collection-plan.md`](docs/data-collection-plan.md)
for the capture backlog.

---

## Hardware

```
Vehicle OBD-II Port (J1962)
         │
   Canable USB / SocketCAN
  (CAN sniff + OBD ISO-TP)
          │
    Raspberry Pi 5 (8 GB)

Optional RE rig: add an NT510 via an OBD-II splitter when a capture needs a
human-driven diagnostic stimulus. The ELM327 path is retained only for legacy
captures and emulator coverage.
```

---

## License

TBD.
