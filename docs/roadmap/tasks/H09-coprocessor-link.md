# H09 — HAT rev 3.0 coprocessor: the Pi link, its Python side, and the firmware

Part of [H00](H00-hat-integration-README.md). Rev 3.0 adds an STM32U575RGT3
coprocessor, powered from the Pi. It buffers the boot gap, timestamps the
streams only it sees, and owns K-line, the IMU and the ADS8688A. Design
record: `docs/CANary-HAT-Revisions.md` §13.3. The decisions below were made
2026-10-14/15:

| Topic | Where |
|---|---|
| Failure containment | §13.3.0 |
| Boot gap and the witness clock | §13.3.3a, §13.3.3c |
| K-line contract | §13.3.4 |
| Header map | §13.3.6 |
| ADC behind the MCU | §13.3.9 item 2 |
| Part, schematic-entry facts | §13.3.10 |
| PWR compatibility | §13.3.14 |

## Where it stands

- **Designed, nothing built.** There is no schematic, no board and no
  firmware. Every Desk item runs against a simulated MCU and needs neither.
- **The operator brings firmware up on the fabricated board.** There is no
  Nucleo stage (§13.3.0). The firmware's hardware-free core is still tested
  on the laptop before then (H09.10).
- **Toolchain on the laptop:** `arm-none-eabi-gcc` 13.2.1 and OpenOCD 0.12.0
  (with `target/stm32u5x.cfg` and the `linuxgpiod` adapter), installed
  2026-10-15.

## Operator decisions (2026-10-14/15 — do not re-open)

1. **`can0` is the system of record for CAN.** The MCU never streams the whole
   bus in steady state. It sends the boot-gap dump and a witness stream
   (§13.3.3a/c).
2. **The firmware is a physical layer.** No ISO-TP, no KWP2000, no decode or
   bindings, no inference, no poll plan (§13.3.7). K-line framing is by
   silence threshold, and the K-line allowlist stays in Python only (§13.3.4).
3. **Configuration is data the Pi sends.** The ADC channel map and ranges
   (`data/hat_adc_*.yaml`), IMU rates, witness IDs and K-line timings arrive
   over the link at start-up. None of them is a firmware constant. This is
   H08's rule, carried over.
4. **The MCU never transmits on a vehicle bus unprompted.** CAN is listen-only
   (FDCAN bus monitoring). K-line transmits only inside a `wake` or `transact`
   the Pi commanded. If the command channel goes quiet in command mode, the
   MCU drops EN and the pull-up itself.
5. **Bare-metal C, no RTOS.** ST's STM32CubeU5 library sets up the clock tree
   and FDCAN; the paths with deadlines go to registers (CAN RX, K-line edges,
   SPI slave, ADC pacing). Build with `arm-none-eabi-gcc`; flash and debug
   with OpenOCD and gdb. This follows `firmware/usb-adc` and
   `scripts/build_canable_fw.sh`.
6. **The hardware-free core is tested from pytest.** Link framing, the delta
   encoder, the head-kept ring and the K-line timing state machine are plain C
   with no hardware headers. They compile for the host and pytest drives them,
   like `tests/test_usb_adc_firmware_filter.py`. The link tests share golden
   vectors with the Python codec.
7. **The independent watchdog (IWDG) is on from the first image. Never set
   RDP level 2. Keep TrustZone (`TZEN`) off.**

## Rules

- **One spec, two implementations, one set of vectors.** The link spec
  (H09.1) is the contract. The Python codec and the firmware framing each pass
  the same golden vectors, so neither can drift on its own.
- **Every MCU-stamped row says so.** Rows timed by the MCU carry
  `clock: "mcu"`, and an unmapped one is never passed off as `kernel` time.
- **The CAN RX interrupt has the top priority,** and nothing may hold it off
  longer than ~0.5 ms: two RX FIFOs of three frames (§13.3.10). A firmware
  change that adds a critical section states its worst-case length.
- **An empty or failed read names its kind.** "No coprocessor", "link CRC
  error", "boot gap not stitched" and "K-line echo mismatch" are different
  outcomes, reported differently (AGENTS.md).

---

## Spec

- [~] **H09.1 Link protocol spec.** **Evidence (2026-10-15):**
  `docs/coprocessor-link.md` (draft v1) and
  `tests/fixtures/coprocessor_link/vectors.yaml`. There are 40 cases covering
  all 23 message types, the headers, a poll, two full transactions, a
  boot-gap block, and four invalid cases (header CRC, all-zero MISO, frame
  CRC, version). All 40 were re-checked by a second, independent decoder with
  bitwise CRCs. **Open:** the operator's review. **Do:** write
  `docs/coprocessor-link.md` and commit golden vectors under
  `tests/fixtures/coprocessor_link/`. It must fix:
  - **Physical:** SPI0 CE1, mode and clock; DATA_RDY on GPIO23, active high,
    pulled down; MISO through the CE1-gated buffer (§13.3.6, §13.3.0).
  - **Transaction model:** the Pi is always master. It reads a fixed status
    word (pending-message length, flags, sequence) and then that many bytes,
    and it writes commands in the same frame format. State what DATA_RDY
    asserts and when it drops.
  - **Framing:** magic/version, type, sequence number, length, payload,
    CRC (name the polynomial). Say what happens on a CRC failure, a sequence
    gap and a version mismatch.
  - **Message catalogue:** STATUS/heartbeat, CONFIG (push and readback),
    ADC_BLOCK, IMU_BLOCK, CAN_WITNESS, BOOTGAP_CHUNK (with head/tail markers
    and overflow counts), PPS_EDGE, the KLINE commands, results and sniff
    events (§13.3.4), and LOG.
  - **Timestamps:** the MCU counter's width, rate and wrap handling, and how
    PPS disciplines it.
  - **Backpressure:** what the MCU drops first when the Pi falls behind, and
    how it counts the drops.

  **Accept:** the operator reviews the spec, and the vectors cover every
  message type plus one corrupted frame.

## Desk — Python against a simulated MCU (no hardware)

- [ ] **H09.2 Link codec.** **Do:** encode and decode every H09.1 message,
  with typed frozen dataclasses, refusing bad CRCs and unknown types by name.
  **Accept:** the golden vectors round-trip byte for byte.
- [ ] **H09.3 Fake MCU.** **Do:** an in-process simulator speaking the spec. It
  has a clock that drifts and jumps on command, a scripted CAN bus fed from a
  real capture, a scripted K-line peer, and ADC/IMU sources. It is the twin of
  `tests/ads8688a_fake.py`. **Accept:** H09.4–H09.8 run against it in the
  default pytest tier.
- [ ] **H09.4 Witness fit.** **Do:** match witnesses to `can0` rows by ID,
  payload and order, fit MCU→Pi time over a sliding window, report the
  residual as `can0` stamp jitter, and refuse when too few pairs match (the
  `compare_channels` discipline). **Accept:** on a real capture re-stamped
  with a known drift and offset, the fit recovers both within tolerance; a
  payload-ambiguous ID is refused.
- [ ] **H09.5 Boot-gap stitching.** **Do:** align the dump to `can0`'s first
  seconds by exact (ID, payload) sequence; keep the frames before the first
  match; map their times with the overlap fit. Add `"mcu"` to `AcquiredClock`
  (`src/collection/obd_reading.py`) and to `frame_time.py`'s policy.
  **Accept:** a synthetic boot gap round-trips with no duplicate and no gap.
  A dump with no overlap, or with a reset mid-gap, is written as
  `clock: "mcu"` unmapped and says why.
- [ ] **H09.6 `McuKlineTransport`.** **Do:** `KlineTransport`'s API over the
  link (§13.3.4). The sniff decoder takes measured break events instead of
  mark-parity bytes. Everything above the transport (sessions, plan,
  allowlist, `tx_gate`) stays unchanged. **Accept:** the existing K-line
  session tests pass against the fake MCU, and an echo mismatch from the fake
  peer raises `EchoMismatchError`.
- [ ] **H09.7 ADC over the link.** **Do:** the ADS8688A collector reads
  ADC_BLOCKs instead of `/dev/spidev0.1` when the revision says so. The
  channel map stays `data/hat_adc_*.yaml` and is pushed by CONFIG.
  **Accept:** H08's channel and calibration tests pass on the link path.
- [ ] **H09.8 IMU over the link.** **Do:** an `ImuDevice` implementation (the
  seam `src/collection/imu_collector.py` already has) fed by IMU_BLOCKs.
  **Accept:** H02's frame-resolution tests pass on the link path.

## Desk — firmware without a board

- [ ] **H09.9 Firmware project.** **Do:** `firmware/canary-hat-mcu/` with a
  Makefile or CMake, startup code, a linker script for the U575 (SRAM3 holding
  the boot-gap ring), and ST's CMSIS device headers and HAL fetched at a
  pinned commit by `scripts/build_hat_mcu_fw.sh` (the `build_canable_fw.sh`
  pattern: no hand-vendored copies, toolchain checked, one-line install hint).
  **Accept:** a clean checkout builds and links an image with a size report,
  and the map file puts the ring in SRAM3. Nothing can boot it before the
  board exists.
- [ ] **H09.10 Host-tested core.** **Do:** the hardware-free C modules
  (framing + CRC, delta encoder, head-kept ring, K-line timing state machine),
  compiled for the host and driven from pytest, skipping where no C compiler
  exists. **Accept:** the framing passes the H09.1 vectors. The delta encoder
  round-trips a real capture and reports its bytes/frame, which re-checks
  §13.3.10's ~6 B/frame on more than the 5-ID slice. The ring keeps the head
  under overflow.
- [ ] **H09.11 Peripheral drivers (written, untestable until the board).**
  - FDCAN in bus-monitoring mode with RX timestamps (confirm both in RM0456
    first);
  - SPI slave with DMA, and DATA_RDY;
  - ADS8688A sampling on a hardware timer;
  - the LSM6DSOX FIFO on the private I²C;
  - the K-line primitives (`set_mode`, `wake`, `transact`, `receive`, the
    sniff stream);
  - PPS input capture, the witness stream, the console UART and the IWDG.

  **Accept:** it builds; each driver names the H09.14 step that proves it.

## After the schematic (pins can still move during routing)

- [ ] **H09.12 Revision row and overlay.** **Do:** a 3.0 row in
  `hardware/canary-hat-firmware/hat_revisions.json` and a `canary-hat-r30`
  overlay: `uart0` as the MCU console, no K-line pins, no ADC spidev on CE1,
  CE1 for the link, DATA_RDY as an interrupt. **Accept:**
  `tests/test_hat_revisions.py` checks the overlay against the row.
- [ ] **H09.13 Flash from the Pi.** **Do:** an OpenOCD config using
  `linuxgpiod` on GPIO26 SWDIO, GPIO21 SWCLK and GPIO16 NRST (through the
  BSS138), and a `canaryctl`/script path that flashes and verifies without
  stopping a capture. **Accept:** a dry run against the config on the Pi
  **(Pi)**.

## Board **(hardware)**

- [ ] **H09.14 Bring-up.** In order: SWD flash from the Pi; console alive;
  IWDG resets a deliberately hung image; link STATUS round trip; FDCAN
  listen-only frames match `can0` 1:1; witness jitter measured; a cold start's
  boot gap captured and stitched **(truck)**; ADC and IMU blocks; one K-line
  transaction and one sniffed session **(truck)**. Also measurements 12 and
  15 (§13.1) and PWR §P10 item 14 (ripple through the hold-up).

## Order

```
H09.1 spec ─┬─> H09.2 codec ─> H09.3 fake MCU ─┬─> H09.4 witness ─> H09.5 stitching
            │                                  ├─> H09.6 K-line
            │                                  ├─> H09.7 ADC
            │                                  └─> H09.8 IMU
            └─> H09.9 firmware project ─> H09.10 core ─> H09.11 drivers
schematic ─> H09.12 overlay, H09.13 flash ─> board ─> H09.14
```
