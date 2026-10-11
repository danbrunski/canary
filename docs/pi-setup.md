# Raspberry Pi 5 Setup

This is the first headless deployment path for CANary on a Raspberry Pi 5.
The Pi runs the full daemon under systemd and exposes the dashboard/API on the
local vehicle LAN.

Recommended operating model:

- Pi: data collection, storage, inference, API, dashboard hosting.
- Laptop: SSH for service/log work; browser or `curl` for API/dashboard work.
- Network: vehicle hotspot, phone hotspot, a trusted LAN, or the Pi's own
  self-hosted AP for field use away from any known network — see
  `docs/pi-hotspot.md`. v1 has no auth.

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

The Pi installer creates a `canary` user/group (make it a login user —
SSH/rsync run as `canary@`), creates `/etc/canary` and `/data/canary`,
installs the Python virtualenv, generates `/etc/canary/control.token`,
and installs + enables the systemd units: `canary.service`,
`canary-mcp.service` (MCP server, port 8001), and
`ups-shutdown.service` (X1202 UPS watchdog). It also installs
`/etc/tmpfiles.d/canary.conf` (the shared `/run/canary` runtime dir),
the i2c modules-load config, and the `canary-restart` sudoers drop-in.

```bash
sudo scripts/install_pi.sh
```

Default install is runtime-light: no dev tools and no Torch. To include CPU
Torch for LSTM inference:

```bash
sudo CANARY_TORCH=cpu scripts/install_pi.sh
```

Re-running the installer is safe and is what `canary-update` does when the
installer or the files it installs change. It rewrites a managed config file
only when its content differs, and restarts gpsd, chrony and journald only
when the file it writes for that service changed (or did not exist yet, as on
a fresh card); otherwise it prints `unchanged: <service> not restarted`.
`systemctl daemon-reload` always runs — it restarts nothing. The GPS hint
("set `hardware.gps.source: gpsd`") appears only when the config's
`hardware.gps.source` is not already `gpsd`.

**Hostname.** On a first install the installer renames a Pi still called
`raspberrypi` or `canary` to `canary-XXXX`, the last four hex digits of its
wired MAC (`88:a2:9e:1d:89:74` → `canary-8974`), and prints it at the end:
the Pi is then `canary-XXXX.local`. Every CANary calling itself `canary` made
two of them on one network race for the name, and mDNS gave whichever booted
second `canary-2.local`. A name you chose yourself is kept, a re-run
(`canary-update`) never renames, and `CANARY_HOSTNAME=<name>` sets one
explicitly. To find the name without a screen: `ip link show eth0` on the Pi,
or your router's client list.

The installer copies `canary.pi.example.yaml` to `/etc/canary/canary.yaml` if
the config does not already exist. Edit that file before first start:

```bash
sudo nano /etc/canary/canary.yaml
```

## 4. Bring Up SocketCAN

Three SocketCAN adapters are supported. The **CANary-HAT** (MCP2518FD/SPI —
current in-vehicle rig) needs no `config.txt` lines: its ID EEPROM names the
`canary-hat` overlay, `install_pi.sh` installs
`/boot/firmware/overlays/canary-hat.dtbo`, and the firmware loads it at boot
(programming a new board, checkout and K-line tools:
`hardware/canary-hat/firmware/README.md`). The **Waveshare 2-CH CAN HAT**
(MCP2515/SPI) enumerates as `can0` at boot once the SPI overlay is configured.
Either HAT comes up as `can0`, and `systemd/can0.service` raises it with
`restart-ms 100`. A **Canable USB** must run **candleLight/gs_usb**
firmware so it appears as a native `can0` (slcan firmware exposes only
`/dev/ttyACM0` and breaks the headless automation) — if `can0` does not
appear on plug-in, reflash per **`docs/canable-firmware.md`**.

`install_pi.sh` installs and enables `can0.service` with its udev rule, so
`can0` comes up at 500 kbit/s on every boot and whenever the device
reappears. To raise it by hand (first check, or after changing the
bitrate) for the default active-diagnostics OBD + CAN mode:

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
sudo systemctl start canary canary-mcp
sudo systemctl status canary canary-mcp ups-shutdown
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
export CANARY_CONTROL_TOKEN='<contents of /etc/canary/control.token on the Pi>'
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

## Data Drive (Optional)

CANary's data (`/data`, with `/data/canary` under it) can live on a separate
drive, such as an NVMe SSD in a USB 3 enclosure. Without one it stays on the SD
card, and nothing needs doing. On the dev Pi (2026-10-10, Patriot P300 in a
Realtek RTL9210 enclosure) the drive read all of `/data` in 47 s, against
about 225 s from the SD card, and took the constant small writes off the card.

**Run from a terminal, `install_pi.sh` asks where `/data` should live.** The
drive is the default when one is fitted:

```
Where should CANary keep /data?
  1) /dev/sda: 476.9G usb M.2 P300 512GB, blank: will be formatted
  2) the SD card (root filesystem)
Choice [1]:
```

Offered drives:
- **Already labelled `canary-data`:** used as it is.
- **Completely blank solid-state drives of 32 GB or more:** formatted when
  picked.
- **Not offered:** a drive with anything on it, and the SD card.

Choosing a drive does three things:
1. mounts it at `/data` by UUID;
2. copies what `/data` already held onto it, keeping the SD card's copy as
   `/data.sd-backup-<time>`;
3. makes `canary` and `canary-mcp` require the mount.

Choosing the SD card is remembered in `/etc/canary/data-drive.choice`, so
`canary-update` does not ask again about that drive. Once `/data` is on a
drive, nothing asks either.

**Run without a terminal** (unattended), the installer only adopts a drive
already labelled `canary-data`, and never formats.

To ask again, or to work by hand:

```bash
sudo bash scripts/data_drive.sh status            # where /data lives; any candidate drive
sudo bash scripts/data_drive.sh choose --ask      # the prompt, including drives turned down before
sudo bash scripts/data_drive.sh setup /dev/sda    # format a BLANK drive as canary-data, then adopt it
```

`setup` refuses a drive that has partitions, a signature, or anything mounted.

**With a data drive, the services do not start without it.** Each gets a
`RequiresMountsFor=/data` drop-in, `data-drive.conf`, written from `fstab` by
`data_drive.sh guard`. Starting without the drive would write into the empty
mount point on the SD card, and that data would vanish behind the drive once
it came back. The empty mount point is also made immutable (`chattr +i`).
`fstab` uses `nofail`, so the Pi still boots without the drive. To go back to
the SD card, remove the `/data` line from `/etc/fstab` and copy the data back.
The next `canary-update` then removes the guard.

## Useful Operations

```bash
sudo canary-update
sudo systemctl stop canary
sudo journalctl -u canary --since "10 minutes ago"
```

`canary-update` is installed into `/usr/local/sbin` by `install_pi.sh`. It
refuses to update during an active capture (through `restart_canary.sh
--check`) or when the checkout has tracked local changes outside `models/`,
fast-forwards the checkout, then does only the work the change needs, restarts both CANary
services through `scripts/restart_canary.sh`, and waits up to 90 s for the
daemon health endpoint. It uses the checkout's configured Git remote, so
private-repository installs still need a working deploy key or SSH credential
for now.

Models belong to the Pi, not to git. A manifest describes the binary beside it,
and binaries never travel by git (`canaryctl models upload`, scp, or a retrain
on the Pi), so a pulled manifest would not match the binary on disk. Since S03
(2026-10-04) nothing under `models/` is tracked at all, and the Pi keeps its
models in `daemon.state_dir` (`/data/canary/models/<vehicle>/`, with
`behavior_models.yaml`). The updater still puts any tracked `models/` path back
exactly as it was after a pull or rollback, which matters only when crossing
the commit that untracked the two manifests.

**Nothing the Pi writes is in the checkout.** With `daemon.state_dir:
/data/canary` (set in `canary.pi.example.yaml`), every runtime path resolves
under it: `anomalies/`, `models/`, and `control/` (jobs, ledgers, audit, the
session catalog, capture-profile overrides, the vehicle-registry overlay). The
tracked files the daemon reads stay in the checkout and are never written:
the capture-profile catalog (the dashboard saves overrides beside the other
state, layered by profile name), `profiles.yaml` (names, VINs and calibration
IDs learned on the Pi go to the overlay) and the behavior policy and validation
documents. Don't also pin individual paths in `canary.yaml`: an explicit path
wins over `state_dir`.

What runs is decided from `git diff --name-only <before> <after>` and from the
artefacts on disk, and every decision is printed as `ran (<reason>)` or
`skipped (<reason>)`:

| Step | Runs when |
| --- | --- |
| dashboard build (`pnpm install && pnpm build`) | anything under `src/dashboard/static/` changed, or `dist/index.html` is missing or older than a source file there |
| `pip install -e .` | `pyproject.toml` (or a lock/requirements file) changed, or `.venv/bin/python -m pip check` fails |
| `install_pi.sh` | it, `scripts/install_deps.py`, `scripts/update_canary.sh`, `canary.pi.example.yaml` or anything under `systemd/` changed, or `.venv` is missing (it installs dependencies itself, so the pip step reads `ran (by install_pi.sh)`) |

When the checkout is already at the fetched commit it says `already at
<hash>`, still checks the artefacts, and restarts. `--full` runs every step
regardless; use it for a first install and for "something is wrong" runs.

If the update changes `scripts/update_canary.sh` itself, the running copy
hands over to the new one right after the fetch (`re-executing the new
copy`), so the rest of the update runs the updater it just installed rather
than the copy bash had already opened.

If the restart fails or `/status` does not answer within the wait, the
checkout is reset to the commit it was on before the fetch (`git reset --hard
<before>`, safe because a dirty tree was refused up front — and never
anything older than that recorded commit), the steps that reverse diff needs
are redone, the daemon is restarted again, and health is re-checked.
`--no-rollback` leaves the failed version in place for debugging.

Every run ends with one summary block: `source: <before> -> <after>`, each
step's `ran`/`skipped` line, `restart:`, `health: ok after <n> s`, `memory
limits: enforced (...)` or `NOT enforced`, `rollback:`, `result: ... (exit
<code>)` and `total: <n> s`. The exit codes:

| Code | Meaning |
| --- | --- |
| 0 | updated (or already current) and healthy |
| 1 | setup error: not root, missing checkout or installer |
| 2 | usage error |
| 3 | refused: a capture is active (`restart_canary.sh --check`'s own code) |
| 4 | refused: the checkout has tracked local changes (outside `models/`) |
| 5 | fetch or fast-forward failed; nothing was changed |
| 6 | new code failed its health check; rolled back to `<before>`, which is healthy again |
| 7 | new code failed its health check and the rollback did not come up either |
| 8 | new code failed its health check and was left in place (`--no-rollback`, or nothing older to roll back to) |

Pass `--force` only when intentionally ending an active capture. To test a
specific remote ref without changing the updater, set `CANARY_UPDATE_REF`:

```bash
sudo CANARY_UPDATE_REF=main canary-update
```

The updater is tested offline by `tests/test_update_canary_script.py`, which
runs it against a throwaway git upstream with shimmed `systemctl`, `curl`,
`pnpm` and `runuser`; add a test there before changing its behaviour.

The planned public distribution replaces the Git fetch with a signed or
checksummed release bundle. The rest of this update path remains the same, and
the Pi will no longer need repository credentials.

For laptop frontend development against the Pi backend, see
`docs/headless-api.md`.
