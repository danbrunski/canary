# CANable Firmware (candleLight / gs_usb)

This doc covers the **Canable USB path**. The current in-vehicle rig is
the Waveshare 2-CH CAN HAT (MCP2515/SPI, mcp251x driver), which needs no
firmware management; the Canable remains a fully supported option for
bench work, second-vehicle use, and fallback — this doc applies whenever
one is the adapter.

When used, the CANable2 (STM32G431) **must run candleLight (gs_usb)
firmware**, not slcan. This documents why, how to build/flash it, how to
verify, and how to roll back.

## Why candleLight, not slcan

The headless capture path depends on the stock automation in
`systemd/can0.service` + `systemd/70-canable-can0.rules`. Both key on a
**native `can0` net device**:

- `can0.service` is `BindsTo=sys-subsystem-net-devices-can0.device`.
- the udev rule triggers on `SUBSYSTEM=="net", KERNEL=="can0"`.

Only **gs_usb/candleLight** firmware creates a native `can0` on plug-in. slcan
firmware exposes only `/dev/ttyACM0`; `can0` exists only after a manual
`slcand`, which the automation never runs — so with slcan the bus stays down
after every power-cycle / USB re-enumeration and the daemon can't capture.
Reflashing to candleLight makes the whole stock chain
(`gs_usb` → native `can0` → `can0.service` → `canary.service`) self-recover
with no human in the loop.

## Firmware identity

| | |
|---|---|
| Device | CANable2 (STM32G431); USB `1d50:606f` running, `0483:df11` in DFU |
| Source | `github.com/normaldotcom/candleLight_fw` @ `ba6b1dd` (2021-07-16) |
| Target | `canable2` → `canable2_fw.bin` |
| Committed | `firmware/canable2_fw-ba6b1dd.bin` |
| sha256 | `0adcb0bf71b28089f652bf5be6ca6f8c466e1f9edba14cb6d29959b3793d70fb` |

This is the exact source behind canable.io's own `canable2_fw-ba6b1dd.bin`. We
build and commit it because **there is no verified upstream download**:
canable.io's TLS certificate expired 2026-01-06, and `normaldotcom/canable2-fw`
ships no release artifacts. Mainline `candle-usb/candleLight_fw` does **not**
support the STM32G431, so this `normaldotcom` fork at this commit is the source
of record.

## Build

```bash
sudo apt-get install -y gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi cmake git
scripts/build_canable_fw.sh            # clone, build, sha256-verify vs the committed .bin
scripts/build_canable_fw.sh --install  # also refresh firmware/canable2_fw-ba6b1dd.bin
```

`-fpermissive` is required on GCC ≥ 14: this 2021 code has `return;` in
non-void functions, which modern GCC promotes from a warning to a hard error
(`-Wreturn-mismatch`). It does not change emitted code versus the GCC-8 era the
vendor used — proven by the byte-identical sha256 check the script enforces.
Most users never need to build: flash the committed binary directly.

## Flash

1. **Stop the daemon and free the bus:** `sudo systemctl stop canary.service`.
2. **Enter DFU:** unplug the Canable, hold its **BOOT** button, replug while
   holding. Confirm: `lsusb | grep 0483:df11` (shows "STM Device in DFU Mode").
3. **Flash:**

   ```bash
   sudo dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D firmware/canable2_fw-ba6b1dd.bin
   ```

   `Erase done` / `Download done` / `File downloaded successfully` = success.
   A trailing `dfu-util: Error during download get_status` after "Submitting
   leave request" is **benign and expected** — `:leave` resets the chip into
   the new app immediately, so dfu-util's final status query has nothing to
   talk to. The write itself completed.
4. **Boot the app:** unplug/replug **without** holding BOOT (and return any
   latching BOOT switch to the run position — otherwise the next power-cycle
   re-enters the bootloader and defeats the whole fix).

## Verify

```bash
lsusb | grep -i 1d50:606f                 # candleLight (was 16d0:117e slcan)
lsmod | grep gs_usb                        # gs_usb + can_dev loaded
ip -details -brief link show can0          # native can0 present, UP
systemctl is-active can0.service canary.service   # both active
```

Then drive (or run a capture) and confirm `canaryctl status` populates
`latest_session`.

## Bus-off recovery caveat (gs_usb has no `restart-ms`)

The gs_usb driver for this firmware implements no bus-off restart, so the
kernel rejects `IFLA_CAN_RESTART_MS` ("Device doesn't support restart from Bus
Off"). `scripts/canup.sh` therefore deliberately does **not** pass
`restart-ms` — adding it makes the whole `ip link set type can` call error
(the bitrate only survived by attribute-ordering luck). Note
`systemd/can0.service` now targets the MCP2515 HAT, whose mcp251x driver
*does* support `restart-ms 100`; if you point that unit back at a
Canable, drop the `restart-ms` argument.

Bus-off and full USB re-enumeration recovery is handled instead at the
**application layer** by the recorder's bounded-backoff reconnect
(`src/collection/can_collector.py`, `ReconnectPolicy`), which tears down and
rebuilds the `python-can` bus on any failure — a superset of what kernel
`restart-ms` would do (it also survives USB re-enumeration, which `restart-ms`
cannot). Trade-off: kernel `restart-ms` would recover a *transient* bus-off in
~100 ms; the app layer waits `collection.reconnect_initial_delay_seconds`
(default 10 s) before its first rebuild, so a transient mid-drive bus-off costs
a ≥10 s capture gap. Lower that config value if faster transient recovery
matters more than reconnect churn on a legitimately absent bus.

## Roll back to slcan

Only if you intend to abandon the headless automation (slcan needs manual
`slcand` and breaks `can0.service`). Build the slcan firmware from
`github.com/normaldotcom/canable2-fw` (`make`), enter DFU as above, then
`sudo dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D <slcan>.bin`.
