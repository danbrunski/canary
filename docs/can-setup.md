# SocketCAN Setup

CANary has two SocketCAN operating modes:

- Active read-only OBD + CAN capture: `collection.obd_source: canable`
  (default). CANary transmits standard diagnostic read requests for Mode 01 /
  configured Mode 22 data and passively observes broadcast CAN frames. Do not
  set SocketCAN `listen-only` in this mode or OBD polling cannot work.
- Passive CAN-only capture: `collection.obd_source: disabled`. CANary never
  sends frames in this mode, so SocketCAN `listen-only` is the right kernel
  guardrail.

## Bring Up `can0`

Default active read-only OBD + CAN mode:

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

CANary's OBD path enforces read-only diagnostic services and rejects write /
actuation modes, but it still has to transmit diagnostic read requests. Use
listen-only only for passive CAN-only captures.

## Idempotent Bring-up Helper

`scripts/canup.sh` is the manual bring-up wrapped idempotently — safe to
re-run, and the fastest way to recover the link by hand after a glitch:

```bash
sudo scripts/canup.sh                          # can0 @ 500000, active mode
sudo CAN_LISTEN_ONLY=1 scripts/canup.sh        # passive CAN-only
sudo CAN_IFACE=can1 CAN_BITRATE=250000 scripts/canup.sh
```

It sets `restart-ms 100` so the controller auto-recovers from a CAN bus-off
event (electrical noise / transient short on the vehicle bus).

## Persistent, Auto-Recovering can0 (in-vehicle)

A by-hand `ip link` bring-up does not survive a reboot or a mid-drive USB
re-enumeration of the Canable (the in-vehicle analogue of the WSL `usbipd`
drop). Two artifacts make the link come back without a human:

- `systemd/can0.service` — a `oneshot` unit that configures + raises the
  link at the configured bitrate, ordered `Before=canary.service`.
- `systemd/70-canable-can0.rules` — a udev rule that (re)starts
  `can0.service` every time the `can0` net device appears, so a re-plug or
  brown-out auto-recovers the link.

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
