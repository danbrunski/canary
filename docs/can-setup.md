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
