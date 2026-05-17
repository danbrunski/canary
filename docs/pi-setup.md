# Raspberry Pi 5 Setup

This is the first headless deployment path for CANary on a Raspberry Pi 5.
The Pi runs the full daemon under systemd and exposes the dashboard/API on the
local vehicle LAN.

Recommended operating model:

- Pi: data collection, storage, inference, API, dashboard hosting.
- Laptop: SSH for service/log work; browser or `curl` for API/dashboard work.
- Network: vehicle hotspot, phone hotspot, or a trusted LAN. v1 has no auth.

There are two install paths:

- **Developer bring-up** (run + develop from a clone): one idempotent script,
  covered just below. Use this on a desktop/bench Pi.
- **In-vehicle systemd service** (system user, `/etc/canary`, `/data/canary`):
  sections 1–6 below, driven by `scripts/install_pi.sh`.

## Developer Bring-up (one command)

On a fresh clone, to get a working run-and-develop environment (OS packages,
dev virtualenv with CPU PyTorch, `canary.yaml`, and the built dashboard):

```bash
git clone <repo-url> ~/canary && cd ~/canary
./scripts/setup_pi_dev.sh
```

It is idempotent and self-documenting (`./scripts/setup_pi_dev.sh --help`).
Tune it via environment variables, e.g. `CANARY_TORCH=none` (skip PyTorch),
`CANARY_DASHBOARD=0` (skip the frontend build), or `CANARY_VERIFY=1` (run
ruff/mypy/pytest at the end). Once PyTorch is installed, re-run with
`CANARY_TORCH=none` so it is not re-downloaded.

The rest of this document is the in-vehicle systemd service path.

## 1. Install OS Packages

On Raspberry Pi OS Bookworm or later:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip can-utils iproute2
```

Optional, only if you want to build the React dashboard on the Pi:

```bash
sudo apt install -y nodejs npm
sudo corepack enable
```

## 2. Clone

Use `/opt/canary` for the service install:

```bash
sudo mkdir -p /opt
sudo git clone <repo-url> /opt/canary
cd /opt/canary
```

If you clone somewhere else, run the installer from that clone; it writes the
actual clone path into `/etc/canary/canary.env`.

## 3. Install CANary

The Pi installer creates a non-root `canary` user/group, creates
`/etc/canary` and `/data/canary`, installs the Python virtualenv, installs the
systemd unit, generates `/etc/canary/control.token`, and enables the service.

```bash
sudo scripts/install_pi.sh
```

Default install is runtime-light: no dev tools and no Torch. To include CPU
Torch for LSTM inference:

```bash
sudo CANARY_TORCH=cpu scripts/install_pi.sh
```

The installer copies `canary.pi.example.yaml` to `/etc/canary/canary.yaml` if
the config does not already exist. Edit that file before first start:

```bash
sudo nano /etc/canary/canary.yaml
```

## 4. Bring Up SocketCAN

The Canable must run **candleLight/gs_usb** firmware so it appears as a native
`can0` (slcan firmware exposes only `/dev/ttyACM0` and breaks the headless
automation). If `can0` does not appear on plug-in, reflash per
**`docs/canable-firmware.md`** before continuing.

With candleLight firmware `can0` appears automatically; bring it up for the
default active read-only OBD + CAN mode:

```bash
sudo ip link set can0 down || true
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
ip -details link show can0
```

The final command should show `bitrate 500000`.

If you want passive CAN-only capture instead, set `collection.obd_source:
disabled` in `/etc/canary/canary.yaml` and bring the interface up with
`listen-only on`. `docs/can-setup.md` has both variants.

Check that frames arrive with ignition on:

```bash
candump can0
```

Stop with `Ctrl-C`. More details are in `docs/can-setup.md`.

## 5. Build The Dashboard

The API works without a frontend build. To serve the browser dashboard from
the Pi:

```bash
cd /opt/canary/src/dashboard/static
corepack prepare pnpm@10.29.3 --activate
pnpm install --frozen-lockfile
pnpm build
```

The FastAPI backend auto-mounts `src/dashboard/static/dist/` on `/` when it
exists.

## 6. Start And Inspect

```bash
sudo systemctl start canary
sudo systemctl status canary
sudo journalctl -u canary -f
```

From a laptop or phone on the same network:

```bash
curl http://canary.local:8000/status
curl 'http://canary.local:8000/telemetry/latest?n=20'
```

For control commands from a laptop, copy the token once:

```bash
export CANARY_HOST=http://canary.local:8000
export CANARY_TOKEN='<contents of /etc/canary/control.token on the Pi>'
```

Then:

```bash
scripts/canaryctl.py preflight
scripts/canaryctl.py capture start --purpose "next drive" --protocol drive
scripts/canaryctl.py tag cold_start
scripts/canaryctl.py capture stop --reason arrived
scripts/canaryctl.py jobs start corpus_report
```

Open the dashboard at:

```text
http://canary.local:8000/
```

If mDNS is not available, use the Pi's IP address instead of `canary.local`.

## Useful Operations

```bash
sudo systemctl restart canary
sudo systemctl stop canary
sudo journalctl -u canary --since "10 minutes ago"
sudo -u canary git -C /opt/canary pull --ff-only
```

For laptop frontend development against the Pi backend, see
`docs/headless-api.md`.
