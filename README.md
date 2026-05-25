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
  path remains for emulator/fallback coverage. An optional serial NMEA GPS
  receiver adds ground-speed and position channels.
- **Parquet ring buffer** — timestamped, columnar, rotated per ignition session,
  bounded by both age and total bytes, with an emergency low-space sweep.
- **Operating-mode segmentation** — KOEO / warmup / idle / city / highway (with
  a transient-stop sub-mode), so models see context-appropriate signal ranges.
  Behavior models are fit and thresholded per mode.
- **Layered detection** — per-mode Isolation Forest + a GRU-D masking-aware LSTM
  autoencoder (handles canary's multi-rate, gappy telemetry without fabricating
  fills) + first-class behavior residuals, combined in a calibrated-severity
  fusion layer.
- **Sensor committees & physics residuals** — redundant-sensor cross-checks
  (e.g. OBD speed ↔ GPS) and physical-relationship residuals (fuel-trim bank
  balance, MAF/RPM air mass) surface sensor faults and engine misbehavior.
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

To bring up a fresh Pi for running **and developing** CANary in one idempotent
step (OS packages, dev venv with CPU Torch, `canary.yaml`, built dashboard):

```bash
./scripts/setup_pi_dev.sh        # --help for options
```

For the in-vehicle headless **service**, use the Pi-specific config and systemd
install path instead:

```bash
sudo git clone <repo-url> /opt/canary
cd /opt/canary
sudo scripts/install_pi.sh
sudo nano /etc/canary/canary.yaml
sudo systemctl start canary
```

The daemon serves the read-only API and browser dashboard on the configured
LAN address, defaulting to `http://<pi-hostname-or-ip>:8000/`. A companion
**MCP server** (Model Context Protocol) runs as a separate systemd unit at
`http://<pi-hostname-or-ip>:8001/mcp` and exposes the same surface — plus
ad-hoc Parquet SQL — to Claude Code, Qwen-Agent, and other MCP-speaking
clients. See [`docs/pi-setup.md`](docs/pi-setup.md),
[`docs/headless-api.md`](docs/headless-api.md), and
[`docs/mcp-server.md`](docs/mcp-server.md).

From a laptop, `scripts/canaryctl.py` drives the authenticated control API:

```bash
export CANARY_HOST=http://192.168.0.230:8000
export CANARY_TOKEN='<token from /etc/canary/control.token>'
scripts/canaryctl.py preflight
scripts/canaryctl.py capture start --purpose "next drive" --protocol drive
scripts/canaryctl.py tag highway_cruise
scripts/canaryctl.py capture stop
scripts/canaryctl.py jobs start corpus_report
```

### Test / lint / typecheck

**Before every commit run the one-command CI-parity gate** — it runs exactly
what CI (`.github/workflows/ci.yml`) runs, in the same order, so "green
locally" means "green in CI":

```bash
scripts/check.sh          # ruff check . → ruff format --check . → mypy → pytest -v
scripts/check.sh --fix    # auto-apply `ruff format` + `ruff check --fix`, then gate
scripts/check.sh --fast   # skip pytest (lint/format/type only)
```

Enable the tracked pre-commit hook **once per clone** so this is enforced, not
remembered (the recurring failure is forgetting `ruff format --check .`, which
`ruff check .` does **not** catch):

```bash
git config core.hooksPath scripts/git-hooks   # runs scripts/check.sh --fast pre-commit
```

Bypass for rare WIP commits: `git commit --no-verify`. The raw underlying
commands, if you need them individually:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

Do not wrap FastAPI/Starlette `TestClient` tests in GNU `timeout` inside the
Codex sandbox. `timeout 90s .venv/bin/pytest ...` can deadlock AnyIO's blocking
portal and make simple in-process API tests look hung even though
`.venv/bin/pytest ...` completes normally. If a run appears stuck, first rerun
the same pytest command directly before debugging app code.

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
│   ├── daemon.py             # Live capture + inference loop
│   ├── config.py             # YAML → Pydantic
│   ├── log.py
│   ├── collection/           # OBD + CAN + GPS ingestion
│   ├── storage/              # Parquet ring buffer
│   ├── preprocessing/        # Resample, segment, features
│   ├── analysis/             # Derived signals, sensor committees, refit
│   ├── models/               # Isolation Forest, GRU-D LSTM AE, fusion
│   ├── alerts/               # Fusion + anomaly event logging
│   ├── control/              # Authenticated control API
│   ├── mcp_server/           # MCP surface for agents
│   └── dashboard/            # FastAPI + WebSocket
└── tests/
```

---

## Status

The software stack is well past the original Phase 1 collection milestone:
preprocessing, per-mode Isolation Forest, the GRU-D masking-aware LSTM
autoencoder, behavior residuals fused into a calibrated severity score, sensor
committees, GPS ingestion, bindings decode, behavior-model surfaces, dashboard,
alert logging, and model hot-swap are all implemented. The Canable-native OBD
collector is the default hardware path, and deployment to the in-vehicle Pi is
active. The remaining v1 work is mostly operational: accumulating real drive
captures for training/validation and long-run vehicle validation.

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
    Raspberry Pi 5 (8 GB) ──── optional serial NMEA GPS (USB)

Optional RE rig: add an NT510 via an OBD-II splitter when a capture needs a
human-driven diagnostic stimulus. The ELM327 path is retained only for legacy
captures and emulator coverage.
```

---

## License

TBD.
