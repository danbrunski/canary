# AEM fuel pressure → USB ADC (HAT rev 2.0 path)

How the AEM X-Series `30-0301` fuel pressure gauge's 0–5 V analog output
reaches the Pi **today**, on HAT rev 2.0, through a NOYITO 10-channel
CH340 USB ADC module.

> **This is not the rev 2.1 plan.** `CANary-HAT-Rev2.1.md` §13.5.4 routes the
> same gauge into an ADS8688 at 0–5.12 V with **no divider at all**. That board
> does not exist yet. This document is the USB path on rev 2.0 hardware, and
> nothing here carries over to the HAT.

## What the AEM manual settles

Both questions §13.5.4 left open are closed by the install manual, and one of
them simplifies the wiring.

**The output is differential.** WHITE is analog positive, BROWN is analog
negative. The NOYITO's inputs are single-ended, so BROWN lands directly on the
module's GND pin — which is what AEM prescribes for this case: *"if the logging
device or ECU does not have a differential analog input then connect the BROWN
wire to the shared signal ground."* That connection is what makes the reading
correct, not merely convenient: measuring WHITE against module GND **is**
WHITE − BROWN, so the gauge's own negative defines zero and any offset between
the gauge's power ground and the Pi's drops out of the reading.

This also settles the wire colours §13.5.4 said must be metered rather than
assumed. Meter them anyway as a check, but the manual is explicit.

**One channel, not two.** AEM publishes a fixed transfer function, so the
`FUEL_P_REF` ratiometric companion channel is not needed:

```
psig = (25.00 × Volts) − 12.50
bar  = (1.72  × Volts) − 0.860
```

0.50 V is 0 psig and 4.50 V is 100 psig. A *fixed* volts-to-psi relation means
the gauge head regenerates the output against its own internal reference rather
than passing the pressure sensor's ratiometric signal through. No reference
channel, no second divider, no fifth wire.

Note for anyone reading §13.5.4's discriminator: it says ~0.5 V at zero
pressure indicates a raw sensor passthrough needing a reference channel. For
this gauge the 0.50 V floor is **by design**, and the published formula
supersedes the test.

**The valid window is exactly 0.50–4.50 V.** Outside it the manual reads
SENSOR ERROR — a fault state, not a pressure. Maximum signal is therefore
4.50 V.

## The resistor network

10 kΩ / 10 kΩ, 1% metal film, plus 100 nF.

```
AEM WHITE (analog +) ──── R1 10k ──┬──── AIN0
                                   ├──── C1 100nF ──┐
                                   └──── R2 10k ────┤
                                                    │
AEM BROWN (analog −) ───────────────────────────────┴──── GND
```

| Quantity | Value |
|---|---|
| Ratio | 0.500 — `V_gauge = 2 × V_adc` |
| 0.50 V in (0 psig) | 0.250 V → 310 counts |
| 4.50 V in (100 psig) | 2.250 V → 2792 counts |
| Usable span | 2482 counts → **0.0403 psi/count** |
| Saturates at | 6.60 V in |
| Source impedance | 5.0 kΩ |
| Load on the gauge | 20 kΩ (225 µA at 4.50 V) |
| Filter corner | 318 Hz |
| Dissipation | 1.0 mW — any wattage resistor |

Buy a strip from one reel. The *ratio* matters; the absolute value does not —
any matched pair from ~4.7 k to ~22 k gives the same 0.500.

**Why these values.** ×2 is exact arithmetic and two resistors off one reel
track each other. 6.60 V of headroom covers WHITE shorted to a 5 V rail, or an
over-range fault, without clipping into nonsense. 5 kΩ source with C1 as the
sampling charge reservoir suits an MCU ADC. Resolution is a non-issue at
0.04 psi/count against a gauge specced around ±1 psi, so reaching only 68% of
the ADC range costs nothing worth having.

**Protection.** A BAT54S at the input pin — one Schottky leg to GND, one to
3V3. Without it, WHITE chafing onto the gauge's fused 12 V in the same harness
puts ~1.1 mA through the module's internal clamp indefinitely. The ~1 mA it
injects into the 3V3 rail is absorbed by the module's own supply current. That
clamp leg is the only thing the 3V3 pin does here; a divider is not referenced
to 3V3.

## Wiring

1. **Build the divider at the module**, not at the gauge. The divider output is
   a 5 kΩ node and that is the noise-sensitive part — solder R1, R2, C1 and the
   BAT54S straight onto the module's pins. The long run from the gauge carries
   its low-impedance output, which does not care.
2. **Run WHITE and BROWN as a twisted pair.**
3. **WHITE → R1.** R1's other end is the junction feeding AIN0.
4. **BROWN → the module's GND pin**, at the same point as R2 and C1.
5. **Gauge power is separate** — switched, fused 12 V (AEM: 5 A inline fuse)
   with its own power ground to chassis. Not from the Pi or the HAT, which
   would leave it lit with the truck parked.
6. **Module and divider stay in the cabin.** The gauge is not waterproof per
   the manual, and a bare perfboard divider less so.

### Why BROWN on module GND is correct, not a compromise

BROWN does two jobs and they want the same connection.

It is the **differential negative**: the gauge drives WHITE relative to BROWN,
so measuring WHITE against module GND gives WHITE − BROWN once BROWN *is*
module GND.

It is also the **bond to the gauge's power-ground reference**. The manual's
bench-testing note is the tell — off the vehicle you must jumper BROWN to the
gauge's power ground yourself, because the gauge does not tie them internally:
*"this connection is usually made by the circuitry inside an ECU or data
logger."* In the truck that bond already exists: BROWN → module GND → Pi
ground → chassis → gauge power ground.

**Bench-testing trap:** with the gauge on a separate supply, BROWN must be
jumpered to that supply's ground or the reading is meaningless.

BROWN carries only the divider's 225 µA, so its IR drop is microvolts (a few
metres of 22 AWG is ~0.16 Ω, or 36 µV). That is why this works and the
alternative does not: tie BROWN to a chassis stud and rely on the chassis as
the reference, and whatever drop sits between those two chassis points lands
straight in the pressure reading. **20 mV is 0.5 psi** — the same point
§13.5.4 makes about single-ended analog inputs having no common-mode
tolerance.

One caveat worth measuring rather than assuming: forcing BROWN to the Pi's
ground means the gauge's output stage drives against a reference possibly tens
or hundreds of millivolts off its own power ground. A driven differential
output handles small offsets, but measure it.

### Pi-side device naming

Pin the serial device by **USB port path, not serial number.** Plain CH340s
ship with no serial number descriptor, so a udev rule matching on serial never
fires and `/dev/ttyUSB*` ordering falls back to probe order. Match on
`KERNELS`/`ID_PATH` for the physical port and always use the same one. This is
the same rule AGENTS.md applies to the gs_usb adapter, for the same reason:
a name must never depend on probe order.

## Scaling

```
volts = k × (counts − counts_zero)
psig  = 25.00 × volts − 12.50
```

With a nominal `V_ref` of 3.300 V and an exact ×2 ratio that is
`psig = 0.040293 × counts − 12.50`. **Treat that as a placeholder** — it
assumes a reference and a ratio neither of which is true to better than a
couple of percent on this hardware. `k` comes from calibration.

| Gauge V | ADC V | Counts | psig |
|---|---|---|---|
| < 0.50 | — | < 310 | **SENSOR ERROR** |
| 0.50 | 0.250 | 310 | 0.0 |
| 1.50 | 0.750 | 931 | 25.0 |
| 2.50 | 1.250 | 1551 | 50.0 |
| 3.50 | 1.750 | 2172 | 75.0 |
| 4.50 | 2.250 | 2792 | 100.0 |
| > 4.50 | — | > 2792 | **SENSOR ERROR** |

The pre-kit rail runs around 51 psi — 2.54 V, 1576 counts, close to mid-scale.

### The 0.50–4.50 V window is a gate, not a note

Outside 310–2792 counts the decoder **returns a fault state, never a number**.
Per *make it impossible, not documented*: a required field or a refusal, not a
docstring.

The reason is specific. Clamp an out-of-range reading to the nearest valid
value instead and a broken WHITE wire, sitting at 0 V, decodes to **0.0 psig** —
a plausible-looking lie, and exactly the shape of thing that becomes a
retracted finding. The gate is also free diagnostics: an open WHITE and a
WHITE-to-BROWN short both read 0 V, so both land in the fault band rather than
reading as zero pressure.

Expected consequence: at zero rail pressure the gauge sits at 0.50 V, on the
boundary. Key-on with no fuel pressure it will flicker in and out of the fault
band. That is the sensor's floor, not a wiring problem.

## Calibration

Because AEM publishes the volts-to-psi leg exactly, **only the electrical chain
is calibrated** — counts to volts. That is the one leg actually unknown, since
it carries the module's reference error and the resistor tolerance.

1. **Zero.** Disconnect WHITE at the divider; R2 pulls AIN0 to GND, a true 0 V
   input. Read counts — that is `counts_zero`, the ADC offset.
2. **Span.** Reconnect WHITE. At two or three rail pressures, record DMM volts
   across WHITE–BROWN **and** ADC counts at the same moment. A DMM is ~10 MΩ,
   so it loads the output negligibly.
3. **Fit.** `k = volts / (counts − counts_zero)` per point. Drift across points
   means something is nonlinear and is worth chasing before trusting the fit.
4. **Cross-check against the gauge face.** Compute psig from the ADC and
   compare to the display. The *reproduce a known answer* step: the face is
   quantised to whole psi, so ~1 psi agreement is all it resolves, but that
   catches a swapped resistor, a wrong ratio, or a reference nowhere near
   3.3 V.

The DMM is truth for the electrical fit; the gauge face is the independent
check only. Calibrating against the face directly adds no information about the
chain — it is derived from the same sensor, quantised coarser, and the leg it
would calibrate is the one AEM already gives us.

Pin the result in `catalog_pins` like every other decode constant.

### Loading check

At a steady pressure, read WHITE–BROWN with the divider disconnected, then
connected. Sag means the gauge's output has real source impedance and the
20 kΩ divider is loading it — roughly 200 Ω of source impedance per 1% of sag.
A *fixed* source impedance is benign: the scale error is linear and `k` absorbs
it completely. Measure anyway, to know why `k` landed where it did and to catch
the unkind case where the impedance is not constant across the range. More
than ~0.5% sag and move to 47 k / 47 k, keeping the 100 nF (corner then 68 Hz).

## Status

- [ ] Module identified (board markings, ADC chip, channel labelling, baud)
- [ ] AIN→GND and AIN→3V3 confirmed open (no onboard input network)
- [ ] Wire colours metered against the manual
- [ ] BROWN-to-power-ground continuity checked (expect open)
- [ ] Divider built and connected
- [ ] Ground offset BROWN→module GND measured under load
- [ ] Loading check
- [ ] `k` and `counts_zero` fitted, pinned in `catalog_pins`
- [ ] Cross-checked against the gauge face
- [ ] Collector written — nothing in `src/` reads an analog channel today
