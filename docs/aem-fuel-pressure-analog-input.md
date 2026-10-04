# AEM fuel pressure → USB ADC (HAT rev 2.0 path)

How the AEM X-Series `30-0301` fuel pressure gauge's 0–5 V analog output
reaches the Pi **today**, on HAT rev 2.0, through a NOYITO 10-channel
STM32F103 + CH340 USB ADC module, running CANary's own 100 Hz firmware.
Built and checked 2026-10-03/04: `sensor:FUEL_PRESSURE` in kPa (gauge pressure).

> **This is not the rev 2.1 plan.** `CANary-HAT-Rev2.1.md` §13.5.4 routes the
> same gauge into an ADS8688A at 0–5.12 V with **no divider at all**. That board
> does not exist yet; this document is the USB path on rev 2.0 hardware.
>
> **What carries over and what does not.** The *electrical* half is specific to
> this path: the divider, the module's reference, the counts-to-volts leg. The
> *gauge* half is universal and rev 2.1 depends on it: the differential
> WHITE/BROWN output, AEM's published `psig = 25.00·V − 12.50`, and the
> 0.50–4.50 V fault gate. Both are written once, in
> `src/decode/analog_sensors.py`, with the two legs as separate parts of the
> catalog (`data/usb_adc_sensors.yaml`). Rev 2.1 reuses the sensor half.
>
> **One difference worth knowing** (from the ADS8688A datasheet, 2026-10-03):
> the HAT does *not* bond BROWN to ground the way this path does. Every
> ADS8688A channel is a pin pair and samples `AIN_nP − AIN_nGND`, tolerating
> ±100 mV on the negative pin — so BROWN lands on `AIN_4GND` and the gauge's
> output stays differential. That is strictly better than the module's
> single-ended input, and it is why "BROWN on module GND" below is correct
> **for this module** rather than in general.

## What the AEM manual settles

**The output is differential.** WHITE is analog positive, BROWN is analog
negative. The NOYITO's inputs are single-ended, so BROWN lands directly on the
module's GND pin — which is what AEM prescribes for this case: *"if the logging
device or ECU does not have a differential analog input then connect the BROWN
wire to the shared signal ground."* Measuring WHITE against module GND **is**
WHITE − BROWN once BROWN is module GND.

**One channel, not two.** AEM publishes a fixed transfer function, so no
ratiometric reference channel is needed:

```
psig = (25.00 × Volts) − 12.50
bar  = (1.72  × Volts) − 0.860
```

0.50 V is 0 psig and 4.50 V is 100 psig. The gauge regenerates the output
against its own internal reference, so the 0.50 V floor is **by design**, not
a sign of a raw sensor passthrough (§13.5.4's discriminator does not apply).

**The valid window is exactly 0.50–4.50 V.** Outside it the manual reads
SENSOR ERROR — a fault state, not a pressure.

## The network, as built

```
AEM WHITE (analog +) ──── R1 9.82k ──┬──── CH0
                                     ├──── C1 100nF ──┐
                                     └──── R2 19.64k ─┤
                                                      │
AEM BROWN (analog −) ─────────────────────────────────┴──── module GND
```

R1 and R2 are 10 k and 20 k 5 % parts from the bag, **measured with a DMM
before soldering**. Their ratio came out at exactly 1.5000 (both 1.8 % low), but
the measured values are what the catalog carries, so a different pair is just
a YAML edit.

| Quantity | Value |
|---|---|
| Sensor volts per pin volt | (R1 + R2) / R2 = **1.5000** |
| 0.50 V in (0 psig) | 0.333 V → 416 counts |
| 4.50 V in (100 psig) | 3.000 V → 3743 counts |
| Saturates at | 4.92 V in (above the window, so it reads as a fault) |
| Resolution | 0.030 psi/count |
| Source impedance | 6.5 kΩ; with C1 the corner is 243 Hz |
| Load on the gauge | 29.5 kΩ (150 µA at 4.50 V) |

**No clamp diode.** The original design had a BAT54S at the pin. R1 alone
limits a fault: WHITE chafed onto 14.4 V puts ~0.9 mA into the STM32's own
clamp and a 40 V load dump ~3.5 mA, both inside its ±5 mA injection rating.
What is given up is accuracy on the *other* inputs during a fault, and none are
used. Add one (BAT54S: pin 1 GND, pin 3 node, pin 2 3V3) if that changes. Do
not substitute silicon diodes: they conduct at the same ~0.6 V as the internal
clamps and share the current instead of taking it.

**The divider must bring 4.50 V under the reference.** The catalog refuses one
that does not, because a saturated pin would otherwise read as a valid 100 psi.
That is why the module cannot take the gauge directly.

## Wiring

1. **Divider at the module**, not at the gauge: the 6.5 kΩ node is the
   noise-sensitive part. The long run carries the gauge's low-impedance output.
2. **WHITE and BROWN as a twisted pair.** BROWN → module GND at the same
   point as R2 and C1.
3. **Gauge power is separate**: switched, fused 12 V (AEM: 5 A inline fuse),
   its own ground to chassis.
4. **Module and divider stay in the cabin.**

### BROWN on module GND, measured

BROWN is the differential negative, and in the truck it is also the bond
between the gauge and the Pi's ground (BROWN → module GND → Pi → chassis →
gauge power ground). The manual's bench note says the gauge does not tie them
internally. **Bench-testing trap:** on a separate supply, jumper BROWN to that
supply's ground or the reading is meaningless.

Measured 2026-10-03, red probe on BROWN, black on the gauge's power ground:
**+37 to +47 mV** KOEO, **+40 mV** running. So the Pi's ground sits ~40 mV above
the gauge's, and the offset barely moves with the engine running. BROWN carries
only the divider's 150 µA, so nothing in this path adds IR drop of its own.

## Scaling

```
pin volts    = counts × reference_volts / 4095
sensor volts = pin volts × (R1 + R2) / R2
psig         = 25.00 × sensor volts − 12.50 + offset
```

With the as-built constants (3.282 V, 1.5000) that is
`psig = 0.030055 × counts − 12.50`. Recorded in kPa (× 6.894757), the project's
pressure unit.

### The 0.50–4.50 V window is a gate, not a note

Outside the window, applied to the **sensor volts** (after the measured
constants, not to raw counts), the decoder records `sensor:FUEL_PRESSURE_FAULT`
with value `below_window` or `above_window` and **no pressure**. Clamping
instead would make a broken WHITE wire at 0 V decode to 0.0 psig, a
plausible-looking lie. An open WHITE, a WHITE-to-BROWN short and an unpowered
gauge all land in `below_window`. Seen live on 2026-10-03: with the gauge off,
CH0 read 1–3 counts and recorded `below_window` every frame.

At zero rail pressure the gauge sits at its 0.50 V floor, on the boundary, so
expect it to flicker in and out of `below_window` at key-on before the prime.

## Constants instead of a calibration run

Because AEM publishes the volts-to-psi leg exactly, only the electrical chain
is unknown, and it was **measured** rather than fitted:

| Constant | How | Value |
|---|---|---|
| `r_top_ohms` (R1) | DMM, out of circuit | 9820 |
| `r_bottom_ohms` (R2) | DMM, out of circuit | 19640 |
| `reference_volts` | DMM, module 3V3 pin to GND, plugged into the Pi | 3.282 |

The 3V3 pin is the reference because the STM32's ADC reference is its supply
rail. It is 0.5 % below nominal; the firmware's own volts column assumes
3.300 V, which is why the collector uses counts only. The resistor ratio from
one DMM range is good to ~0.1 %, so the whole chain is good to under 1 % with
no calibration run. The rail on this truck is returnless and fixed at ~51 psi,
so a multi-point fit against rail pressure was never possible anyway.

### Checks (2026-10-03)

The electrical chain, end to end:

| Point | DMM | From counts |
|---|---|---|
| CH0 pin to GND (ACC, zero pressure) | 382.5 mV | 381.8 mV |
| Gauge WHITE–BROWN, same moment | 0.574 V | 0.573 V |
| WHITE to gauge power ground, running | 2.58 V | 2.542 + 0.040 = 2.582 V |

Against the gauge face:

| Condition | Face | ADC | ADC − face |
|---|---|---|---|
| ACC, zero pressure | 0 | 1.8 | +1.8 |
| KOEO prime, bleeding down | 44 → 43 | 45.5 → 44.9 | +1.5 … +1.9 |
| KOEO | 42 | 43.7 | +1.7 |
| KOEO | 41 | 42.4 | +1.4 |
| KOEO | 34 | 36.1 | +2.1 |
| Engine running | 49 | 51.0 | +2.0 |

A **constant +1.8 ± 0.3 psi** from 0 to 51 psi, KOEO and running. The slope
agrees, so the divider and reference are right; the gauge really outputs
0.574 V at zero. Which instrument is right is **unresolved**:

- the face read 0 at zero pressure, which favours the face;
- the analog output's 51.0 psi running matches the factory 350 kPa (50.8 psi),
  which favours the analog output, and a face that cannot show negatives
  would read 0 if it were the one 1.8 low.

The catalog's `offset` (psi, added after the transfer function) stays **0**
until this is settled. Settle it with the sensor vented to atmosphere, next
time the fuel system is open for the supercharger work: whichever instrument
does not read 0 there is the one that is off. If it is the analog output, set
`offset` to minus its reading. Pressure *changes* (rail sag, injector ΔP under
boost) are right to within ~1 % of the change either way.

## The collector

| | |
|---|---|
| Config | `hardware.usb_adc` (`enabled`, `port`, `baudrate`, `stall_seconds`, `sensors`) |
| Calibration | `data/usb_adc_sensors.yaml`: module reference, and per channel the input, divider, transfer, window, record unit and offset |
| Code | `src/collection/usb_adc_collector.py` (serial, frame sync, records); `src/decode/analog_sensors.py` (catalog, counts → value, gate); `firmware/usb-adc/` (the module's firmware) |
| Channels | `sensor:FUEL_PRESSURE` (kPa); `sensor:FUEL_PRESSURE_FAULT` (`below_window`/`above_window`) |
| Per record | metadata `adc_input`, `adc_counts`, `sensor_volts`; payload the raw `CHn:…` line, so a corrected constant can be re-applied to stored sessions |
| Tests | `tests/test_usb_adc.py`, with the 2026-10-03 readings as known answers |

- **Port by USB path.** A plain CH340 has no serial number, so `by-id` cannot
  tell two apart and `/dev/ttyUSB*` follows probe order. On the Pi it is
  `/dev/serial/by-path/platform-xhci-hcd.1-usb-0:2:1.0-port0`. That names the
  physical port, so the module must go back in the same one. (It did not
  enumerate at all in the first port tried; the second worked.)
- **The first frame after opening is discarded.** It can start mid-line, and
  lines queued before the port opened would carry a later stamp than the moment
  they describe.
- **Stamps are line arrival.** Each reading is stamped when its line arrives.
- **A silent module is reopened.** No parseable line for `stall_seconds` (5 s)
  raises and the reconnect loop reopens the port.
- **External sensor.** `sensor` is outside `TRAINABLE_SOURCES`, so the channel
  never trains or scores the anomaly detectors.
- **One instrument per channel.** `data/sensor_can_signals.yaml` still defines
  `FUEL_PRESSURE` from the AEMnet plan (this gauge turned out not to be AEMnet).
  Enabling both `hardware.usb_adc` and `hardware.sensor_can` with the same name
  is refused at start-up.

## Firmware and sample rate

**The module runs CANary's own firmware since 2026-10-04: CH0 at 100 Hz**
(`firmware/usb-adc/`, ~700 bytes). Each reading is the mean of every conversion
in its 10 ms (~420), and the text format is the stock one minus the volts column,
so the collector reads either firmware unchanged. At ACC with the gauge at 0 psi
the two firmwares agreed to 3 counts (476.4 stock, 479.4 new; 0.09 psi), so the
constants carried over. Noise fell from ±5 counts to ±1.5.

**Why it was replaced.** The stock firmware printed all ten inputs every
0.516 s and answered nothing sent to it (`?`, `help`, `AT`, single bytes;
see also the [ADC-10-F103C notes](https://github.com/swharden/ADC-10-F103C)).
A frame is 15 ms of wire time at 115200 baud, so the 2 Hz was a delay in the
firmware, not a limit of the link or the ADC, and 2 Hz cannot resolve rail
sag through a WOT pull.

**How it was flashed.** The board has a **BOOT0 jumper** (GND or 3V3). At 3V3
the STM32's ROM bootloader answers on the same USB serial port, so
`stm32flash` (installed on the Pi) needs no extra hardware. The chip identified
as **STM32F103x6** (device ID `0x0412`, 32 KB flash, 10 KB RAM), not the C8 the
listing claims. Read-out protection was off, so the stock image was read out
first: `firmware/usb-adc/stock-noyito.bin`. Build, flash and restore steps are
in `firmware/usb-adc/README.md`.

**What to check before going faster.** The gauge regenerates its analog output
at some rate of its own. Sampling faster than that only oversamples. At 100 Hz,
look at a WOT pull's trace for steps before raising `OUTPUT_HZ`.
*Answered 2026-10-04:* no steps. The output carries the rail's injector
pulsation (0.12–0.38 psi rms, at engine-order harmonics of rpm/40 Hz) and
the 100 Hz samples alias it, so the gauge is not the bandwidth limit. Average
over ≥ 0.5 s for pressure; a faster rate or an anti-alias filter is only for
studying the pulsation (`data/analyses/2026-10-04-fuel-pressure-first-drive.md`).

## Status

- [x] Module identified: NOYITO 10-ch, STM32F103x6 (ID 0x0412, not the listed C8) + CH340 (`1a86:7523`), 115200 baud, stock text frames at 2 Hz
- [x] No onboard input network: gauge-side 0.574 V (DMM) vs 0.573 V from counts through the measured divider; a resistor at the pin would have to be ≳10 MΩ to hide in that 0.2 %
- [x] Wire colours: WHITE +, BROWN − (operator, per the manual)
- [x] Divider built and connected (R1 9.82k, R2 19.64k, 100 nF, no clamp)
- [x] Ground offset BROWN→gauge ground measured: +37…+47 mV KOEO, +40 mV running
- [x] Electrical chain checked against the DMM at three points
- [x] Cross-checked against the gauge face: constant +1.8 psi, unresolved (see above)
- [ ] Loading check (divider on vs off at steady pressure). Bounded already: a fixed source impedance would show as a slope error against the face, and the offset held constant from 0 to 51 psi, so sag is ≲1 %
- [x] Collector written: `sensor:FUEL_PRESSURE`, `sensor:FUEL_PRESSURE_FAULT`
- [x] Enabled in `/etc/canary/canary.yaml` on the Pi (`hardware.usb_adc`): first drive 2026-10-04, 205k rows at 100 Hz, one start-up stall (`data/analyses/2026-10-04-fuel-pressure-first-drive.md`)
- [ ] Offset settled with the sensor vented to atmosphere
- [x] Faster firmware: `firmware/usb-adc`, 100 Hz, flashed 2026-10-04 via the BOOT0 jumper; stock image saved
