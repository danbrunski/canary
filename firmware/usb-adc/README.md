# CANary firmware for the NOYITO USB ADC module

Replaces the module's stock firmware (all ten inputs, fixed 2 Hz, no command
interface). **Version 2** (2026-10-04) reads CH0 at **503.9 Hz**, anti-alias
filtered, for studying the fuel rail's injector pulsation; version 1 (git
history, flashed 2026-10-04) was a 100 Hz boxcar mean. The output is the stock
text format without the volts column, plus a sequence number, so
`src/collection/usb_adc_collector.py` reads all three:

```
FW:2 503905 15864        <- about once a second: version, rate (mHz), group delay (us)
CH0:2114\t7342           <- counts, tab, sequence number mod 10000
                         <- blank line ends each frame
```

| | Stock | v1 | v2 |
|---|---|---|---|
| Rate | 2 Hz (0.516 s) | 100 Hz | 503.9 Hz (from the ADC clock) |
| Inputs | CH0..CH9 | `CHANNELS[]` | `ADC_INPUT` (one) |
| Per reading | one conversion (assumed) | mean of ~420 / 10 ms | 21-sum, 64-tap FIR, every 4th |
| Response | n/a | boxcar: 50 Hz at -4 dB | flat to 180 Hz, >= 74 dB stop from 304 Hz |
| CH0 noise, gauge at 0 psi | ±5 counts | ±1.5 counts | not yet measured |

## The filter

`design_fir.py` designs the FIR and writes `fir_taps.h` (Q15 taps summing to
32768, so DC passes at unit gain). Rerun it after changing any rate or band;
`--check` fails when the header is stale, and so does
`tests/test_usb_adc_firmware_filter.py`, which also compiles `filter.h` on the
host (`host_filter.c`) and checks DC, passband and stopband with tones. The
ADC converts continuously into a 512-sample DMA ring (42,328 conversions/s,
no gaps), so the rate is exact to the module's RC oscillator (~1 %). The
collector measures the true rate against the Pi's clock, and stamps each
sample by sequence number minus the 15.9 ms group delay. The divider's 100 nF
(corner ~243 Hz) is part of the chain: it costs -1.9 dB at 180 Hz and guards
the stage-1 aliases near 2 kHz.

## The hardware

- MCU: **STM32F103x6**, low density (bootloader device ID `0x0412`, 32 KB
  flash, 10 KB RAM), not the C8 the listing claims. CH340 (`1a86:7523`) on
  USART1, PA9/PA10. The module's `CHn` is `ADC_INn` (PA0..PA7, PB0, PB1);
  CH0 = PA0 confirmed against the AEM gauge.
- **BOOT0 jumper** selects GND (run the flashed firmware) or 3V3 (the ROM
  bootloader, which listens on the same USB serial port). It takes effect at
  power-up.
- Read-out protection was off. The stock image was read out before the first
  flash: `stock-noyito.bin` (32768 bytes, sha256 `b20be324…1d652`).

## Build

On the Pi, which has `gcc-arm-none-eabi` (the laptop does not; there,
`clang --target=thumbv7m-none-eabi -c main.c` compile-checks it):

```bash
cd firmware/usb-adc && make          # canary-usb-adc.bin, ~1.5 KB
```

No vendor headers or libc: registers by address, the vector table and startup
are in `main.c`. The 64-bit filter accumulator is shifted, never divided, so
nothing needs libgcc. The clock is the internal RC through the PLL (64 MHz),
so nothing depends on the board's crystal. An independent watchdog resets
the chip if the loop stops for ~1 s. The committed `canary-usb-adc.bin` is
whatever was last flashed. `make` rebuilds it whenever `main.c` is newer.

## Flash

The daemon must not hold the port: set `hardware.usb_adc.enabled: false` and
restart it with `scripts/restart_canary.sh` first. Then:

1. Move BOOT0 to **3V3**, unplug and replug the module. It goes silent.
2. `stm32flash -b 115200 /dev/serial/by-path/…` identifies the chip and writes
   nothing.
3. `make flash` writes, verifies by read-back, and starts the firmware
   (`-g 0x08000000`) without a jumper change, so it can be checked.
4. Move BOOT0 to **GND**, unplug and replug. The firmware now runs from flash
   at every power-up.

**To go back to stock:** steps 1–2, then
`stm32flash -b 115200 -w stock-noyito.bin -v /dev/serial/by-path/…`, then step 4.

**After flashing v2, check:** at ACC with the gauge at 0 psi, CH0 reads
within a few counts of v1's 479; lines carry a sequence number and an `FW:2`
line arrives about once a second; `/status` shows no USB ADC stalls. Then
re-enable `hardware.usb_adc` and restart with `scripts/restart_canary.sh`.

`ADC_INPUT` picks the input. The collector ignores inputs that
`data/usb_adc_sensors.yaml` does not name. Reading several inputs needs scan
mode, which v2 does not build.
