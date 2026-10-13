# Roadmap: the phone as the primary UI

Status: **opened 2026-10-12**, and only P1 has code so far. Changes go
straight to `main`, with no feature branches.

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

This comes from a read-only survey of `src/dashboard/static` on 2026-10-12.

| Area | Today |
|---|---|
| WiFi from a phone | **Code landed, not yet run on the Pi** (P1 / N1.1–N1.3) |
| Phone layout | **Desktop-first, partly responsive.** `index.html` has a correct viewport. Below 900px, `styles.ts` collapses the workbench grid and turns the sidebar into a horizontal strip of about 38 items. The header packs about ten widgets. There are about 40 fixed-width tables or `<table>`s, plus canvas charts. |
| PWA | Has a manifest and a service worker, but the manifest locks `orientation: "landscape"` and ships only an SVG icon |
| Software update | **Missing from the UI and API.** It is only `sudo canary-update`. The daemon runs `NoNewPrivileges` and the only sudoers grant is the restart. |
| Monitor parameters | **Exists** (Drive screen pins, Charts, Builder). Not checked at phone width. |
| Collection profiles | **Exists** (`CaptureRunner`, `CaptureProfileEditor`). The editor is a dense desktop form. |
| ROM download | **Exists.** `EcuBackupCard` reads the ECU into the ROM library, and the library downloads via a blob. Not tested on Android. |
| Control token on a phone | **Pairing by key cycle** (P2.4): tap *Pair this device*, turn the key ACC → ON → ACC. Pasting the token still works. |

## P1: onto a network from the phone (`network-and-provisioning.md` N1)

- [~] **P1.1 Improv WiFi over BLE**, with the gate and the status
  characteristic. The code, tests and `docs/ble-provisioning.md` landed on
  2026-10-12. Still open:
  - a bench run with Android Chrome on improv-wifi.com;
  - `install_pi.sh` installing and enabling `canary-provision.service` (N2.1).
- [ ] **P1.2 Bench acceptance.** On a freshly installed Pi with no WiFi
  profile, provision from Android and confirm three things:
  - the dashboard link the provisioner returns opens;
  - after that, a second provisioning is refused until a window is opened;
  - nRF Connect reads the status characteristic.

## P2: the dashboard on a phone

- [ ] **P2.1 Manifest.** Remove the landscape lock (`"orientation": "any"`).
  Add 192 and 512 px PNG icons, plus a maskable one. Use
  `display: standalone` so Android's status bar stays visible.
- [ ] **P2.2 Phone navigation.** A horizontal strip of about 38 items is not
  navigation. Below about 600px, use a bottom bar with the five things done
  from a phone:
  1. Drive (live parameters)
  2. Logging (start or stop a capture profile)
  3. Alerts/DTCs
  4. ROM
  5. System

  Put the rest of `NAV` behind a grouped "More" sheet. Condense the header to
  brand, connection status and one menu.
- [ ] **P2.3 System → Network panel.** Show the joined SSID and the Pi's
  addresses (also from `/status`, so the phone learns the IP it should
  bookmark). Add an "Allow a phone to change WiFi for 5 min" button that calls
  `POST /control/network/provisioning/window`.
- [~] **P2.4 Pairing instead of a pasted token** (operator decisions
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
  - [ ] **Approval from a paired device.**
    - A new request appears on every paired device over `/ws/control` ("Pixel
      7 wants to pair — approve?"). Approving it is an alternative to the key
      cycle, for adding a laptop away from the car.
    - It also surfaces a stranger's request on the owner's phone the moment
      it is made.
  - [ ] **Devices panel.** System → Paired devices: list, rename, remove.
  - [ ] **Bench pairing.** `canaryctl pair` over SSH for a Pi with no vehicle.
  - [ ] **A per-device secret in images.** A pre-flashed image must not ship
    a control token. Generate it at first boot, not at image build.
- [ ] **P2.5 Tables and charts below 600px.** Show key/value tables as stacked
  cards. Give wide data tables (`MapTable`, ROM tables) a horizontal scroll
  container with a sticky first column, not page overflow. Size canvases from
  their container.
- [ ] **P2.6 Phone viewport in e2e.** Add a Playwright project at Pixel 7 size
  (412×915) running the existing dashboard journeys. Assert that no page
  scrolls horizontally on the five bottom-bar pages. This is the regression
  net for everything in P2–P5.

## P3: software update from the UI (extends `tasks/S02-pi-updater.md`)

- [ ] **P3.1 A root trigger the daemon can pull.** The daemon cannot run sudo
  and should not be able to. Use the same pattern as the UPS keep-alive and
  the provisioning window:
  - The daemon writes `/run/canary/update_request`.
  - A root `canary-update.path` unit sees the file and starts a oneshot
    `canary-update.service`, which runs the existing updater.
  - The updater writes progress and its outcome to
    `/run/canary/update_status`.

  The updater keeps every refusal it has today: an active capture, a dirty
  checkout, a failed health check followed by rollback.
- [ ] **P3.2 Endpoints.**
  - `GET /control/update` returns the current SHA, the upstream SHA and the
    commits behind it (from a `git fetch` the root unit runs on request), plus
    the last outcome.
  - `POST /control/update` writes the request. It returns 409 while a capture
    or ECU backup is active, before anything starts.
- [ ] **P3.3 UI.** Add an update card on System showing:
  - what is installed, what is available, and the commit subjects;
  - an Update button;
  - progress.

  The daemon serving the page restarts mid-update, so the page has to
  reconnect, re-read `update_status`, and report "updated to X" or "rolled
  back: reason". It must not just go blank.
- [ ] **P3.4 Needs internet.** A WiFi client usually has it now that the AP
  is gone. With no route, say "no internet" before starting, not after a
  `git fetch` timeout. The offline bundle path (`git bundle` over the
  dashboard upload) is a follow-on.

## P4: monitor parameters

- [ ] **P4.1 Drive screen in portrait.** Large tiles in two columns, with the
  pinned set chosen on the phone. The pinned set is already stored per
  device.
- [ ] **P4.2 Charts at phone width.** One series per row, with touch to read
  values (there is no hover on a phone). Keep the selected channels across
  reloads, as the Builder does.

## P5: collection profiles and ROM

- [ ] **P5.1 Start, stop and switch a capture profile from the phone**
  (`CaptureRunner`), with the active profile visible on every page
  (`CaptureProfileBanner`). Editing a profile can stay desktop-first, but it
  must not be broken at phone width.
- [ ] **P5.2 ROM read and download on Android.** Run `EcuBackupCard` end to
  end on a phone:
  - the precondition refusal reads clearly (`vehicle_state` must allow it);
  - progress survives the screen sleeping;
  - the download lands in Android's Downloads with the library's filename.

  `RomMemoryMap`'s hover-only block selection needs a tap equivalent.

## Decisions for the operator

1. **The update trigger (P3.1).** The recommendation is the path-activated
   root oneshot above. The alternative, a polkit rule letting `canary` start
   `canary-update.service`, works too, but it adds a second privilege
   mechanism next to the `/run/canary` files.
2. **Phone navigation (P2.2).** Are the five bottom-bar entries the right
   five?
