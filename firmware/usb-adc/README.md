# CANary firmware for the NOYITO USB ADC module

Replaces the module's stock firmware (all ten inputs, fixed 2 Hz, no command
interface) with the configured inputs only, each a boxcar mean over the whole
output period, at 100 Hz. The output is the stock text format without the volts
column, so `src/collection/usb_adc_collector.py` reads either firmware with no
change:

```
CH0:2114
                 <- blank line ends each frame
```

| | Stock | This firmware |
|---|---|---|
| Rate | 2 Hz (0.516 s) | 100 Hz |
| Inputs | CH0..CH9 | `CHANNELS[]` in `main.c` (default: CH0) |
| Per reading | one conversion (assumed) | mean of ~420 conversions / 10 ms |
| CH0 noise, gauge at 0 psi | ±5 counts | ±1.5 counts |
| CH0 at ACC, 2026-10-04 | 476.4 | 479.4 (0.09 psi apart) |

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

On the Pi, which has `gcc-arm-none-eabi` (the laptop does not):

```bash
cd firmware/usb-adc && make          # canary-usb-adc.bin, ~700 bytes
make OUTPUT_HZ=50                    # a different rate
```

No vendor headers or libc: registers by address, the vector table and startup
are in `main.c`. The clock is the internal RC through the PLL (64 MHz), so
nothing depends on the board's crystal. The Pi stamps lines on arrival, so the
RC's ~1 % rate error does not reach the data. An independent watchdog resets
the chip if the loop stops for ~1 s.

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

Changing `CHANNELS[]` changes what the collector sees. It ignores inputs that
`data/usb_adc_sensors.yaml` does not name, and an input with no line in a
frame simply records nothing.
