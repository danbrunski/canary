# CANary

> Early anomaly detection and diagnostics for vehicle telemetry via OBD-II and CAN bus monitoring.

CANary is an on-vehicle telemetry, anomaly-detection, and diagnostics platform.
It continuously reads raw CAN bus traffic (SocketCAN — CANary-HAT, Waveshare CAN HAT or
Canable USB) and standardized OBD-II PID reads, uses unsupervised machine
learning to learn normal vehicle behavior and flag anomalous conditions in real
time, and doubles as an **engine performance analysis and diagnostic
workbench** — PID/DID exploration, learned behavior surfaces, physics-derived
channels, trouble codes, and trip/session analysis.

It is designed to be **generalized and adaptable to any OBD-II vehicle**:
per-VIN vehicle profiles partition all learned state, and vehicle-specific
facts live in profile/config/data files rather than code. The current install
is a single 2008 Nissan Xterra, which serves as the development vehicle.

It runs headlessly on a Raspberry Pi 5 mounted in-vehicle, logs all telemetry to local
storage, and surfaces anomaly alerts via a local network dashboard.

The name references both the **CAN** bus and the *"canary in a coal mine"* idiom —
detecting subtle signs of trouble before they become failures.

---

## Features

- **Dual-channel ingestion** — raw CAN frames plus OBD-II Mode 01 / selected
  Mode 22 (Nissan DID) reads through one CAN-native `python-can` path, over
  the CANary-HAT (MCP2518FD/SPI, the current in-vehicle rig; see
  `hardware/canary-hat/`), the Waveshare 2-CH CAN HAT (MCP2515/SPI) or a
  Canable USB adapter; the ELM327 path remains for emulator/fallback
  coverage. An optional serial NMEA GPS receiver adds ground-speed and
  position channels.
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
- **Vehicle profiles** — the VIN (Mode 09) keys a per-vehicle data partition:
  training corpus, thresholds, behavior surfaces, and session storage are all
  profile-scoped, so the platform ports to another vehicle without
  cross-contamination.
- **Diagnostics workbench** — learned behavior-surface heatmaps with live
  overlay, a Nissan DID map workbench, custom dashboards and multi-channel
  charting, session replay through the live pipeline (isolated `_replay-*`
  outputs), and an authenticated control plane for captures, retrains, and
  model publishing.
- **Engine tuning & calibration analysis** — named capture profiles (Mode 01,
  Mode 22 and RAM channels at stated rates), ReadMemoryByAddress channels with
  same-instant multi-field windows for quantities that are not DIDs (the ECM's
  commanded target AFR, ROM map-cell traces), delivery-error and fuel-trim
  surface analysis with explicit gates and noise floors, a tune-revision
  registry, and a ROM table viewer. Each RAM address records the calibration
  it was checked on, and a known-answer relation re-verifies it on demand.
- **Local dashboard** — FastAPI + WebSocket, accessible only over the vehicle's LAN.
- **MCP server** — the daemon's read/control surface exposed to MCP-speaking
  agents (Claude Code and others), including ad-hoc SQL over the Parquet
  store.
- **Active diagnostics** — OBD polling, ISO-TP flow control, session control,
  security access, memory reads, and ad-hoc diagnostic/control requests all
  transmit on the bus. The request path accepts any service byte, including
  state-changing and actuation commands. CANary's hard boundary is narrower:
  it writes ECU non-volatile memory (a reflash or calibration write) only with
  the operator's explicit permission for that operation, and never while the
  vehicle is moving or the engine is running. Broadcast CAN capture itself
  remains sniff-only.

See [`docs/CANary-overview.md`](docs/CANary-overview.md) for the full architecture,
requirements, and development phases.

---

## Quick start

### Prerequisites

- Python 3.12+
- For on-vehicle use: Raspberry Pi 5 plus a SocketCAN adapter — CANary-HAT
  (current in-vehicle rig), Waveshare 2-CH CAN HAT or Canable USB. OBD reads run through
  the CAN-native client; no ELM327 is required.

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
cp canary.dev.example.yaml canary.yaml
# edit canary.yaml — vehicle, hardware ports, storage path, model settings
```

`canary.yaml` is gitignored; commit shared development-template changes to
`canary.dev.example.yaml` and deployment-template changes to
`canary.pi.example.yaml` instead.

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

After the first install, update and redeploy the complete Pi application with:

```bash
sudo canary-update
```

The command stops before changing anything when a capture is active, then
updates the source, does only the dashboard build / dependency / Pi
configuration work that change needs (`--full` forces all of it), restarts both
services, checks that the daemon comes back, and rolls back to the previous
commit if it does not. It ends with a summary block and an exit code that says
what happened; see `docs/pi-setup.md`.

The daemon serves the browser dashboard, public observation endpoints, and an
authenticated control API on the configured LAN address, defaulting to
`http://<pi-hostname-or-ip>:8000/`. A companion
**MCP server** (Model Context Protocol) runs as a separate systemd unit at
`http://<pi-hostname-or-ip>:8001/mcp` and exposes the same surface — plus
ad-hoc Parquet SQL — to Claude Code, Qwen-Agent, and other MCP-speaking
clients. See [`docs/pi-setup.md`](docs/pi-setup.md),
[`docs/headless-api.md`](docs/headless-api.md), and
[`docs/mcp-server.md`](docs/mcp-server.md).

From a laptop, `scripts/canaryctl.py` drives the authenticated control API:

```bash
export CANARY_HOST=http://192.168.0.230:8000
export CANARY_CONTROL_TOKEN='<token from /etc/canary/control.token>'
scripts/canaryctl.py preflight
scripts/canaryctl.py capture start --purpose "next drive" --protocol drive
scripts/canaryctl.py tag highway_cruise
scripts/canaryctl.py capture stop
scripts/canaryctl.py jobs start corpus_report
```

### Test / lint / typecheck

**Before every commit run the backend CI-parity gate** — it runs the same
lint, format, type, and pytest commands as CI's Python job, in the same order:

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

For dashboard changes, also run the frontend checks used by CI:

```bash
cd src/dashboard/static
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test
pnpm e2e
```

Playwright's Chromium browser must be installed once before `pnpm e2e`; see
[`src/dashboard/static/README.md`](src/dashboard/static/README.md) for the
frontend development workflow. During Python iteration, use
`.venv/bin/python -m pytest -m "not integration and not slow"`; reserve bare
pytest for the commit gate.

Do not wrap FastAPI/Starlette `TestClient` tests in GNU `timeout` inside the
Codex sandbox. `timeout 90s .venv/bin/pytest ...` can deadlock AnyIO's blocking
portal and make simple in-process API tests look hung even though
`.venv/bin/pytest ...` completes normally. If a run appears stuck, first rerun
the same pytest command directly before debugging app code.

---

## Project layout

```
canary/
├── canary.dev.example.yaml   # Development template (copy → canary.yaml)
├── canary.pi.example.yaml    # Pi/systemd deployment template
├── pyproject.toml
├── docs/
│   ├── CANary-overview.md    # Architecture, requirements, phases
│   ├── pi-setup.md           # Raspberry Pi 5 bring-up
│   ├── headless-api.md       # REST/WebSocket/browser access
│   ├── implementation-tasks.md
│   └── roadmap/              # Planned work as checklist docs
│       ├── tasks/            #   detailed implementation checklists (D03, W00–W07)
│       └── archive/          #   finished plans, kept for their reasoning
├── systemd/
│   └── canary.service        # Headless daemon unit
├── src/
│   ├── main.py               # Entry point
│   ├── config.py             # YAML → Pydantic
│   ├── vehicle/              # Per-VIN profiles, tune revisions, vehicle ledger
│   ├── collection/           # OBD + CAN + GPS ingestion
│   ├── daemon/               # Live capture + inference runtime
│   ├── storage/              # Parquet ring buffer, retention
│   ├── preprocessing/        # Resample, segment, features
│   ├── decode/               # Bindings, DIDs, DTC catalog, behavior models
│   ├── analysis/             # Derived signals, sensor committees, refit
│   ├── models/               # Isolation Forest, GRU-D LSTM AE, TCN
│   ├── alerts/               # Fusion, persistence gating, event logging
│   ├── control/              # Authenticated control API + jobs
│   ├── datasets/             # Dataset export
│   ├── companion/            # Laptop-side library + local API
│   ├── mcp_server/           # MCP surface for agents
│   └── dashboard/            # FastAPI + WebSocket + React frontend
└── tests/
```

---

## Status

The software stack is well past the original collection milestone. The
CAN-native collector and in-vehicle Pi deployment are live, with per-mode
Isolation Forest, the GRU-D masking-aware LSTM autoencoder (TCN selectable),
behavior residuals, sensor committees, GPS, per-VIN profiles, runtime replay,
DTC and Mode 06 monitor reads, longitudinal trends, configurable capture
profiles, and historical analysis surfaced through the dashboard and control
plane. Model evaluation includes held-out false-positive checks, synthetic
fault injection, and a committed real-drive ground-truth fixture.

Open data-collection, model-validation, diagnostic-discovery, and engine
analysis work is tracked as checkbox-level task truth in
[`docs/roadmap/`](docs/roadmap/); the roadmap documents, rather than this
summary, are authoritative for current completion state.

See [`docs/implementation-tasks.md`](docs/implementation-tasks.md) for the historical
task breakdown, [`docs/roadmap/tasks/`](docs/roadmap/tasks/README.md) for the live
implementation checklists, and
[`docs/roadmap/archive/data-collection-plan.md`](docs/roadmap/archive/data-collection-plan.md)
for the original (now complete) capture plan.

---

## Hardware

```
Vehicle OBD-II Port (J1962)
         │
  SocketCAN adapter — either:
   · CANary-HAT rev 2.0 (MCP2518FD/SPI; also K-line, GNSS, IMU) — current in-vehicle rig
   · Waveshare 2-CH CAN HAT (MCP2515/SPI)
   · Canable USB
  (CAN sniff + OBD ISO-TP)
          │
      Raspberry Pi 5 ──── optional serial NMEA GPS (USB)
                     ──── optional X1202 UPS HAT (battery-gated shutdown)

Optional RE rig: add an NT510 via an OBD-II splitter when a capture needs a
human-driven diagnostic stimulus. The ELM327 path is retained for legacy
captures and emulator coverage.
```

---

## License

TBD.
