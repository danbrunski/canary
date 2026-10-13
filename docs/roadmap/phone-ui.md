# Roadmap: the phone as the primary UI

Status: **desk work complete (2026-10-12); hardware checks open.** Every item
is built and tested on the laptop, including a Playwright phone project.
What remains needs a Pi, the truck or a real phone, plus installer wiring
that waits on another session's pending `install_pi.sh` edits. See
[Remaining](#remaining). Changes go straight to `main`, with no feature
branches.

## Goal (operator, 2026-10-12)

A user, on Android for now, should be able to:

1. Connect to the Pi over Bluetooth and tell it which WiFi network to join.
2. Open the dashboard and have it display properly on a phone screen.
3. Run a software update from the UI.
4. Monitor parameters, set collection profiles, download their ROM, and so on.

iOS is out of scope for Bluetooth. Safari has no Web Bluetooth, and
`docs/ble-provisioning.md` explains this once. Every other item here must work
in mobile Safari too, because it is just the dashboard.

## Where it stands

The 2026-10-12 survey found a desktop-first dashboard, no update path, and a
pasted token. Each row below is now built, as each item records.

| Area | Now |
|---|---|
| WiFi from a phone | BLE Improv setup with a gate and a status characteristic (P1) |
| Phone layout | Bottom bar and More sheet, condensed header, scrolling tables, sized charts (P2.2, P2.5) |
| PWA | Standalone and orientation-free, with PNG and maskable icons (P2.1) |
| Software update | Check and update from System Info, through a root path unit (P3) |
| Monitor parameters | Two-column Drive tiles; Charts one series per row with touch readout (P4) |
| Collection profiles | Capture runner and banner fit a phone (P5.1) |
| ROM download | Backup and download fit 412px, with a tap-friendly memory map (P5.2, desk part) |
| Control token on a phone | Pairing by key cycle, by a paired device, or over SSH (P2.4) |
| A panel that crashes | Contained to that panel (`PanelErrorBoundary`); the app no longer blanks |
| A daemon of another version | Pages show their load error, not a crash: `requireFields` in `api/core.ts`; `e2e/emptyDaemon.spec.ts` opens every page against `{}` responses (2026-10-13) |

## P1: onto a network from the phone (`network-and-provisioning.md` N1)

- [~] **P1.1 Improv WiFi over BLE**, with the gate and the status
  characteristic. The code, tests and `docs/ble-provisioning.md` landed on
  2026-10-12 (`ab78d54`). `install_pi.sh` installs and enables
  `canary-provision.service` (2026-10-13,
  `test_phone_setup_update_and_first_boot_units_are_installed`). Still open:
  a bench run with Android Chrome on improv-wifi.com (P1.2).
- [ ] **P1.2 Bench acceptance.** On a freshly installed Pi with no WiFi
  profile, provision from Android and confirm three things:
  - the dashboard link the provisioner returns opens;
  - after that, a second provisioning is refused until a window is opened;
  - nRF Connect reads the status characteristic.

## P2: the dashboard on a phone

- [x] **P2.1 Manifest.** (`dc8fe79`) The manifest is standalone with
  `orientation: any` and has 192, 512 and maskable icons plus an
  apple-touch-icon. `scripts/render_pwa_icons.py` renders them, and the SW
  cache is bumped. Test: `src/manifest.test.ts`. Remove the landscape lock (`"orientation": "any"`).
  Add 192 and 512 px PNG icons, plus a maskable one. Use
  `display: standalone` so Android's status bar stays visible.
- [x] **P2.2 Phone navigation.** (`b25f6d8`) `PhoneNav` and `Sheet`; More
  is derived from `NAV`, and a test pins every page as reachable exactly
  once. Alerts opens Trouble Codes, with Anomalies and Alarms on its strip.
  Below 600px the header is brand, connection and a menu. A horizontal strip of about 38 items is not
  navigation. Below about 600px, use a bottom bar with the five things done
  from a phone:
  1. Drive (live parameters)
  2. Logging (start or stop a capture profile)
  3. Alerts/DTCs
  4. ROM
  5. System

  Put the rest of `NAV` behind a grouped "More" sheet. Condense the header to
  brand, connection status and one menu.
- [x] **P2.3 System → Network panel.** (`dc8fe79`) `GET /control/network`
  reads through a cached `NetworkStatusReader`, and `/status.network` peeks
  without blocking. `NetworkPanel` has a copyable URL and the window button
  with its countdown. Tests: `tests/test_control_network.py`,
  `tests/test_provisioning_reader.py`, `NetworkPanel.test.tsx`. Show the joined SSID and the Pi's
  addresses (also from `/status`, so the phone learns the IP it should
  bookmark). Add an "Allow a phone to change WiFi for 5 min" button that calls
  `POST /control/network/provisioning/window`.
- [~] **P2.4 Pairing instead of a pasted token** (desk complete; truck check
  and installer wiring open) (operator decisions
  2026-10-12). Physical presence is the proof: a person in the car turns the
  key ACC → ON → ACC. It is ACC rather than OFF because a Pi on a switched
  lighter socket would lose power at OFF; OBD-port power needs the X1202.
  Each device gets its own revocable token, and the shared token stays for
  SSH, MCP and scripts. Nothing secret crosses Bluetooth: the Improv success
  URL is plain.
  - [x] **Key-cycle pairing** (2026-10-12).
    - `src/control/key_cycle_pairing.py`. ON is the ECM answering OBD.
      Approval needs exactly one waiting request made before ON; two
      waiting is a named conflict. ON held over 60 s, replay, passive and
      unknown never count.
    - A cycle with no request opens the BLE provisioning window for 5 min.
    - Routes: `/pair/request` (no token, request secret in a header) and
      `/control/devices`. Device tokens are accepted by the one shared auth
      dependency, so every guarded route and `/ws/control` takes them.
    - Dashboard: `PairingCard` shows while the browser has no token.
    - Tests: `tests/test_key_cycle_pairing.py`, `tests/test_pairing_routes.py`,
      `tests/test_paired_devices.py`, `PairingCard.test.tsx`,
      `pairing.test.ts`.
    - **Not yet run on the truck.** Measure how long after ACC the classifier
      leaves `ignition_only` (`FRESHNESS_THRESHOLD_SECONDS` is 10 s), and
      confirm ACC never reads as ON on the Xterra.
  - [x] **Approval from a paired device.** (`04238c3`)
    `/control/pairing/requests` and `.../approve`. The shared auth records
    the caller (`request.state.control_caller`). Paired devices see an
    approval prompt. Removing a device closes its open `/ws/control`
    socket.
    - A new request appears on every paired device over `/ws/control` ("Pixel
      7 wants to pair — approve?"). Approving it is an alternative to the key
      cycle, for adding a laptop away from the car.
    - It also surfaces a stranger's request on the owner's phone the moment
      it is made.
  - [x] **Devices panel.** System → Paired Devices: list, rename, remove,
    "this device", and waiting requests with Approve (`04238c3`).
  - [x] **Bench pairing.** `canaryctl pair [--yes | --approve ID]` and
    `canaryctl devices [--remove ID]`. canaryctl reads
    `/etc/canary/control.token` (`04238c3`).
  - [x] **A per-device secret in images.** (`57d1e73`) Before imaging,
    `scripts/prepare_image.sh` strips the token, paired devices, WiFi
    profiles, hostname, machine-id and SSH keys. `canary-firstboot.service`
    then makes them per-Pi (`tests/test_prepare_image.py`; docs/install.md
    "Making an Image to Hand Out"). `install_pi.sh` installs and enables the
    unit, and names a Pi with the same `scripts/lib/canary_hostname.sh` rule
    first boot uses (2026-10-13).
- [x] **P2.5 Tables and charts below 600px.** (`b25f6d8`) `TableScroll`
  wraps 19 tables, with a sticky first column on phones. Key/value rows
  stack. `useContainerWidth` sizes MultiSeriesChart, Sparkline, Curve and
  Heatmap. Show key/value tables as stacked
  cards. Give wide data tables (`MapTable`, ROM tables) a horizontal scroll
  container with a sticky first column, not page overflow. Size canvases from
  their container.
- [x] **P2.6 Phone viewport in e2e.** (`b25f6d8`) The Playwright `phone`
  project (Pixel 7, touch) runs the dashboard journeys and `e2e/phone.spec.ts`.
  That spec's overflow check fails on a planted 2000px element, as verified on
  2026-10-12. 20 passed, 1 desktop-only skip. Add a Playwright project at Pixel 7 size
  (412×915) running the existing dashboard journeys. Assert that no page
  scrolls horizontally on the five bottom-bar pages. This is the regression
  net for everything in P2–P5.

## P3: software update from the UI (extends `tasks/S02-pi-updater.md`)

- [~] **P3.1 A root trigger the daemon can pull.** (`ecf97de`)
  `canary-update.path` and `.service`, with `src/control/update_runner.py`
  (root, checkout venv). The request is renamed away before Python runs, and
  read with `O_NOFOLLOW` within a size cap. Status goes to
  `/run/canary/update_status` with a heartbeat. `install_pi.sh` installs
  both units and starts the path unit (2026-10-13). `canary-update` runs git
  as root with `safe.directory` passed through `GIT_CONFIG_*`. On 2026-10-13
  git 2.39.5 (Debian bookworm, a container) refused a root `rev-parse` in a
  user-owned repository without it and accepted it with it. Open: a real
  update run from the phone. The daemon cannot run sudo
  and should not be able to. Use the same pattern as the UPS keep-alive and
  the provisioning window:
  - The daemon writes `/run/canary/update_request`.
  - A root `canary-update.path` unit sees the file and starts a oneshot
    `canary-update.service`, which runs the existing updater.
  - The updater writes progress and its outcome to
    `/run/canary/update_status`.

  The updater keeps every refusal it has today: an active capture, a dirty
  checkout, a failed health check followed by rollback.
- [x] **P3.2 Endpoints.** (`ecf97de`, `tests/test_control_update_routes.py`)
  - `GET /control/update` returns the current SHA, the upstream SHA and the
    commits behind it (from a `git fetch` the root unit runs on request), plus
    the last outcome.
  - `POST /control/update` writes the request. It returns 409 while a capture
    or ECU backup is active, before anything starts.
- [x] **P3.3 UI.** (`ecf97de`, `UpdateCard.test.tsx`, including the
  reconnect-after-restart path) Add an update card on System showing:
  - what is installed, what is available, and the commit subjects;
  - an Update button;
  - progress.

  The daemon serving the page restarts mid-update, so the page has to
  reconnect, re-read `update_status`, and report "updated to X" or "rolled
  back: reason". It must not just go blank.
- [x] **P3.4 Needs internet.** (`ecf97de`) There are two checks. The route
  refuses with no default route, and the runner probes `git ls-remote` within
  20 s before updating. A WiFi client usually has it now that the AP
  is gone. With no route, say "no internet" before starting, not after a
  `git fetch` timeout. The offline bundle path (`git bundle` over the
  dashboard upload) is a follow-on.

## P4: monitor parameters

- [x] **P4.1 Drive screen in portrait.** (`b25f6d8`, e2e "two large tiles a
  row") Large tiles in two columns, with the
  pinned set chosen on the phone. The pinned set is already stored per
  device.
- [x] **P4.2 Charts at phone width.** (`b25f6d8`, `ChartsPanelPhone.test.tsx`)
  Pointer-event readout; the selection persists in `canary.charts.selected`. One series per row, with touch to read
  values (there is no hover on a phone). Keep the selected channels across
  reloads, as the Builder does.

## P5: collection profiles and ROM

- [x] **P5.1 Start, stop and switch a capture profile from the phone**
  (`b25f6d8`)
  (`CaptureRunner`), with the active profile visible on every page
  (`CaptureProfileBanner`). Editing a profile can stay desktop-first, but it
  must not be broken at phone width.
- [~] **P5.2 ROM read and download on Android.** Desk part done (`b25f6d8`):
  the card and table fit 412px, and the memory map has tap selection and
  step buttons. Open: the three on-device checks below. Run `EcuBackupCard` end to
  end on a phone:
  - the precondition refusal reads clearly (`vehicle_state` must allow it);
  - progress survives the screen sleeping;
  - the download lands in Android's Downloads with the library's filename.

  `RomMemoryMap`'s hover-only block selection needs a tap equivalent.

## Decisions taken (2026-10-12)

- **Update trigger (P3.1):** the path-activated root oneshot, as
  recommended. There is no polkit rule.
- **Phone navigation (P2.2):** the five proposed entries, with Alerts opening
  Trouble Codes and its siblings on a strip.

## Remaining

Each of these needs the Pi, the truck or a real phone:

1. **P1.2 bench acceptance.** Provision a fresh Pi from Android Chrome. Then
   confirm a second setup is refused until a window opens, and that nRF
   Connect reads the status characteristic.
2. **Key cycle on the truck (P2.4).** Measure the lag after ACC, and confirm
   ACC never reads as ON on the Xterra.
3. **A real update from the phone (P3.1).** The Pi's git honouring
   `safe.directory` through `GIT_CONFIG_*` was confirmed on git 2.39.5 in a
   container; the Pi's own git still gets the real run.
4. **P5.2 on Android.** Progress across screen sleep, and the download
   landing in Downloads.
5. **A pass on a real Android phone and in mobile Safari**, with icon masking
   on the launcher.
