# Roadmap — networking, discovery, and headless provisioning

Status: **partially landed** (2026-09-11; BLE provisioning code 2026-10-12). Straight to `main`, no feature
branches.

## Why this exists

Until 2026-09-11 the Pi served its own WiFi access point (`CANary`,
`10.42.0.1`) full time and got internet from a laptop over an Ethernet
cable running Windows Internet Connection Sharing. That arrangement had
three problems, in increasing order of importance:

1. ICS is unreliable, and it was the *only* internet path. Losing it lost
   `apt`, `git pull`, and clock sync at once.
2. The Pi has **one** WiFi radio, so AP mode and client mode are exclusive.
   Serving the AP meant the Pi could never join a network that had
   internet, and the radio cannot even *scan* while it is beaconing. Every
   AP-to-client transition therefore costs an outage, and a supervisor has
   to arbitrate.
3. Nothing in `scripts/install_pi.sh` configures networking at all. Every
   adopter reassembles it from prose in `docs/pi-hotspot.md`.

The operator now has Starlink in the vehicle, which changes the premise:
there is usually a LAN with internet on it, in the truck and in the garage.
A Pi that *joins* that LAN is reachable by any phone or laptop already on
it, so the AP's only remaining job is the case where no LAN exists at all.

## The decided architecture (operator-approved 2026-09-11 — do not re-open)

> **Amended by the operator, 2026-11-07:** the built-in radio *may* serve a
> hotspot, but only as a **setup and fallback** hotspot (N6): when no saved
> network is reachable. It never runs alongside the client. The full-time
> AP's problems came from switching between AP and client while someone
> depended on both; a hotspot that exists only when there is no client
> network has nothing to switch away from. Why: a user away from every saved
> network, with only a phone, otherwise has no way to reach the Pi at all
> (BLE needs Android Chrome; iPhones have no Web Bluetooth). The rest of this
> section stands.

Each transport gets one job, matched to its bandwidth:

- **WiFi is always a client.** It joins a known network for internet and for
  LAN reachability. It never serves an AP on the built-in radio. This
  deletes the AP/client supervisor, the scan blackout, the anti-flap
  backoff, and the stranding watchdog — the entire state machine.
- **Ethernet is a direct laptop link** for bulk transfer and recovery. It
  carries no default route and no internet.
- **Bluetooth is a provisioning and status channel.** Not a network, not a
  dashboard transport, not a fallback data path.
- **A second WiFi adapter is optional and purely additive.** If present it
  serves the `CANary` AP on its own radio, which restores a fixed address
  and phone access with no LAN. Nothing in the base design needs to know
  whether it is installed.

Rejected, with reasons, so they are not re-proposed:

- *AP and client concurrently on the built-in radio.* Same-channel only and
  unreliable on this chipset.
- *Bluetooth PAN as the phone's dashboard transport.* iOS cannot join a PAN
  served by another device, and the phone's own hotspot does the same job
  faster using the WiFi client that already exists.
- *Keeping ICS.* It was the original complaint.

## What already landed (2026-09-11, bench)

- [x] **N0.1 Pi switched from AP to WiFi client.** `canary-ap` kept as a
  profile but `autoconnect=no` and down. Two client profiles created,
  `canary-home` (priority 100) and `canary-starlink` (priority 50), both
  with `psk-flags=0` so they reconnect at boot without an agent.
- [x] **N0.2 Ethernet demoted to a data link.** `ipv4.never-default yes`,
  gateway and DNS cleared on `Wired connection 1`. The default route is now
  the WiFi client. The Pi keeps `192.168.137.2` on `eth0`.
- [x] **N0.3 Hostname aligned with the docs.** The host was `canarypi`
  while 11 files in the repo say `canary.local`, which only resolved
  because of a hand-added `/etc/hosts` entry on the operator's laptop
  pointing at the *Ethernet* address. Renamed to `canary`, with
  `preserve_hostname: true` in `/etc/cloud/cloud.cfg` so cloud-init does
  not revert it. `canary.local` is now a real mDNS name.

## Open work

### Provisioning and discovery

- [~] **N1.1 Bluetooth LE provisioning service.** *Code landed 2026-10-12, not
  yet run on the Pi:* `src/provisioning/` (Improv BLE v2.2 over BlueZ with
  dbus-fast, joins through NetworkManager keyfiles so the passphrase never
  reaches an argv, a failed join restores the profiles),
  `systemd/canary-provision.service`, operator guide `docs/ble-provisioning.md`,
  tests `tests/test_provisioning_*.py`. Open: a bench run against a real
  phone (Android Chrome on improv-wifi.com). `install_pi.sh` installs and
  enables the unit (2026-10-13).
  Original scope: A BlueZ GATT server that
  advertises, lists the networks the Pi can currently see, accepts an SSID
  and passphrase, hands them to NetworkManager, and reports the resulting
  address. This is the only way to put a Pi onto an unfamiliar network from
  a phone with no cable and no AP, and it is also how an adopter with a
  fresh install gets on their home WiFi at all without an SSH session.
  Implement the **Improv WiFi** GATT protocol rather than inventing one:
  it is an open standard with a browser-based provisioner that speaks it
  over Web Bluetooth, so there is no app to write and none to install. Its
  success response carries a URL, which closes discovery in the same step
  by sending the browser straight to the dashboard.
  Note the free win: because WiFi is now always a client, the Pi can scan
  whenever it likes, so the user picks from a real list instead of typing a
  name. Under the old AP-by-default design that scan cost an outage.
- [~] **N1.2 Gate the provisioning service so it is not an open write.**
  *Landed with N1.1 (`src/provisioning/gate.py`):* open while no WiFi client
  profile exists or while a window (`POST /control/network/provisioning/window`,
  at most 30 min, or `python -m src.provisioning open-window`, or a key cycle
  ACC → ON → ACC with no pairing request waiting) is open; otherwise
  Improv's *authorization required*. Fails closed when NetworkManager cannot
  be queried. The service keeps advertising when locked so N1.3 stays
  reachable. Open: a dashboard button for the window (phone-ui P2.3).
  The Improv success URL is plain; devices pair by key cycle (phone-ui P2.4).
  Original scope:
  A service that accepts network credentials over the air and creates
  profiles is reachable by anyone in range. Advertise unconditionally only
  while no WiFi profile is configured; afterwards only inside a short
  window opened by someone who already has access. The first provisioning
  **cannot** require the control token, because obtaining the token
  requires already being on the network being joined — so the
  unprovisioned-is-open model is forced, and the post-provisioning gate is
  where the restriction goes.
- [~] **N1.3 Status characteristic.** *Landed with N1.1
  (`src/provisioning/status.py`):* JSON with mode, SSID, every address, an
  IP-based dashboard URL, and the gate state. Open: bench verification.
  Original scope: Expose current mode, joined SSID, and
  every current address, readable without provisioning rights. This is the
  answer to "the Pi joined something and I do not know where it went", and
  it is the out-of-band recovery path that makes a client-only design safe:
  with no AP, a Pi that fails to join is otherwise silent and reachable
  only by cable.
- [ ] **N1.5 Do not let phone access depend on mDNS alone.** iOS resolves
  `.local` natively via Bonjour, but **Android's platform resolver has
  historically not resolved `.local` hostnames for ordinary apps**, which is
  why Chrome on Android has long failed to open `.local` URLs. Newer Android
  versions improved this and it varies by OEM build, so it is not a
  dependable base. Every path that gets a phone to the dashboard therefore
  needs a non-mDNS answer:
  - On the optional AP (N3.1) the Pi owns DNS, so pin the name in
    NetworkManager's shared dnsmasq (`address=/canary.local/10.42.0.1`) and
    AP clients never use multicast at all.
  - On a foreign LAN there is no such control, which is what makes the BLE
    status characteristic (N1.3) load-bearing rather than a convenience.
  - A DHCP reservation on the home router plus a bookmarked IP covers the
    garage case on any phone. The Starlink router cannot make reservations.

- [x] **N1.6 A laptop finds the Pi on its own (companion mode).** *Done
  2026-10-16.* The daemon answers `GET /identity` (no token: product, hostname,
  `host_kind` from the device tree, profile, version, port), and
  `src/companion/discovery.py` finds daemons from the laptop side in priority
  order: `CANARY_HOST`, the conventional names where resolvable (not on WSL,
  N4.3), the wired `192.168.137.2`, then a bounded sweep of each attached
  /24. The desk daemon runs it once at startup and links to a lone find;
  `GET /companion/discover` runs it again. `install_pi.sh` now also writes
  `/etc/avahi/services/canary.service` (`_canary._tcp`, port 8000) so a
  Bonjour-capable client can browse for a CANary by service. Not done: a
  laptop-side mDNS browse (needs `zeroconf` or avahi tools, neither
  installed; the sweep covers WSL) and N1.5's phone paths.
- [x] **N1.4 Document the iOS limitation.** `docs/ble-provisioning.md`
  "iPhone" (2026-10-12). Safari implements no Web
  Bluetooth, so an iPhone needs a native client or a third-party browser
  that does. Every Bluetooth path in this design has the same hole; say so
  once, in `docs/`, rather than letting each reader rediscover it.

### Saved WiFi networks in the dashboard (N5, operator 2026-11-07)

The Network page shows what the Pi joined and nothing else, so a second
network, a phone hotspot saved ahead of time, or a typo'd one could only be
handled with `nmcli` over SSH. NetworkManager already keeps any number of
saved networks and auto-joins the best one in range; this exposes that.

**Privilege (decided):** the root `canary-provision` service is the one owner
of WiFi state. It already writes keyfiles without putting a passphrase on an
argv, and joins with a journalled rollback (`src/provisioning/network.py`).
The unprivileged daemon asks it over a local socket, the same "daemon asks,
root acts" shape P3.1 chose for updates over a polkit rule. The fallback
hotspot (N6) lives in the same service.

- [ ] **N5.1 Network-control socket in `canary-provision`.** A Unix stream
  socket, `/run/canary/network.sock`, root:canary 0660, one JSON request and
  one JSON reply per line, with `SO_PEERCRED` refusing a peer outside the
  canary group. Operations: `list`, `scan`, `add`, `connect`, `forget`,
  `order`. The socket is served whether or not BLE is advertising.
  Passphrases arrive only in `add` and are never logged, echoed, or
  returned.
- [ ] **N5.2 Saved-network operations in `src/provisioning/network.py`.**
  - `list`: every WiFi *client* profile NetworkManager holds, including ones
    CANary did not write (the Imager's `preconfigured`), but never `mode=ap`.
    Fields: id (connection UUID), SSID, priority, autoconnect, hidden,
    security, whether it is active, in range now (with signal), and
    `managed_by` (`canary` for `canary-wifi-*`, else `other`). Never a secret.
  - `add` with `join: true`: today's verified join, undone on failure.
  - `add` with `join: false` ("save for later"): write and load the profile
    without activating it. This is how a phone hotspot or a network
    somewhere else gets saved. It is validated the same way, so a
    passphrase of the wrong length is refused before it is written.
  - `connect`: activate a saved profile now.
  - `forget`: delete the profile. Refuse an id that is not a WiFi client
    profile, so this can never delete `canary-wired` or an AP profile.
  - `order`: given ids in priority order, set `autoconnect-priority` so the
    first wins (for example 100, 90, 80, ...), in one request.
  Tested against recorded `nmcli` output, as the existing join is.
- [ ] **N5.3 Daemon routes behind the control token**, in
  `src/dashboard/routes/control/network.py`:
  - `GET /control/network/wifi`: saved networks plus the last scan.
  - `POST /control/network/wifi/scan`: rescan, then the same payload.
  - `POST /control/network/wifi`: body `{ssid, password?, hidden?, join}`.
  - `POST /control/network/wifi/{id}/connect`
  - `PUT /control/network/wifi/order`: body `{ids: [...]}`.
  - `DELETE /control/network/wifi/{id}`
  Each mutation publishes `network.wifi_changed` on `/ws/control` and lands
  in the control ledger without the passphrase. **An empty result says which
  kind of empty it is:** the service not running (a laptop dev install, or
  `canary-provision` down) is a 503 naming the socket, never an empty list.
  Documented in `docs/headless-api.md`.
- [ ] **N5.4 "Saved WiFi networks" on the Network page.** It lists the saved
  networks in priority order with in-range and connected markers, and
  up/down to reorder. **Add network** picks from the scan or takes a typed
  name (a hidden network, or one not here), with a passphrase field, then
  **Join now** or **Save for later**. **Forget** asks first, and **Connect**
  switches now. Joining or forgetting the network this browser is reaching
  the Pi through warns first: the page will drop, and the Pi will be at its
  new address. Works at phone width. vitest covers each control, and e2e
  covers the add flow at 412 px.
- [ ] **N5.5 Docs.** The Network page in `docs/pi-setup.md`, plus
  `docs/ble-provisioning.md`, which says a second network can now be added
  from the dashboard, without a provisioning window.

### Setup and fallback hotspot (N6, operator 2026-11-07)

For a user with only a phone: at first setup, and anywhere no saved network
reaches. The phone's WiFi list shows `CANary-XXXX setup`; joining it opens a
captive portal, which is the N5 Network page served on the hotspot.

- [ ] **N6.1 Adopt or follow an established implementation.** Evaluate
  comitup (Debian, built for a Pi) and balena wifi-connect before writing
  a supervisor. Record the choice and why here.
- [ ] **N6.2 When it runs.** Up when no saved network has been reachable for
  about 2 minutes (and on a first boot with none saved). Scan first and
  cache the list, because the radio cannot scan while it beacons. While it
  is up *and no client is connected to it*, drop it every few minutes for a
  short scan, and rejoin a saved network that is back in range. Never drop
  it under a connected client.
- [ ] **N6.3 Captive portal.** The hotspot hands out DNS that answers every
  name with the Pi, so iOS and Android open their sign-in sheet on their own,
  and that sheet is the Network page. Pin `canary.setup` (or similar) as a
  name too, since mDNS is unreliable on phones (N1.5).
- [ ] **N6.4 Who may change WiFi through it.** Open while no network is saved,
  the N1.2 rule. Otherwise changing WiFi from the portal needs the key
  cycle ACC -> ON -> ACC, physical presence in the truck, as pairing does,
  or a control token. A bench Pi with no truck uses the cable or SSH.
- [ ] **N6.5 "Add your phone's hotspot" as the portal's last step**, with
  N5's "save for later". With the phone's hotspot saved, the Pi works
  anywhere the phone does, with no hotspot needed at all.
- [ ] **N6.6 Off switch.** A dashboard toggle turns the fallback hotspot and
  BLE advertising off, for a user who wants nothing listening once set up.
  The UI says the cost plainly: a Pi with both off and no saved network in
  range can only be reached by cable.

### Installer ownership

- [ ] **N2.1 `scripts/install_pi.sh` configures networking.** It currently
  sets up the clock and GPS and touches no network state, so the whole
  arrangement above exists only as commands someone ran once. It should
  set the hostname, enable avahi, install `iw` and `avahi-utils`, set the
  WiFi regulatory domain, materialise the client profiles, and apply
  `never-default` to the wired connection.
  *Hostname done (2026-10-11):* a first install names the Pi `canary-XXXX`
  from its wired MAC, so two CANaries on one LAN no longer race for
  `canary.local` (docs/pi-setup.md). A fallback AP's SSID should carry the
  same suffix. *Wired link done (2026-11-06):* the installer writes the
  `canary-wired` profile, `192.168.137.2/24` with `never-default`
  (docs/pi-setup.md "Wired address"). The rest of this item is open.
  **WiFi passphrases must not land in a repo-tracked config file.** Keep
  the network list under `/etc/canary/` beside the control token, or let
  NetworkManager hold the secrets in its own store and have the installer
  create only the profiles.
- [ ] **N2.2 Rewrite `docs/pi-hotspot.md`.** It teaches ICS as the field
  internet path and its single-radio warning drives a design that no longer
  applies. Both premises are now false.

### Optional second adapter

- [ ] **N3.1 Support an optional USB WiFi adapter serving the AP.** Detect
  a second wireless device and bring up the `CANary` profile on it; do
  nothing when absent.
  **Put the AP on the built-in chip and the client on the dongle**, not the
  reverse. AP duty is where driver support actually varies and the built-in
  part is already known to work in that role; client duty asks almost
  nothing of a driver. That means adopters can buy essentially any adapter
  instead of us maintaining a compatibility list. The cost is that
  installing the adapter *swaps* roles rather than only adding one, so pin
  each profile to a specific radio by MAC — USB wireless interface naming
  is not stable across boots.
- [ ] **N3.2 Band split.** AP on 2.4 GHz for range inside a vehicle, client
  on 5 GHz where the network offers it, so the two radios do not contend
  and Bluetooth coexists better on the shared 2.4 GHz front end.

### Ethernet and the laptop side

- [ ] **N4.1 Decommission ICS on the Windows laptop.** The Pi no longer
  needs it. Note the consequence before doing it: ICS is what assigns
  `192.168.137.1` to the Windows adapter, so disabling it strands
  `192.168.137.2` unless the laptop is given a static address on that
  subnet or both ends move to link-local.
- [ ] **N4.2 Move the wired link to link-local.** `ipv4.method link-local`
  on both ends self-assigns with no DHCP server anywhere and `canary.local`
  resolves over the direct link with nothing configured on the laptop. This
  is what makes the cable a real recovery channel rather than one that
  depends on Windows still offering DHCP.
- [ ] **N4.3 Give the laptop mDNS.** WSL has `libnss-mdns` and
  `avahi-daemon` uninstalled and `hosts: files dns` in
  `/etc/nsswitch.conf`, so it cannot resolve `.local` at all. Its
  `/etc/hosts` is a manual file (`generateHosts = false`) still pinning
  `canary.local` to the Ethernet address. Install the packages, add
  `mdns4_minimal` to nsswitch, and delete the stale entry — otherwise every
  doc that says `canary.local` keeps resolving to the wrong interface.

## Non-goals

- Serving the dashboard over Bluetooth.
- Any AP on the built-in radio while it is also the client. (The setup/fallback hotspot, N6,
  runs only when the client has no network.)
- A DHCP reservation scheme; mDNS plus the optional AP's fixed address
  cover addressing, and the Starlink router cannot make reservations
  anyway.
