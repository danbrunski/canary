# SocketCAN Listen-Only Setup

Production CAN capture should use SocketCAN in listen-only mode so CANary can
observe the bus without transmitting frames.

## Bring Up `can0`

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
ip -details link show can0
```

The final command should show `bitrate 500000` and `listen-only on`.

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

CANary's collector code never calls `send()`, but listen-only mode is still the
hardware/kernel guardrail that prevents accidental bus transmission.
