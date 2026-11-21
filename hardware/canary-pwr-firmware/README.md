# CANary-PWR supervisor firmware

Firmware for the STM32G031K8 on CANary-PWR rev 1.0 (`docs/CANary-PWR-Spec.md`
§P7), and the Pi-side tool that flashes and reads it. Built and run on the Pi;
no SWD probe is needed at any step, including the one-time option-byte change.

**This is bring-up firmware: it observes and reports, and switches nothing.**
It reads the INA226 (Pi 5 V volts and amps), the LTC3350 (input, output, bank
and per-cell voltages, currents, die temperature, charger and monitor status),
the MCU's ADC (VIN_SENSE, Pi 3V3, VREFINT) and every status pin, and serves it
to the Pi on I²C1 at **0x2E**. It never drives PB0 (`PI_5V_OFF`) or PA8
(`~PWR_FAIL`), never writes the LTC3350, and starts no watchdog: those pins are
plain inputs, so their pull resistors hold them exactly as when the MCU is in
reset, the default-on rule of §P7. The state machine (PARKED/RUN/DIP/HOLDUP),
`~PWR_FAIL`, the Pi watchdog, LTC3350 setup and the daemon's ingest are later
work.

LEDs: both on for the first second, then **green** blinks 100 ms a second
(solid while SW1 is held) and **red** is on while the INA226 or the LTC3350
does not answer.

## Files

| Path | What it is |
|---|---|
| `src/regs.h` | The register map the Pi reads: the one copy of the contract. The C struct is checked against it at compile time and `pwr_tool.py` parses it at run time. |
| `src/main.c`, `src/startup.c`, `src/board.h` | The firmware. HSI16, no PLL; I2C1 target (PB6/PB7) for the Pi, I2C2 controller (PA11/PA12) for the private bus. |
| `stm32g031k8.ld` | Link map. `.fw_tag` ("CANary-PWR fw") sits at 0xC0, right after the vectors, and the tool refuses an image without it. |
| `ram/helper.c`, `ram/helper.h`, `ram/helper.ld` | The RAM helper. The ST bootloader loads it at 0x20001000 and runs it with Go: one build clears nBOOT_SEL, the other samples BOOT0 and the button. It reports through status words at 0x20001E00, which the Pi reads back through the bootloader. |
| `pwr_tool.py` | Pi-side tool, standard library only. It drives GPIO27 (`PI_BOOT0`) and GPIO24 (`PI_NRST`) around stm32flash, which speaks the AN4221 bootloader protocol. Anything that resets the MCU refuses while a capture is recording. |
| `vendor/st`, `vendor/arm` | CMSIS device header (STMicroelectronics/cmsis-device-g0 v1.4.5) and core headers (ARM-software/CMSIS_5 5.9.0), unmodified, Apache-2.0 (licences alongside). |

## Build

On the Pi, which has Debian's `gcc-arm-none-eabi` and `stm32flash`:

```bash
cd ~/canary/hardware/canary-pwr-firmware
make            # build/pwr.bin, build/ram-boot-sel.bin, build/ram-pins.bin
```

`PWR_REG_BUILD_ID` carries the checkout's short commit hash, so `status` shows
which commit is on the board.

## Bringing up a new board

Every step is safe with the Pi powered from PWR. In reset or in the
bootloader, the MCU's pins float and the pull-downs keep the buck on.

```bash
python3 pwr_tool.py info --stay        # bootloader 0x56, option bytes, flash blank
python3 pwr_tool.py probe-pins         # GPIO27 reaches BOOT0, GPIO24 resets the MCU
python3 pwr_tool.py boot-sel           # dry run: shows the one-bit plan
python3 pwr_tool.py boot-sel --yes     # nBOOT_SEL 1 -> 0, checked after the reload
python3 pwr_tool.py flash              # write build/pwr.bin, verify, start it
python3 pwr_tool.py check-reentry      # back into the bootloader by pins and by command
python3 pwr_tool.py status             # what the board sees
```

**Why `boot-sel` comes first.** A new STM32G0 ships with `nBOOT_SEL = 1`,
which makes it ignore the BOOT0 pin. A blank chip still starts its bootloader
(AN2606 pattern 11), but once the flash holds code the Pi's GPIO27 could no
longer bring it back: only the firmware's own command or an SWD probe could.
`flash` refuses while `nBOOT_SEL` is 1. `boot-sel` loads the RAM helper, which
runs ST's own option-byte sequence (the G0 HAL's unlock, OPTR write, OPTSTRT
and OBL_LAUNCH). It first refuses unless readout protection is level 0
(0xAA), BOOT_LOCK is clear and the new word differs in nBOOT_SEL only: 0xCC
in the RDP byte would lock the chip for good. Afterwards the tool reads the
whole option area back through the bootloader and fails unless OPTR is the
planned word and every other option register is unchanged.

## What the first board taught (2026-11-20)

- **FLASH_OPTR reads back the value loaded at the last option-byte load, not
  the value just written.** The helper's first version refused because a
  read-back straight after the write returned the old word. ST's
  `HAL_FLASHEx_OBGetConfig` warns of exactly this. The helper now only records
  that read, and the check happens after the reload.
- **OPTSTRT rewrites every option word from the registers**, which read their
  reserved bits as 0, so the factory `0xFF00FF3F` (WRP1AR) is stored as
  `0x0000001F` afterwards with the same settings. `option_settings()` compares
  implemented bits only. The before/after dumps are fixtures in
  `tests/test_pwr_tool.py`.
- **The first reset after flashing came straight up in the firmware**, so
  `start_app()`'s fallback to the bootloader's Go was not needed. It stays for
  a part that keeps PROGEMPTY latched. The firmware also clears PROGEMPTY at
  start.
- **The TCAN1043 shows RXD and nFAULT low** with EN and nSTB left undriven
  (their internal pull-downs): that is its wake-request flag after power-up,
  not a fault. The supervisor firmware will clear it when it takes the pins.
- **With the Pi off, the MCU side of the TCA9511A sits low**: R34/R35 pull up
  to the Pi's 3V3. The supervisor firmware should stop its I2C1 target while
  `PI_3V3_SENSE` is low.

First board's readings, parked with the cap bank fitted: Pi 5 V 5.06–5.10 V
at 0.66–1.06 A; VIN 12.19 V (MCU ADC) and 12.28 V (LTC3350); VCAP 10.01 V with
cells 2.495–2.500 V; charger `stepdown cv cappg`; LTC3350 die 43.7 °C; main
loop 1.85 kHz, about 940 INA226 bus readings a second.

## Not done yet

- The spec's second option change, hardware IWDG (`IWDG_SW = 0`), waits for
  firmware that refreshes a watchdog. `boot-sel` changes nBOOT_SEL only.
- Everything in §P7 that switches: the state machine, `~PWR_FAIL`, the 5 V cut,
  the Pi heartbeat watchdog, crank summaries and the parked history. Also
  LTC3350 configuration (§P9) and the daemon's reader for these registers.
