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

`install_pi.sh` installs what the dashboard build needs and then builds it
(section 5); `canary-update` rebuilds it and refuses to update without it. It
installs `nodejs` from apt without npm (362 packages on trixie), and pnpm as
its standalone binary at the version `package.json` pins, in
`/usr/local/bin/pnpm`. By hand, the older way:

```bash
sudo apt install -y nodejs npm
sudo corepack enable
```

## 2. Clone

Clone anywhere and run the installer from that clone; it writes the actual
clone path into `/etc/canary/canary.env`. The Pis in use so far have it in the
`canary` user's home:

```bash
git clone <repo-url> ~/canary
cd ~/canary
```

`/opt/canary` (`sudo git clone <repo-url> /opt/canary`) works the same way.

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

**WiFi networks.** The dashboard's System → Network page manages the Pi's
saved WiFi (behind the control token): it lists every network NetworkManager
holds, including one Raspberry Pi Imager wrote, with which is connected and
which are in range; adds one from the Pi's scan or by name (a hidden network,
or one not here), either **Join now** (undone if the join fails) or **Save
for later** (a phone hotspot, a friend's network); reorders them, so the
first in range wins; and forgets them. The Pi joins the highest saved network
in range on its own. Save your phone's hotspot and the Pi can be reached
anywhere the phone is. The root `canary-provision` service does the work, over
`/run/canary/network.sock`; if it is not running the page says so (a 503)
rather than showing an empty list, and `sudo systemctl restart
canary-provision` brings it back. Switching or forgetting the network your
browser is using drops the page; the cable address below still works. Before
NetworkManager has WiFi, `sudo nmcli device wifi connect "<SSID>" password
"<passphrase>"` over the cable does the same. API: `docs/headless-api.md`.

**Wired address.** The installer gives eth0 the fixed address
`192.168.137.2/24` (NetworkManager profile `canary-wired`), the address the
docs, `sync-train.sh` and companion discovery use for a Pi on a direct cable.
Give the laptop's Ethernet adapter `192.168.137.1/24`, or turn on Windows
Internet Connection Sharing, which assigns it. The profile has no default
route, so a cable never takes over from Wi-Fi, and it keeps an IPv6
link-local address, so `ping ff02::1%<laptop-interface>` finds the Pi even
when the laptop side is wrong. Until 2026-11-06 nothing set this address: a
Pi installed by the script alone stayed on DHCP and could not be found on the
cable. A Pi whose eth0 was already in use moves at the next boot (the
installer says so), because switching it mid-install would cut an SSH
session on that cable. `CANARY_WIRED_ADDRESS=<cidr>` picks another address
and `none` leaves eth0 alone. Edit the profile with `nmcli connection modify
canary-wired ...` and the installer keeps your change.

The installer copies `canary.pi.example.yaml` to `/etc/canary/canary.yaml` if
the config does not already exist. Edit that file before first start:

```bash
sudo nano /etc/canary/canary.yaml
```

## 4. Bring Up SocketCAN

Three SocketCAN adapters are supported. The **CANary-HAT** (MCP2518FD/SPI —
current in-vehicle rig) needs no `config.txt` lines: its ID EEPROM names its
revision's overlay (`canary-hat` on rev 2.0, `canary-hat-r21` on rev 2.1),
`install_pi.sh` installs both into `/boot/firmware/overlays/`, and the firmware
loads the named one at boot (programming a new board, checkout, K-line tools and
the two revisions' pin maps: `hardware/canary-hat-firmware/README.md`). The **Waveshare 2-CH CAN HAT**
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

`install_pi.sh` builds the browser dashboard (`pnpm install --frozen-lockfile
&& pnpm build` in `src/dashboard/static`, as the `canary` user) when
`dist/index.html` is missing or older than a source file there. That is the
same rule `canary-update` uses (`scripts/lib/dashboard_build.sh`), so a re-run
with a current bundle prints `unchanged: dashboard dist/ is current, not
rebuilt`. Until 2026-11-06 only `canary-update` built it, so a Pi set up by
the installer alone served no dashboard.

The API works without a frontend build, so a failed build does not stop the
install. The installer finishes and ends with `DASHBOARD NOT BUILT`. Fix the
error and run the installer again, or `sudo canary-update --full`.
`CANARY_DASHBOARD_BUILD=0` skips the build. By hand:

```bash
cd /opt/canary/src/dashboard/static
sudo -u canary pnpm install --frozen-lockfile
sudo -u canary pnpm build
```

The FastAPI backend mounts `src/dashboard/static/dist/` on `/` only if it
exists when the daemon starts. If `canary` was already running when the
installer built a missing bundle, the installer ends with `RESTART NEEDED`.
Restart with `sudo scripts/restart_canary.sh` to serve it.

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

### The control token

Most of the dashboard, and every control command, is locked until the browser
or shell holds the Pi's control token. On the Pi, over SSH:

```bash
canary-token          # prints the token
canary-token pair     # approves a browser waiting on "Pair this device"
```

In the dashboard, click the **control token** pill in the top bar (on a phone:
the menu, under "Control token") and paste what `canary-token` printed. The
browser remembers it. Pairing avoids the copy: press **Pair this device** in
the dashboard, then either turn the key ACC → ON → ACC in the truck, or, on the
bench, run `canary-token pair`. A paired browser gets a token of its own, which
`canaryctl devices --remove ID` revokes without affecting anything else.

The token lives in `/etc/canary/control.token` (root:canary, 0640), so the
`canary` user reads it without sudo. The installer no longer prints it: it
used to, which put it in every install log and console scrollback.

For control commands from a laptop, copy it once:

```bash
export CANARY_HOST=http://canary.local:8000
export CANARY_CONTROL_TOKEN="$(ssh canary@canary.local canary-token)"
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

### Updating from the dashboard

System → System Info has an **Update** card: what is installed, **Check for
updates** (the commits available, with their subjects), and **Update**, which
runs this same `canary-update` and shows each step as it starts. The daemon
runs as `canary` with `NoNewPrivileges` and cannot run sudo, so it never
updates anything itself:

1. `POST /control/update` writes `/run/canary/update_request`.
2. The root `canary-update.path` unit sees it and starts the oneshot
   `canary-update.service`. Its `ExecStartPre` renames the request to
   `update_request.taken` before any Python runs, so a request that crashes
   the runner is already consumed and cannot start it again.
3. The runner (`python -m src.control.update_runner`, on the checkout's venv)
   runs a check (`git fetch` as the checkout's owner, then the commits behind
   and whether the checkout is dirty) or `canary-update`, and writes
   `/run/canary/update_status` throughout: the step, the summary block, the
   exit code and what it means, and a heartbeat every 10 s. A status whose
   heartbeat is over 3 minutes old reads `lost`.

The daemon restarts in the middle of an update, so the page loses its
connection. It keeps polling, and when the daemon answers again it reads the
status and says "Updated to `<sha>`" or "Rolled back: `<reason>`".

Every refusal `canary-update` has still applies, and some come earlier:
the update action is refused (409) before anything is filed while a capture
records or an ECU backup runs; either action is refused while another is
running, and with no default route ("no internet"). Before running
`canary-update` the runner also checks that the git remote answers
(`git ls-remote`, 20 s), so a WiFi network without internet is reported
before anything starts, not after a fetch timeout. `--force` is not offered
from the dashboard; `--full` is.

Logs: `journalctl -u canary-update` has every line `canary-update` printed.
`install_pi.sh` installs the two units and starts `canary-update.path`. On a
Pi installed before that, `POST /control/update` answers 503 naming
`canary-update.path` until `install_pi.sh` runs again.

The planned public distribution replaces the Git fetch with a signed or
checksummed release bundle. The rest of this update path remains the same, and
the Pi will no longer need repository credentials.

For laptop frontend development against the Pi backend, see
`docs/headless-api.md`.
