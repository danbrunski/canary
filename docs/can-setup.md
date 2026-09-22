# SocketCAN Setup

CANary has two SocketCAN operating modes:

- Active diagnostics + CAN capture: `collection.obd_source: canable`
  (default). CANary transmits diagnostic requests, ISO-TP flow-control frames,
  and any ad-hoc diagnostic/control request submitted through the control API,
  while also observing broadcast CAN frames. Do not set SocketCAN
  `listen-only` in this mode or active diagnostics cannot work.
- Passive CAN-only capture: `collection.obd_source: disabled`. CANary never
  sends frames in this mode, so SocketCAN `listen-only` is the right kernel
  guardrail.

## Bring Up `can0`

Default active diagnostics + CAN capture mode:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000
sudo ip link set can0 up
ip -details link show can0
```

The final command should show `bitrate 500000`.

Passive CAN-only mode:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
ip -details link show can0
```

The final command should show `bitrate 500000` and `listen-only on`. Also set:

```yaml
collection:
  obd_source: disabled
```

## Quick Receive Check

```bash
candump can0
```

You should see frames when the ignition is on. Stop with `Ctrl-C`.

## Persist Device Naming

For USB CAN adapters, add a udev rule that assigns a stable symlink or network
name based on the adapter serial number. Keep `canary.yaml` pointed at the
stable name instead of relying on attach order.

Example discovery:

```bash
udevadm info --attribute-walk --name=/dev/ttyACM0
```

## CANary Config

Use SocketCAN for the Raspberry Pi deployment:

```yaml
hardware:
  can:
    interface: socketcan
    channel: can0
    bitrate: 500000
```

CANary's OBD path transmits diagnostic requests and does not filter them by
service, so listen-only mode disables the whole active-OBD path. Use it only
for passive CAN-only captures.

## Idempotent Bring-up Helper

`scripts/canup.sh` is the manual bring-up wrapped idempotently — safe to
re-run, and the fastest way to recover the link by hand after a glitch:

```bash
sudo scripts/canup.sh                          # can0 @ 500000, active mode
sudo CAN_LISTEN_ONLY=1 scripts/canup.sh        # passive CAN-only
sudo CAN_IFACE=can1 CAN_BITRATE=250000 scripts/canup.sh
```

Note on `restart-ms` (bus-off auto-recovery): support depends on the
adapter driver. The mcp251x driver (Waveshare 2-CH CAN HAT, the current
in-vehicle rig) accepts it, and `systemd/can0.service` sets
`restart-ms 100`. The gs_usb driver (Canable) rejects it, which is why
`scripts/canup.sh` deliberately does *not* pass it — on a Canable,
bus-off recovery is a re-plug / `canup.sh` re-run.

## Persistent, Auto-Recovering can0 (in-vehicle)

A by-hand `ip link` bring-up does not survive a reboot. Two artifacts
make the link come back without a human:

- `systemd/can0.service` — a `oneshot` unit that configures + raises the
  link at the configured bitrate (with `restart-ms 100` for drivers that
  support it), ordered `Before=canary.service`. This covers the SPI CAN
  HAT, which enumerates at boot and never "re-plugs".
- `systemd/70-canable-can0.rules` — a udev rule that (re)starts
  `can0.service` every time the `can0` net device appears. This is the
  Canable/USB half of the story: a mid-drive USB re-enumeration or
  brown-out auto-recovers the link. Harmless when the HAT is the
  adapter.

```bash
sudo install -m0644 systemd/can0.service /etc/systemd/system/can0.service
sudo install -m0644 systemd/70-canable-can0.rules \
        /etc/udev/rules.d/70-canable-can0.rules
sudo udevadm control --reload
sudo systemctl daemon-reload
sudo systemctl enable --now can0.service
ip -details -brief link show can0          # expect: UP, can <bitrate>
```

This is the link-layer half of the resilience story; the recorder's
adapter-level auto-reconnect (`scripts/record.py --obd-source canable`,
default on) rebuilds the python-can bus on top of the recovered link. If
your Canable enumerates as `canN`, mirror the rule/unit for that name or pin
a stable name via the serial-based udev rule above.

## Aux sensor bus (add-on CAN gauges)

A second, private CAN interface for sensors CANary adds to the truck — first the
AEM X-Series fuel-pressure gauge (AEMnet: 500 kbit/s, 29-bit ID `0x000A0301`,
20 Hz). It is not the vehicle bus: nothing else is on it, and its frames are
recorded as `sensor:<NAME>`, never in the vehicle's `can:` namespace. The
hardware is either a second on-board channel (CANary HAT rev 2.1 CAN1) or a USB
CAN adapter; config names the interface, the code does not care which
(`docs/CANary-HAT-Rev2.1.md` §13.5.4).

- **Active mode, never listen-only.** The gauge is the only other node; if
  nothing ACKs its frames it retransmits until error-passive.
- **Termination at both ends.** The gauge has none. One 120 Ω at the Pi end
  (HAT JP2, or the adapter's jumper) and one in the harness at the gauge.
- **Bring-up** (until a per-interface unit exists):
  `sudo CAN_IFACE=can1 CAN_BITRATE=500000 scripts/canup.sh`, then
  `candump can1` should show `0A0301` about 20 times a second.
- **Enable in config:**

  ```yaml
  hardware:
    sensor_can:
      enabled: true
      channel: can1          # or the USB adapter's interface name
      bitrate: 500000
      signals: data/sensor_can_signals.yaml
  ```

- **Verify the layout before trusting it.** `data/sensor_can_signals.yaml`
  carries `verified: false` for the AEM gauge until a `candump` beside the
  gauge's own display confirms bytes 0–1 big-endian at 1 kPa/bit; the daemon
  logs the unverified signals at start-up.

