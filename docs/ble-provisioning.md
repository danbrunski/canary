# Putting the Pi on WiFi from a phone (BLE provisioning)

The Pi's WiFi is always a client (`docs/roadmap/network-and-provisioning.md`).
It joins a network and never serves one. To get it onto a network it has not
seen before, with no cable and no SSH session, it runs a Bluetooth LE service
that speaks **Improv WiFi** (https://www.improv-wifi.com), an open standard.
Improv already has a browser-based provisioner, so there is no app to install
on Android.

The service is `canary-provision.service`. It runs as root, independently of
the daemon, from `python -m src.provisioning run`. The code is
`src/provisioning/`.

## Android (Chrome)

1. Turn on Bluetooth (and Location, which older Android versions require for
   BLE scanning).
2. In Chrome, open **https://www.improv-wifi.com** and press *Connect device
   to Wi-Fi*. Chrome's chooser lists nearby Improv devices. The Pi shows up
   under its hostname (`canary-XXXX`).
3. Pick the network from the list the Pi scanned, then enter the passphrase.
4. When the join succeeds, the page offers a link to the dashboard,
   `http://<address>:8000/`. Open it.
5. The dashboard shows **Pair this device**. Tap it, then from the driver's
   seat turn the key **ACC → ON → ACC**. The card ticks off each position as
   the Pi sees it, and when it is done the phone has its own control token.

There is no token to paste, no keyboard or monitor, and nothing secret sent
over Bluetooth.

That link is an IP address on purpose. Android does not reliably resolve
`.local` names (N1.5), so do not rely on `canary.local` from a phone. Bookmark
the address, or read it again later from the status characteristic (below).

Web Bluetooth works only on HTTPS pages. That is why the provisioner is a
public page and not part of the Pi's dashboard: the dashboard is plain HTTP on
a network the phone has not joined yet. Improv's web SDK (`improv-wifi-sdk`)
can be self-hosted on any HTTPS origin, such as GitHub Pages, if you would
rather not depend on improv-wifi.com.

Home Assistant also discovers Improv BLE devices and can provision them.

## iPhone (N1.4)

**Safari implements no Web Bluetooth, so the improv-wifi.com page does not work
on an iPhone.** Every Bluetooth path in this design has the same gap. The
workarounds:

- A third-party browser that implements Web Bluetooth, such as Bluefy, runs
  the same page.
- A generic BLE app, such as nRF Connect, can read the status characteristic,
  so it can tell you where the Pi is. It cannot practically provision the Pi.
- Without either, use the Ethernet cable or another device.

## Pairing by key cycle

A device without a control token asks to pair (`POST /pair/request`). The
person in the car then turns the key ACC → ON → ACC. The Pi tells the
positions apart by the ECM: it answers OBD at ON (`vehicle_state`
`ignition_only` or `engine_running`) and is silent at ACC. Nobody outside the
car can produce that, so it is the proof. The code is
`src/control/key_cycle_pairing.py`.

- **ACC, never OFF.** Most users power the Pi from a switched lighter socket,
  where OFF would power it down mid-gesture. The card says so.
- **One request, one approval.** A cycle approves a request only when exactly
  one is waiting and it was made before the key reached ON. Two waiting
  requests means both fail, and each phone sees the other's name ("2 devices
  asked to pair at once (Android phone, Galaxy S23)"). A stranger who asks at
  the same moment causes a retry, never a silent approval.
- **Not a gesture you make while driving.** ON held longer than 60 s does not
  count. Requests expire after 2 minutes.
- **Only the real car counts.** A replay, passive mode, an unclassifiable
  state, or no live data all show "cannot see the key" and never complete a
  cycle. On a bench with no vehicle, pair over SSH instead.
- **A cycle with no request waiting opens BLE setup** for 5 minutes. That is
  how to change WiFi later, needing nothing but the car.

Each paired device has its own token. The Pi stores only a hash of it, in
`paired_devices.json` beside the control state, written so a power cut cannot
tear it. **System → Paired Devices** in the dashboard lists them, renames
them and removes a lost one (`GET`/`PATCH`/`DELETE /control/devices`). The
shared token (`/etc/canary/control.token`) stays the key for SSH, the MCP
server and scripts.

### Approving from a paired device, or on the bench

Something that already holds a token can vouch for a new device instead of
the key. It picks the request by name, so the key cycle's two-at-once rule
does not apply.

- **From a paired phone or laptop.** Every page of a paired dashboard shows
  "Android phone wants to pair — Approve / Ignore" the moment a request
  arrives (control event `pairing.requested` on `/ws/control`). The Paired
  Devices panel lists the waiting requests too. Ignore hides it on that
  device only; it still expires after 2 minutes.
- **On a bench Pi with no vehicle**, over SSH on the Pi itself:

  ```bash
  scripts/canaryctl.py --host localhost:8000 pair            # list; approve a lone one after a yes
  scripts/canaryctl.py --host localhost:8000 pair --approve <id>
  scripts/canaryctl.py --host localhost:8000 devices [--remove <id>]
  ```

  On the Pi, `canaryctl` reads `/etc/canary/control.token` when no token is
  set. With several requests waiting it never guesses: name one with
  `--approve`.

The new device's `method` records who approved it: `key-cycle`,
`approved-by:<device id>`, or `approved-by:shared-token`
(`GET /control/pairing/requests`,
`POST /control/pairing/requests/{id}/approve`).

## Where did it go? The status characteristic (N1.3)

Beside the Improv service there is a second service, CANary status
(`45e00fc6-5b80-4fcc-9a05-be255d9ef23e`), with one read-only characteristic
(`45e00fc7-…`). Anyone in range can read it, with or without provisioning
rights. It holds compact JSON, which nRF Connect shows as text:

```json
{"v":1,"host":"canary-1a2b","mode":"client","ssid":"Starlink",
 "addr":{"wlan0":["192.168.1.23"],"eth0":["192.168.137.2"]},
 "url":"http://192.168.1.23:8000/","prov":"locked","win":0}
```

| Field | Meaning |
|---|---|
| `mode` | `client` (joined a network), `ap` (serving an AP), `disconnected`, or `no-wifi` |
| `prov` | What a WiFi write would get right now: `unprovisioned` (open), `window` (open until `win` seconds from now), or `locked` |

On the Pi itself, `.venv/bin/python -m src.provisioning status` prints the same
JSON, and `… scan` lists the networks a phone would be offered.

## Who may change the network (N1.2)

A service that accepts credentials over the air can be reached by anyone in
radio range, so it opens only in two cases:

- **The Pi has no WiFi client profile.** This covers a fresh install. Nobody
  can hold the control token yet, so this first provisioning has to be open.
- **A provisioning window is open.** Turning the key ACC → ON → ACC with no
  pairing request waiting opens one for 5 minutes. Someone with the control
  token can also open one for up to 30 minutes, either through the API:

  ```bash
  curl -X POST -H "Authorization: Bearer $CANARY_CONTROL_TOKEN" \
       -H 'Content-Type: application/json' -d '{"minutes": 5}' \
       http://<pi>:8000/control/network/provisioning/window
  ```

  or from a shell on the Pi:
  `sudo .venv/bin/python -m src.provisioning open-window -m 5`
  (`close-window` ends it early).

At any other time the Improv state is *authorization required*. The
provisioner then shows "authorize the device", and a WiFi write is refused
with *not authorized*. If the Pi cannot tell whether a profile exists (for
example, NetworkManager is not answering), the gate stays closed.

The window is a single file, `/run/canary/provision_window`, holding the epoch
at which it closes. The daemon writes it and the root service reads it, the
same contract as the UPS keep-alive hold.

### What this does not protect

- **The passphrase crosses the air unencrypted.** Improv has no encryption,
  so someone recording BLE traffic nearby during a provisioning can capture
  the WiFi passphrase. Provision where that does not matter, or change the
  passphrase afterwards on a network where it does.
- **An open Pi can be pointed at any network.** While the Pi has no network
  (or a window is open), anyone in Bluetooth range can set its WiFi, so a
  stranger could put it on theirs. That gains them no control: pairing still
  needs the key cycle in your car. You would notice the Pi missing, and turn
  the key ACC → ON → ACC to reopen setup and set it up again.
- **An accidental cycle while a stranger's request waits.** If someone on the
  Pi's network asks to pair, and within 2 minutes you happen to turn ACC →
  ON → ACC with no request of your own, their request is approved. A paired
  phone shows every request as it arrives, so you see the stranger's name
  before you turn the key; check System → Paired Devices afterwards if that
  worries you.

## What a join does

The service writes a NetworkManager keyfile,
`/etc/NetworkManager/system-connections/canary-wifi-<hash>.nmconnection`
(root, mode 0600). It loads the file and brings the profile up with
`nmcli --wait`. The passphrase goes only into that file, never onto a command
line where other users could read it in `/proc`. Provisioning the same SSID
again replaces the same profile.

A join that fails, for example because of a wrong passphrase or a timeout,
puts everything back as it was. A new profile is deleted, and a replaced one
is restored. A typo does not persist, and it does not count as a configured
network that would close the gate. A join can also be cut short, by a crash,
a restart or a power cut during `connection up`. A journal file left beside
the profile marks that case, and the next start undoes the join unless that
network came up.

The service refuses:

- **WEP and enterprise (802.1X) networks.**
- **WPA passphrases that are not 8–63 printable ASCII characters or 64 hex
  digits.**

A network missing from the scan is treated as hidden. It gets `hidden=true`,
and WPA-PSK if a password was given.

Two known rough edges:

- **Same SSID as a hand-made profile** (such as `canary-home`): a provisioned
  profile for that SSID sits beside the existing one rather than replacing it.
  NetworkManager tries the higher-priority one first.
- **Captive portals** (hotel or café sign-in pages) are not handled.

## Installing it (manual until N2.1)

`scripts/install_pi.sh` does not install this unit yet. On the Pi:

```bash
cd ~canary/canary && .venv/bin/pip install -e .   # pulls in dbus-fast
sudo install -m 0644 systemd/canary-provision.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now canary-provision
journalctl -u canary-provision -f
```

It needs BlueZ (`bluetooth.service`, present on Raspberry Pi OS) and
NetworkManager. Optional settings, set in `/etc/canary/canary.env`:

| Variable | Purpose |
|---|---|
| `CANARY_PROVISION_ADAPTER` | Bluetooth adapter to use, e.g. `hci0` |
| `CANARY_PROVISION_WIFI_IFACE` | WiFi interface to manage |
| `CANARY_DASHBOARD_PORT` | Port used in the returned URL |
| `CANARY_PROVISION_JOIN_TIMEOUT` | Join timeout in seconds |

The module docstring of `src/provisioning/__main__.py` lists them all.

## Protocol notes

These are for whoever touches `src/provisioning/`:

- Improv BLE v2.2. The service advertises its UUID and the 6-byte service data
  (`0x4677`: state, capabilities) in the advertising packet itself, which
  fills all 31 bytes. The hostname goes in the scan response. When the state
  changes, the advertisement is re-registered.
- Capabilities: *device info* and *scan WiFi*. *Identify* is not offered
  (there is nothing to blink) and *hostname* is not offered (the installer
  names the Pi).
- A scan result is one RPC result frame, the form Home Assistant's client
  parses. It is cut to fit the 255-byte length field and the negotiated MTU,
  keeping the strongest networks.
- The error characteristic is reset to *no error* on every frame, so a
  repeated error still notifies. Clients wait on that notification.
