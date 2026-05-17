# Firmware

## `canable2_fw-ba6b1dd.bin` — CANable2 candleLight (gs_usb)

The firmware the Pi deployment's USB-CAN adapter runs. **Do not flash slcan
firmware to the Pi adapter** — the stock `systemd/can0.service` + udev
automation only works with a native `can0` net device, which only
gs_usb/candleLight firmware creates.

| | |
|---|---|
| Device | CANable2 (STM32G431, USB `1d50:606f` when running) |
| Source | `github.com/normaldotcom/candleLight_fw` @ `ba6b1dd` (2021-07-16) |
| Build target | `canable2` (`canable2_fw`) |
| sha256 | `0adcb0bf71b28089f652bf5be6ca6f8c466e1f9edba14cb6d29959b3793d70fb` |
| Size | 17392 bytes |

This is the exact source behind canable.io's `canable2_fw-ba6b1dd.bin`. We
build and commit it ourselves because canable.io has **no verified download**
(its TLS certificate expired 2026-01-06) and `normaldotcom/canable2-fw`
publishes no release artifacts.

Reproduce byte-for-byte:

```bash
sudo apt-get install -y gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi cmake git
scripts/build_canable_fw.sh          # clones, builds, sha256-verifies vs this file
```

Flash and full rationale: **`docs/canable-firmware.md`**.
