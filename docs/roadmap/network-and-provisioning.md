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
  phone (Android Chrome on improv-wifi.com), and installer wiring (N2.1).
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

- [x] **N1.4 Document the iOS limitation.** `docs/ble-provisioning.md`
  "iPhone" (2026-10-12). Safari implements no Web
  Bluetooth, so an iPhone needs a native client or a third-party browser
  that does. Every Bluetooth path in this design has the same hole; say so
  once, in `docs/`, rather than letting each reader rediscover it.

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
  same suffix. The rest of this item is open.
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
- Any AP on the built-in radio while it is also the client.
- A DHCP reservation scheme; mDNS plus the optional AP's fixed address
  cover addressing, and the Starlink router cannot make reservations
  anyway.
