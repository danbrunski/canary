# CANary-HAT — Rev 2.1 (proposed)

> **Status: proposal, under discussion — nothing here is in KiCad.** Rev 2.0 is
> in assembly and is not being changed. Rev 2.1 is a delta against rev 2.0 as
> it exists in `hardware/canary-hat/` (57 components, `U8` ADS1015 on AIN0
> only, `J2` MKDS 1,5/6 + `J3` MKDS 1,5/2). Reference designators below are
> placeholders to make the text unambiguous; they are assigned for real at
> schematic entry.

> **Part of the CANary-HAT board documentation.** Section numbers continue the
> stable scheme: this file is **§13.5**, extending §13 in
> [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md). A bare `§3.9` still
> means the section in [`CANary-HAT-Circuits.md`](CANary-HAT-Circuits.md).

## 13.5 Rev 2.1 — boosted-engine instrumentation

### 13.5.1 Why this revision exists

The Xterra is getting a **Z1 Off-Road VQ40 supercharger kit, level 2**
(install manual `Z124108-IM`, rev IR 12/1/2025), with an **Aeromotive 340**
fuel pump, **~700 cc Bosch injectors**, and Z1 fuel rails. Two new sensors come
with that, and neither has anywhere to go on rev 2.0:

1. **Intake manifold MAP/IAT sensor — Bosch `0 261 230 416`** (VW/Audi OE
   `06L906051K`, EA888 Gen3 TMAP), mounted in the back of the intercooler. The
   kit manual is explicit that it is **not used by the vehicle**: *"This sensor
   will remain unplugged and is not used for this Supercharger System at this
   time"* (step 7, page 38). The VQ40 ECM has no MAP input and the tune stays
   MAF-based. **The sensor is CANary's alone** — nothing is spliced into the ECM
   harness, and it is the truck's only boost measurement.
2. **AEM X-Series fuel pressure gauge `30-0301`** (0–100 psig), sensor in the
   port at the back of the Z1 fuel rails. **It exposes an analog output —
   brown and white wires** (operator, inspected 2026-10-03), not the AEMnet
   CAN pair this document assumed through 2026-10-02. That changed the shape
   of the revision: see §13.5.4.

Rev 2.0 has one CAN channel (vehicle bus) and one 4-channel ADC with a single
routed input. Rev 2.1 **replaces the ADC with an 8-channel part**, adds a
**protected 5 V sensor supply**, and adds the **terminal positions** for four
new analog signals.

> **Revision history of this plan.** Until 2026-10-02 rev 2.1 also added a
> second on-board CAN channel (CAN1), on the premise that the fuel gauge spoke
> AEMnet. It does not. With its only node gone, CAN1 was dropped (operator
> decision, 2026-10-03) and its SPI0 **CE1** chip select went to the new ADC
> instead — which is what made an 8-channel SPI part affordable in GPIO terms.
> Aux CAN buses are USB adapters (§13.5.4).

**Why these two belong on one board.** The fuel system is returnless: the rail
pressure is set by a regulator that does not know manifold pressure. Under
boost, the pressure *across the injector* falls:

```
injector ΔP = P_rail(gauge, AEM) − (MAP(abs, Bosch) − baro(abs, U5 BMP384))
```

Injector flow goes with √ΔP, so e.g. 7 psi of boost on a 51 psi rail is
√(44/51) ≈ **7% less fuel at the same pulse width** — which the tune must
compensate for. Three sensors on one board, one clock, make that a measured
channel instead of an assumption. The 51 psi figure is the pre-kit supply; the
AEM gauge answers what the new system actually runs on first key-on.

### 13.5.2 Change summary

| # | Change | Parts (placeholder refs) |
|---|---|---|
| 1 | Protected 5V sensor supply off `+12V` | U9 TPS7A1650-Q1 + caps + EN resistor |
| 2 | **U8 ADS1015 → ADS8688** — 8-ch, 16-bit, SPI0 **CE1**, programmable 0–5.12 V ranges with ±20 V input protection (§13.5.3 item 5) | U8 ADS8688 (TSSOP-38), decoupling, AVDD from `+5V` |
| 3 | MAP input, **direct — no divider** | C19 (filter only) |
| 4 | IAT (NTC) input | R19 pull-up, C20 |
| 5 | Sensor-supply sense (ratiometric reference), direct | C21 |
| 6 | **Fuel-pressure input** — gauge WHITE direct to CH4, BROWN bonded to analog ground (§13.5.4) | C22 only; **one channel, no divider, no reference** — settled from the manual 2026-10-03 |
| 7 | ~~Second CAN channel (CAN1)~~ — **dropped 2026-10-03**, the gauge is not AEMnet (§13.5.4). CE1 goes to U8 | — (U10, U11, FL2, R24/R25, C24, JP2, D7/R26 all never added) |
| 8 | ~~Divider networks for MAP / IAT / 5V_SENS~~ — **deleted**, the ADS8688 takes 0–5 V directly | R17/R18, R20/R21, R22/R23 and the 15 kΩ + 4.99 kΩ BOM values all dropped |
| 9 | `J2` → two-level terminal block, 12 positions | J2 MKKDS 1,5/6-5,08 |
| 10 | ~~Correct U8's stale schematic note~~ — **moot**, the part is replaced (§13.5.6) | — |
| 11 | **One 40 MHz oscillator replaces the Y1 Pierce loop**; Y1 + C5/C6 deleted | X1 MEMS oscillator, C25 100n, R27 33 Ω |
| 12 | **BT1 polarity reversed** — swap BT1's GND and D2-anode pins (§13.5.6) | BT1 (net change) |
| 13 | **U5 barometer: fix whatever the rev 2.0 investigation finds** (§13.5.6) | U5 — TBD |
| 14 | **J1 BOM line names the 13.5 mm stacking header by part number** (§13.5.6) | J1 (BOM only) |
| 15 | **Fix J4 colliding with the Pi 5's USB ports** (§13.5.7): either swap edges (U7 + J4 to the terminal-block edge, some terminals to the right edge) or replace J4 with a U.FL and a U.FL→SMA pigtail, leaving everything in place | J4; U7, J2/J3 if edges swap |

**Roughly +8 components net**, on a 65 × 56 mm board that already carries 57.
Dropping CAN1 and the dividers took about 18 parts back out of the ~25 the
earlier plan added, which changes §13.5.7's conclusion: **single-sided
placement is now the expectation, not the hope.** The ADS8688's TSSOP-38 is
the one part that got bigger.

### 13.5.3 Analog front end — MAP, IAT, fuel pressure

#### Sensor facts, and what is still unverified

The only datasheet found is Bosch's *pressure sensors* catalogue for the
DS-S2 / micromechanical TMAP family (`0 261 230 030/042`, `0 281 002 437` …),
not the `0 261 230 416` itself. Treat the family values as design guidance and
**measure the actual part before committing values**:

| Parameter | Family value (Bosch catalogue) | Consequence here |
|---|---|---|
| Supply | 4.5 / 5.0 / 5.5 V, abs max **16 V** | Dedicated 5V rail, §13.5.3 item 1 |
| Supply current | 6.0 / 9.0 / 12.5 mA | Trivial for a 100 mA LDO |
| Output span | 0.40 V @ p1 … 4.65 V @ p2 (curve 1) | Pressure range p1…p2 **unknown for this part** — calibrate |
| Diagnostic clamp | low 0.25–0.35 V, high 4.75–4.85 V | Readings outside 0.35–4.75 V = wiring fault, not pressure |
| Output load | pull-up ≥ 5 kΩ, pull-down ≥ 10 kΩ | 25 kΩ divider to GND is inside the limit |
| NTC | **2.5 kΩ ±5 % @ 20 °C**, −40…+130 °C, τ63 ≤ 10 s | Pull-up sized for ≤ ~1 mA measuring current |
| Bosch's example ECU input | NTC pull-up 2.61 kΩ to 5 V, 1.5 nF / 33 nF filters | Reference point, not copied — see IAT below |
| Pinout | 4-pin; commonly 1 GND, 2 NTC, 3 +5 V, 4 pressure | **Unverified for this part** — meter it: the NTC pin reads ~2.5 kΩ to GND at room temp |

Mating connector: a 4-way VW-style housing — part number to confirm against the
sensor in hand.

#### 1. Sensor supply — U9, and why not the Pi's 5V

**U9 = TI `TPS7A1650-Q1`**: fixed 5.0 V, 100 mA, **60 V input**, 5 µA IQ,
enable and power-good, HVSSOP-8 (`DGN`).

- **Why a separate rail at all.** The sensor harness runs into the engine bay.
  A chafed 5V wire on the Pi's `+5V` would brown out the Pi and U4;
  on its own current-limited LDO it costs one sensor.
- **Why this part, not the smaller TPS7B6950-Q1.** §3.7.1 sets the board rule:
  D1 clamps at **38.9 V**, so **50 V is the rating floor for anything on
  `+12V`**. The TPS7B69-Q1 is a 40 V part. The TPS7A16 is 60 V.
- **EN from `+3V3`** (through a resistor, or the part's EN directly if its
  threshold permits — check the datasheet). The sensor is powered only when the
  Pi's logic rail is up, so the MAP and IAT dividers **never drive U8's inputs
  while its VDD is dead**. That keeps §13.2.3's cross-domain argument confined
  to the one net it was made for (`V12_SENSE`) instead of adding three more.
- **Dissipation** is (14.4 − 5.0) V × 12.5 mA ≈ 0.12 W. Fine in HVSSOP.
- **Cranking** drops U9 out of regulation briefly. That is why CH3 measures
  the rail instead of assuming 5.00 V.
- Input/output capacitors per the TPS7A16 datasheet (≥ 50 V rating on the input
  side, by the same rule).

#### 2. MAP → CH1 (direct, no divider)

```
J2-U2 (MAP) ──┬── U8 CH1          (range 0–5.12 V)
              └── C22 100n ── GND
```

**The divider is gone.** This is the single biggest simplification the
ADS8688 brings. Its per-channel programmable range includes **0–5.12 V**, and
its analog inputs are specified to survive **±20 V** regardless of the range
selected — so a 0–5 V automotive sensor connects straight to the pin. R17/R18
and the matched-reel tolerance argument that went with them are deleted.

- **Headroom.** 4.65 V span top, 4.85 V diagnostic clamp and a 5.5 V supply
  fault all sit inside the 0–5.12 V range; the 5.5 V case clips the reading
  but harms nothing. Compare the old path, where the sensor's 0–5 V output
  could not reach a 3.3 V-powered ADS1015 at all without attenuation.
- **Fault cases are the ADC's problem now, by design.** Harness shorted to
  12 V, or D1's 38.9 V clamp: both are inside or near the specified input
  protection rather than relying on a series resistor to limit current into a
  clamp diode. **Confirm the ±20 V figure and whether it is specified with
  AVDD live, dead, or both** — if protection is only guaranteed powered, a
  series resistor comes back. This is the one electrical claim the whole
  no-divider simplification rests on.
- **Loading.** A direct connection, so Bosch's **≥ 10 kΩ pull-down
  requirement is no longer satisfied by the divider's 25 kΩ.** Add an explicit
  pull-down resistor to GND for that purpose — it is a sensor requirement, not
  an ADC one, and deleting the divider deleted it by accident. *(Caught on
  review; size it from the Bosch datasheet with item 1's pinout.)*
- **Filter.** C22 alone against the ADC's input impedance; size the corner
  once the polling rate is chosen. The ADS8688's 500 kSPS means the
  anti-alias corner is no longer pinned near a few hundred Hz, so this filter
  becomes a deliberate choice rather than a constraint — see item 5.
- **Resolution.** 16-bit over 0–5.12 V is **78 µV/LSB**, ~26× finer than the
  ADS1015's 2.0 mV, and it is now at the sensor rather than behind a 0.6
  divider. Across a 4.25 V span that is ~54,000 counts. Resolution has stopped
  being a design consideration on this channel.

#### 3. IAT → CH2

```
U9 5V_SENS ── R19 ──┬── J2-U3 (IAT) ── NTC (in sensor) ── J2-U6 (SENS_GND)
                    │
                    └──┬── U8 CH2          (range 0–5.12 V)
                       └── C20 100n ── GND
```

**R20/R21 are gone** — the pull-up node swings 0–5 V and the ADS8688 takes it
directly. Two consequences, both improvements:

- **R19 pull-up — proposed 4.99 kΩ, final value after measuring the NTC.** At
  4.99 kΩ the worst-case NTC current (NTC → 0 Ω) is 1.0 mA, which respects the
  family's ~1 mA measuring-current limit. At 20 °C (2.5 kΩ) the node sits near
  1.6 V, mid-range. Bosch's own example uses 2.61 kΩ, which buys hot-end
  resolution at up to ~1.9 mA of self-heating current. Post-intercooler air
  should rarely exceed ~80 °C, so favour low self-heating.
- **Pulled up to `5V_SENS`, not `+3V3`.** The IAT reading is then ratiometric
  against the same rail CH3 measures.
- **Nothing loads the node any more.** The old 25 kΩ divider sat in parallel
  with the NTC and had to be solved for in software with the real resistor
  values. Against the ADS8688's high-impedance input, the pull-up and the NTC
  are the whole network: the temperature solve is now R19 and the
  Steinhart–Hart fit, nothing else. One fewer place for a wrong resistor value
  to become a wrong temperature.
- **Open sensor is still detectable, and more cleanly.** With the NTC
  unplugged the node pulls to the full `5V_SENS` rail — **~5.0 V at CH2**,
  hard against the top of range and unmistakable, rather than the old 2.50 V
  which merely sat above the in-range band.
- **Curve.** The catalogue gives one point (2.5 kΩ @ 20 °C), not a table.
  Measure the NTC at three known temperatures (ice water, room, hot water with
  a reference thermometer) and fit Steinhart–Hart; that beats hunting for a
  table that may belong to a different Bosch NTC.

#### 4. Sensor-supply sense → CH3

`5V_SENS` straight to CH3 with C21 as the only part. **This channel survives
the move to the ADS8688, and it is worth being clear why**, because the
no-divider change removes one of its two original jobs:

- It no longer has to cancel a **divider ratio** — there is no divider.
- It still has to cancel **U9's tolerance, its cranking dropout and its
  temperature drift**, because the Bosch sensor is *ratiometric to its own
  supply*: its output is a fraction of whatever U9 actually delivers, not of
  5.000 V. That is a property of the sensor, not of the input network, so no
  ADC change can make it go away.

```
ratio    = code_CH1 / code_CH3              # dimensionless
MAP_kPa  = a · ratio + b                     # a, b: THIS sensor's transfer function
boost    = MAP_kPa − baro_kPa (U5)
```

The ADS8688's own reference (internal 4.096 V) cancels out of that ratio too,
so its drift does not reach the result either.

**`a` and `b` are not known yet and must not be guessed.** For a 0.5–4.5 V,
50–400 kPa (4-bar) sensor they would be 437.5 and 6.25; for a 2.5-bar part they
are different numbers, and the `0 261 230 416`'s range is unconfirmed. Any
placeholder that lands in code becomes a confident wrong reading — exactly the
failure `AGENTS.md` records six retractions for. Fix them from the key-on baro
point and the leak-tester point (item 6), or from a datasheet for this exact
part number, and pin them in `catalog_pins` like every other decode constant.

Sensed at the board, so harness drop (≤ 12.5 mA through a few metres of 22 AWG)
is not included — small, and a calibration term if it ever matters.

#### 5. U8 becomes an ADS8688 — channel map, rate, and what it costs

**Decision (operator, 2026-10-03): replace the ADS1015 with an 8-channel
ADS8688 on SPI0 CE1**, rather than adding a second ADS1015 at I²C 0x49. The
AEM gauge turning out analog (§13.5.4) needed a fifth input and the ADS1015 is
a 4-channel part with all four spent; dropping CAN1 freed the CE1 chip select
that makes an SPI part cost no GPIO.

| Ch | Signal | Range | Notes |
|---|---|---|---|
| CH0 | `V12_SENSE` | 0–5.12 V | **R11/R12 divider kept as built.** 12.68 V × 0.18 = 2.28 V, mid-range |
| CH1 | `MAP` | 0–5.12 V | direct (item 2) |
| CH2 | `IAT` | 0–5.12 V | R19 pull-up only (item 3) |
| CH3 | `5V_SENS` | 0–5.12 V | ratiometric reference (item 4) |
| CH4 | `FUEL_P` | 0–5.12 V | AEM gauge WHITE (analog +), direct. BROWN bonds to analog ground — item 7 |
| CH5 | spare | — | **freed 2026-10-03**: the gauge needs no reference or sense channel (item 7) |
| CH6 | spare | — | |
| CH7 | spare | — | DNF conditioning pads to a test pad, not a terminal (item 8) |

**Three channels spare**, since the gauge turned out to need exactly one. The
4.50 V top of its valid window sits inside 0–5.12 V with no divider, at
78 µV/LSB — about **0.002 psi per count** against a gauge specced near ±1 psi,
so resolution is irrelevant here by three orders of magnitude.

**What this buys:**

- **16-bit, 78 µV/LSB** on 0–5.12 V, against the ADS1015's 2.0 mV behind a
  0.6 divider.
- **No dividers** on the three sensor channels (items 2–4) — six resistors and
  two BOM values deleted, and the matched-reel tolerance argument with them.
- **±20 V input protection** native to the part, on a harness that can see
  ISO 7637 transients.
- **500 kSPS**, which retires this section's old caveat outright: the ADS1015
  gave "boost and IAT, not per-cylinder pulsation," and intake pulsation is
  now measurable. Treat that as available capability, not a reason to log at
  500 kSPS — the daemon polls slowly by default.
- **Per-channel programmable range**, so CH0 keeps rev 2.0's divider and a
  future ±10.24 V input needs no new network.

**What it costs, recorded so it is not rediscovered:**

- **SPI0 now carries the vehicle CAN controller *and* the ADC.** U3 on CE0
  needs prompt service when vehicle frames arrive; ADC polling adds traffic to
  the same bus. The old I²C ADS1015 could not interfere with CAN0 by
  construction, and that property is now gone. Mitigations: poll the ADC at
  the tens-of-Hz the signals actually need, keep its burst short, and **make
  SPI contention something the bring-up measures** — CAN0 frame-loss and
  latency with the ADC idle vs polling, as a §13.1-style measurement. If
  pulsation capture at high rate is ever wanted, it is a deliberate mode that
  accepts CAN jitter for its duration, not a default.
- **Rev 2.0's validated `V12_SENSE` path is re-validated.** `R11/R12` stay,
  but the part reading them changes, so `CANary-HAT-Revisions.md:119`'s
  measurement (0.000 V / 12.68 V / 12.43 V) is repeated on the new ADC.
- **TSSOP-38, 9.7 × 4.4 mm** — one large footprint instead of a VSSOP-10.
  Against this, ~18 parts left the board with CAN1 and the dividers, so
  §13.5.7 gets easier overall.
- **AVDD comes from `+5V`**, a rail the board already has (§13.3).

**12-bit alternative, same pinout:** `ADS8668` if 16-bit proves unnecessary.
Not worth choosing — on a one-off the price gap is a rounding error and the
resolution is free capability.

**BOM commonality.** 100 nF is the C1 group part. New values: none for the
analog front end — deleting the dividers removed the 15 kΩ and 4.99 kΩ lines
that rev 2.1 was going to add, leaving only R19's pull-up value (item 3) to
pick.

> **Unverified, and load-bearing.** Every ADS8688 figure above — the 0–5.12 V
> range, 500 kSPS, ±20 V input protection, the internal 4.096 V reference,
> TSSOP-38 and the pinout — is from recollection and **must be confirmed
> against TI's datasheet before schematic entry** (§13.5.9 item 17). The
> ±20 V protection claim is the one the whole no-divider simplification rests
> on: if it is conditional on AVDD being live, series resistors come back.

#### 6. Calibration against known answers

Nothing on the ECM reads MAP, so there is no OBD cross-check. The known answers
available instead:

- **Offset — every key-on.** Engine off, MAP must equal **U5 BMP384 baro**
  (~80 kPa at this altitude). Free, automatic, repeatable.
- **Slope — the kit supplies it.** The Z1 kit includes a **boost leak tester
  with gauge** (item 39). Engine off, pressurise the intake to a known gauge
  pressure (**≤ 10 psi**, per the manual's warning) and read MAP − baro against
  it.
- **IAT.** After a full cold soak, IAT must match `obd:INTAKE_TEMP` (the MAF
  housing's sensor) and ambient. Only after a soak — while running they measure
  different air (pre-blower vs post-intercooler), and that difference is the
  interesting part.

#### 7. Ground references — BROWN is a differential negative, and must be bonded

**Corrected 2026-10-03** against the AEM install manual, documented in
[`aem-fuel-pressure-analog-input.md`](aem-fuel-pressure-analog-input.md).
An earlier draft of this item specified the gauge's ground as a
*high-impedance sense channel* on CH5, on the assumption that it was a single
ground pin carrying the gauge's own supply current. **Both halves of that were
wrong, and the design it produced would not have worked.**

Deleting the dividers made every analog channel single-ended against the HAT's
ground plane, with no common-mode tolerance in the path. So "what is this
referenced to" is a per-channel question:

| Channel | Referenced to | Status |
|---|---|---|
| `MAP`, `IAT` | U9's `SENS_GND`, back to the HAT | **Clean by construction** — the HAT powers the sensor and provides its return, one local loop |
| `V12_SENSE` | HAT ground via OBD pin 4 (chassis) | As built in rev 2.0, unchanged |
| `FUEL_P` | **BROWN**, the gauge's own analog negative | **Settled** — see below |

**What the manual actually says.** The `30-0301`'s output is **differential**:
WHITE is analog positive, BROWN analog negative. AEM prescribes the
single-ended case explicitly — *"if the logging device or ECU does not have a
differential analog input then connect the BROWN wire to the shared signal
ground."* And BROWN is **not** tied to the gauge's power ground inside the
gauge: the manual's bench-test note says you must jumper it yourself, because
*"this connection is usually made by the circuitry inside an ECU or data
logger."*

**So BROWN must be bonded to the HAT's analog ground. It is not optional and
it is not a compromise:**

```
AEM WHITE (analog +) ──┬── U8 CH4        (range 0–5.12 V)
                       └── C22 100n ── GND

AEM BROWN (analog −) ───── analog GND, bonded at the board
```

- **The bond is what makes the measurement correct.** Once BROWN *is* our
  ground, reading WHITE against it **is** WHITE − BROWN — the differential
  quantity the gauge drives. The gauge's own negative defines zero, and any
  offset between the gauge's power ground and ours drops out.
- **A high-impedance sense channel instead would be a defect.** Leaving BROWN
  unbonded leaves the gauge's output stage without a reference, which the
  manual calls meaningless. Sensing a floating node does not recover it.
- **Once bonded, sensing it is pointless.** BROWN ≈ analog ground by
  definition; a sense channel would read the IR drop of the bond trace, which
  is microvolts. **CH5 is therefore free** (item 5), and so is the 7th
  terminal position the earlier draft needed (§13.5.5).
- **BROWN carries no meaningful current**, which is why this is safe where the
  earlier draft's worry was not. With no divider on rev 2.1 the input draws
  essentially nothing, against the rev 2.0 USB path's 225 µA. A few metres of
  22 AWG at ~0.16 Ω develops microvolts, so none of §3.9's parallel-power-
  return hazard applies. **That hazard was the premise of the earlier draft and
  it was simply not true of this wire.**
- **Bond it near U8's ground pin, not at the far end of the connector.**
  §3.9 establishes that separate analog and digital grounds are not achievable
  here — the plane is the Pi's ground — so the bond point is the only control
  available over how much plane drop sits between BROWN and the ADC's own
  reference. Star it at U8.
- **One thing still worth measuring** (carried over from the rev 2.0 doc):
  forcing BROWN to our ground means the gauge's output stage drives against a
  reference possibly tens or hundreds of millivolts off its own power ground.
  A driven differential output should handle that, but **20 mV is 0.5 psi**, so
  measure the offset under load rather than assuming it.

**The terminal is still needed — as a bond, not a sense.** BROWN gets its own
position on J2, net-tied to analog ground (one wire per clamp; `SENS_GND` is
the same net but already carries the TMAP's return). Count: six, exactly
(§13.5.5).

#### 8. Input conditioning — DNF pads, not jumpers

A pull-down and a pull-up are already *required* rather than optional: Bosch
wants ≥ 10 kΩ to ground on `MAP` (item 2, open item 20), and `IAT` needs R19 to
the sensor rail (item 3). The question raised on 2026-10-03 is whether the
board should make those, and future ones, configurable — possibly by jumper.

**Recommendation: unpopulated 0603 pads on every analog channel; no jumpers.**

```
terminal ──┬── R_pulldown → GND     (DNF)
           ├── R_pullup → 5V_SENS   (DNF)
           └── R_series (0 Ω fitted) ──┬── U8 CHn
                                       └── C 100n ── GND
```

- **The series pad on every channel is the highest-value part of this, and the
  reason is open item 17.** If the ADS8688's ±20 V input protection turns out
  conditional — guaranteed only with AVDD live, say — then *every* channel
  needs a series resistor. With pads already there, that discovery is a BOM
  change; without them it is a board revision. Fit 0 Ω and the no-divider
  simplification is intact; fit a real value and it still works.
- **Pull-up and pull-down go on the terminal side of the series pad.** Then a
  pull-down loads the sensor as Bosch requires without forming a divider
  against an ADC input that draws nothing. Put it *after* the series resistor
  and a non-zero `R_series` silently turns it into one.

**Why not jumpers** — the part worth arguing, since the flexibility is real:

- **On the four committed channels there is no choice to configure.** R19's
  value came out of the NTC curve and the family's ~1 mA self-heating limit;
  the MAP pull-down is a Bosch minimum. A jumper advertises a decision that
  does not exist and supplies a way to get it wrong.
- **A jumper is invisible state.** Software cannot read it, nothing records it,
  and a wrong position yields plausible-but-wrong engineering units — the exact
  failure `AGENTS.md` records six retractions for. §13.5.4 made this same call
  refusing a CAN0 termination footprint: it "would only ever be a fault waiting
  for someone to populate it." A DNF pad is soldered, visible, and in the BOM.
- **Vibration.** A 0.1" shunt on a vehicle-mounted board can walk off, and
  `MAP` losing its pull-down mid-drive is a silent wrong reading.
- **Area.** A three-position header per channel across eight channels is more
  board area than the ADS8688 itself. Two DNF 0603s are ~2 mm².

**Where configurability does earn its place: the spare channels.** CH6 and CH7
have no known sensor, which is the one case where the value really is unknown
until something is wired. Give the spares the full set — series, pull-up,
pull-down — brought to **labelled test pads rather than terminal positions**,
since §13.5.5 has none to give.

**If a jumper is wanted anyway**, confine it to *one* designated experiment
channel and make the position legible: silkscreen that reads as a state
(`PU / OFF / PD`), not three unlabelled pins. One channel where a wrong jumper
is possible is a bounded hazard; eight is a trap.

### 13.5.4 Fuel-pressure input — and why there is no second CAN channel

#### The gauge is analog, not AEMnet

Through 2026-10-02 this section specified a second on-board CAN channel whose
only purpose was the AEM gauge. **On inspection the gauge's harness exposes an
analog output — brown and white** (operator, 2026-10-03). With no AEMnet node
to talk to, CAN1 lost its entire justification.

**Decision (operator, 2026-10-03): drop CAN1.** Its SPI0 **CE1** chip select
goes to the ADS8688 instead (§13.5.3 item 5), which is what lets an 8-channel
SPI ADC cost zero GPIOs. Never added: U10 MCP2518FD, U11 TCAN1042, FL2,
R24/R25 split termination, C24, JP2, D7/R26. GPIO19 and GPIO27 go back to the
free pool, so **GPIO12/13 (reserved `uart4`), 19, 20, 21 and 27 are all
uncommitted** after rev 2.1 — a markedly better position than the earlier plan
left, which spent down to two.

This also keeps the board at **one** CAN controller, so §13.5.3's SPI
contention concern is ADC-against-CAN0 only, not a three-way.

#### Wiring the analog output

```
gauge white (signal) ──┬── J2-U4 ── U8 CH4      (range 0–5.12 V)
                       └── C22 100n ── GND
gauge brown  (ground) ──── J2-U6 (SENS_GND)
```

**Settled by the manual (2026-10-03): WHITE is analog positive, BROWN is
analog negative.** An earlier draft of this section said the colours had to be
metered because AEM's conventions vary — worth doing once as a check, but the
manual is explicit and BROWN is not a ground wire at all (item 7).

#### Closed: it is buffered, and needs no reference channel

**Settled 2026-10-03 from the install manual** — see
[`aem-fuel-pressure-analog-input.md`](aem-fuel-pressure-analog-input.md),
which documents the rev 2.0 USB-ADC path against the same gauge. AEM publishes
a **fixed** transfer function:

```
psig = (25.00 × Volts) − 12.50          # 0.50 V = 0 psig, 4.50 V = 100 psig
bar  = (1.72  × Volts) − 0.860
```

A fixed volts-to-psi relation means the gauge head **regenerates the output
against its own internal reference** rather than passing the pressure sensor's
ratiometric signal through. So: no reference channel, no second divider, no
fifth wire. One input.

> **A discriminator this document got wrong.** An earlier draft proposed
> metering the signal wire at zero pressure, reading **~0.5 V as "raw sensor
> passthrough, needs a reference channel."** For this gauge the 0.50 V floor is
> *by design* — it is the bottom of the published scale. The test would have
> returned "sensor-ratiometric" for a buffered output and bought a channel, a
> terminal position and a larger connector for nothing. **The published formula
> supersedes the test.** Recorded because the test looked sound and was cheap,
> which is exactly when a wrong inference lands unchallenged.

#### The 0.50–4.50 V window is a gate, not a note

Outside that window the manual reads **SENSOR ERROR** — a fault state, not a
pressure. The decoder must **return a fault, never a number**, and this is a
software contract that holds on any board, not a property of rev 2.1's ADC.

The reason is specific: clamp an out-of-range reading to the nearest valid
value instead, and a broken WHITE wire sitting at 0 V decodes to **0.0 psig** —
a plausible-looking lie of exactly the shape `AGENTS.md` records six
retractions for. The gate is also free diagnostics, since an open WHITE and a
WHITE-to-BROWN short both land in the fault band rather than reading as zero
pressure. Expect flicker at the boundary with no rail pressure: the gauge sits
at 0.50 V, which is the sensor's floor, not a wiring fault.

#### Transfer function — from the gauge face, not the range

The gauge is 0–100 psig, and **that does not give the volts-per-psi mapping.**
Do not assume 0.5–4.5 V over 0–100 psi because the range is 0–100 psi; a
0.5–4.5 V sensor on a 0–100 psi gauge is a *guess* that would read ~12 psi
high at mid-scale if the real floor is 0 V.

Derive it from the **gauge's own display as the known answer** — key on, engine
off, read the gauge face against CH4 at two or more pressures (key-on prime,
then engine running at idle gives a second point; the Z1 kit's regulator makes
a third if the rail is bled). Pin the result in `catalog_pins` like every other
decode constant. This is the same discipline §13.5.3 item 4 applies to `a`/`b`
for MAP, and for the same reason: a placeholder that lands in code becomes a
confident wrong reading.

#### Gauge power and ground

- **Gauge power does not come from the HAT.** `+12V` here is OBD pin 16 —
  permanent battery. The gauge wants **switched, fused 12 V** (AEM: 5 A inline
  fuse on pin 1). Powering it from the HAT would leave it lit with the truck
  parked.
- **Power ground is separate from BROWN, and that is the whole point.** The
  gauge's power ground goes to chassis as AEM specifies; **BROWN is the analog
  negative and comes back to the board** on its own terminal, bonded to analog
  ground near U8 (§13.5.3 item 7). The gauge does not tie the two internally.
- **Run WHITE and BROWN as a twisted pair.** They are a differential signal
  pair; treating them as "signal and ground" is what loses the benefit.
- **Why a chassis return would not do.** A single-ended input has no
  common-mode tolerance, and the HAT is bonded to chassis through J2 pin 3
  (§3.9) — so tying BROWN to a chassis stud instead would put whatever drop
  exists between the two chassis points straight into the pressure reading.
  **20 mV is 0.5 psi.** Bringing BROWN back on its own wire costs one terminal
  and removes that error term entirely, because BROWN carries essentially no
  current.

#### Aux CAN buses are USB adapters

Dropping CAN1 does not mean no aux bus — it means the aux bus is **not on the
board**. The role/transport split written for CAN1 survives intact, with one
fewer transport:

- **Software binds the aux bus to an interface name from config**, exactly as
  `hardware.can` does for the vehicle bus today — no "HAT" or "USB" branch in
  the collector. The transport is a config value. This was the right design
  when CAN1 existed and it is the reason dropping CAN1 costs no software.
- **udev pins names by hardware path**, so a name never depends on probe
  order: the gs_usb adapter by USB serial. What must not happen is `can1`
  meaning different hardware on different boots.
- **The link-bring-up unit is per-interface**, replacing the single-interface
  assumptions in `systemd/can0.service` and `70-canable-can0.rules`.
- **`restart-ms`:** the `mcp251xfd` driver accepts it; `gs_usb` on this
  firmware does not, so bus-off does not self-recover on the USB path
  (`docs/can-setup.md`). With the on-board channel gone, **this is now the
  only aux-bus behaviour**, not one of two cases.
- **An aux bus with one other node must run active, not listen-only** —
  something has to ACK or the far node retransmits to error-passive. Recorded
  because the vehicle bus is often sniffed listen-only and the habit must not
  carry over.

**Future vehicles and gauges** (the reason to keep the capability at all): a
125 kbps medium-speed bus on OBD pins 3/11, or a future AEMnet device, is a
**CANable 2 on USB** (`docs/canable-firmware.md`) — no header cost, no board
revision, and CAN FD capable if something wants it. GM's single-wire CAN on
pin 1 needs a different transceiver and was never in scope for an on-board
channel either.

#### AEMnet, recorded in case the gauge has it too

AEM X-Series heads commonly carry **both** AEMnet and an analog output. If this
gauge turns out to have a CAN pair as well, nothing here needs revisiting — the
analog path is simpler, already specified, and a USB adapter covers AEMnet if
it is ever wanted. The decoded message is kept so the work is not repeated
(AEM `10-0301` manual, p. 8):

| Field | Value |
|---|---|
| Bit rate | **500 kbps**, classic CAN (no FD) |
| Identifier | **29-bit, `0x000A0301`** |
| DLC / rate | 8 / **20 Hz** |
| Signal | Pressure (gauge), **16-bit unsigned, big-endian, 0.01 bar/bit**, offset 0 |
| Termination | **None in the gauge** |
| Wires | CANH white/black, CANL green/black |

The manual's table is internally inconsistent — it places a 16-bit value in
"byte 0" and marks bytes 1–7 unused, almost certainly meaning bytes 0–1.
**Unverified, and no longer on the critical path.**

### 13.5.5 Terminal block — `J2` goes two-level

#### The space problem

**Recounted 2026-10-03**, after the AEM manual settled the gauge's wiring
(§13.5.3 item 7). The upper level's six positions are **exactly consumed**:

| Harness | Positions | |
|---|---|---|
| Bosch TMAP (one 4-way connector) | `5V_SENS`, `MAP`, `IAT`, `SENS_GND` | 4 |
| AEM gauge | `FUEL_P` (WHITE, analog +), `FUEL_GND` (BROWN, analog −) | 2 |
| | **Total** | **6 — exactly fits** |

**The 6-position two-level block below is sufficient.** An earlier draft of
this section carried a contingency for a 7th position — `MKKDS 1,5/7-5,08`, or
re-purposing the unconnected OBD-pin-5 terminal — against the possibility that
the gauge needed a ratiometric reference wire. **It does not** (§13.5.4): AEM
publishes a fixed transfer function, so there is no reference channel and no
fifth wire. That contingency is dropped, and with it the ~2 mm fit margin it
would have eaten between MH3 and MH4.

**`FUEL_GND` is a bond, not a sense line.** BROWN is the gauge's differential
negative and net-ties to analog ground on the board — its own terminal because
one clamp takes one wire, not because it is a different net from `SENS_GND`.

From the rev 2.0 PCB:

- `J2` MKDS 1,5/6, pin 1 at x = 111.5 → body ≈ **x 109–139.5**
- `J3` MKDS 1,5/2, pin 1 at x = 145.455 → body ≈ **x 143–153**
- `MH3`/`MH4` at x = 103.5 / 161.5 on the same (y ≈ 96.5) edge

The usable bottom edge between the mounting holes is ~51 mm and rev 2.0 already
uses ~44 mm of it. **Six more positions at 5.08 mm (+30.5 mm) do not fit on one
level.**

| Option | Result |
|---|---|
| **A. Two-level MKKDS 1,5/6-5,08 in J2's place (recommended)** | 12 positions in J2's existing width and pitch. Same screwdriver, same 17.5 A / 1.5 mm² rating as MKDS 1,5. J3 unchanged. |
| B. Single-level MKDS 1,5/12-5,08 | ~61 mm. Does not fit between MH3 and MH4. |
| C. New 3.5/3.81 mm block on another edge | Right edge holds J4 (SMA) and the Q2/Q3 K-line switch; left edge has Y1/U3. Needs a re-layout for less gain than A. |
| D. Pluggable (MC 1,5 header + FK-MC plug) | Nice for unplugging the harness under the dash, but a 14-way 3.81 mm header is ~53 mm — same fit problem as B. |

**Option A needs verifying before layout:**
- Phoenix confirms the MKKDS 1,5 series at 5.08 mm in 2- to 11-position
  variants, and a 6-position part exists (`MKKDS 1,5/ 6-5,08 BK`, item
  1016295). **The standard-colour item number, overall height, and the offset
  between the two pin rows still need reading off the Phoenix datasheet.**
- **No stock KiCad footprint** was confirmed; expect to draw one from the
  datasheet (two staggered rows of pins).
- **Height** grows compared with single-level MKDS. Check it against the
  enclosure and the lid, not just the stack.

#### Proposed pin map

**The lower level is rev 2.0's J2, pin for pin.** The vehicle harness was
re-landed once already for rev 2.0 (§3.9's warning); rev 2.1 must not move it
again. Columns are grouped so each one reads as a single function.

| Column | Lower level (vehicle OBD — unchanged from rev 2.0) | Upper level (new) |
|---|---|---|
| 1 | `+12V_RAW` (OBD 16) | `5V_SENS` — sensor supply |
| 2 | GND (OBD 5, left unconnected per §3.9) | `MAP` — pressure signal |
| 3 | GND (OBD 4, chassis) | `IAT` — NTC signal |
| 4 | `CANH_RAW` (OBD 6) | `FUEL_P` — gauge signal |
| 5 | `CANL_RAW` (OBD 14) | `FUEL_GND` — gauge BROWN (analog −), **bonded to analog ground** (§13.5.3 item 7) |
| 6 | `KLINE` (OBD 7) | `SENS_GND` — sensor return |

Why this arrangement:
- **Column 1 = supply** (12 V below, 5 V above).
- **Each sensor's wires stay adjacent.** `MAP` + `IAT` (columns 2–3) are the
  two signals from the one Bosch TMAP connector; `FUEL_P` + `FUEL_GND`
  (columns 4–5) are the gauge's pair. The operator lands this harness by hand
  under a dash, and grouping by harness is what prevents a miswire.
- **The Bosch sensor's signals get the two quiet columns.** They sit over
  GND (columns 2–3), away from both the K-line and the vehicle CAN pair.
- **`SENS_GND` sits over `KLINE`.** The K-line carries ISO 7637-2 pulses to
  +100 V / −150 V (§3.9); putting a ground above it rather than an analog
  input is the conservative choice.
- **The gauge pair sits over the vehicle CAN pair, and that is a deliberate
  second-best.** With CAN1 gone there is no longer a CAN signal to pair above
  CAN, and there are only two columns over ground for four analog signals, so
  something analog has to sit over CAN. The gauge signals are the right
  choice: they are **driven outputs** (a buffered gauge head, or a sensor with
  its own driver), not high-impedance sensor nodes, and C22 filters what
  little 500 kbps edge energy couples across a 5.08 mm two-level block. If
  bring-up shows CAN edges in the pressure trace, the fix is the filter
  corner, not a re-land.
- **`FUEL_GND` and `SENS_GND` are the same net on two terminals**, which is
  ordinary practice — one wire per clamp. Both may read "ground" on the
  silkscreen without hazard, unlike the earlier draft where one of them had to
  stay unbonded. **Bond BROWN near U8**, though, not at the connector
  (§13.5.3 item 7).
- **There is no growth room left on this connector.** Spare ADC channels
  (CH5–CH7) go to labelled test pads, not terminals (§13.5.3 item 8).
- `J3` (+12V pass-through to the X1202) is **unchanged**.

### 13.5.6 Carry-over fixes

Defects in rev 2.0 itself, found on the first assembled board (bring-up
2026-09-23, §13.1.1 of [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md)).
They go into rev 2.1 whatever happens to the boosted-engine work above.

#### BT1 — GNSS backup cell polarity is reversed

On the first board the cell's polarity at BT1 is backwards: the pin that should
carry the cell's + (to D2's anode, then V_BCKP) is the one wired to GND.
**Fix: swap BT1's two pin connections, GND ↔ D2 anode.** It is a net change
only; the footprint and D2 stay as they are.

- **Rev 2.0 boards are not damaged by it.** With the cell reversed, D2's anode
  sits about 3 V *below* GND, so D2 blocks and V_BCKP gets nothing. The
  receiver cold-starts on every boot, as if no cell were fitted. The workaround
  on rev 2.0 is to swap the two crimps in the cable's JST-SH housing, not to
  rework the board.
- **Make it impossible next time.** JST-SH battery leads have no standard
  polarity: vendors wire red to either pin. Pin the exact cell/lead part number in
  [`BOM-offboard.md`](../hardware/canary-hat/BOM-offboard.md), mark `+` in the
  silkscreen beside BT1, and check the chosen lead against the footprint before
  ordering. "A 2-pin JST-SH cell" alone is what let this happen.

#### U5 — BMP384 powers up but never converts

Symptom (§13.1.1): chip ID `0x50`, NVM, error register, sensor-time counter and
data-ready flags are all correct, but every conversion returns temperature
**exactly 0** and pressure pinned at ≈`0x801C00`. Bring-up concluded "dead part,
replace U5". **That has not been shown yet.** One board is one sample, so a
design or process fault that would recur on rev 2.1 has not been ruled out.
Work through the list below on the rev 2.0 boards before deciding what rev 2.1
changes.

**Register-level result (2026-09-23, `firmware/baro_diag.py` on the first
board).** The temperature channel is dead on its own. The commands, the
pressure channel and the digital core all work.

| Test | STATUS | ERR | Pressure word | Temperature word |
|---|---|---|---|---|
| After soft reset | `0x10` | `0x00` | `0x800000` | `0x800000` (both at the datasheet reset value) |
| Forced, temp only (`PWR_CTRL=0x12`) | `0x50` (drdy_temp) | `0x00` | `0x800000` (untouched) | **`0x000000`** |
| Forced, press only (`0x11`) | `0x30` (drdy_press) | `0x00` | `0x802000`–`0x802C00` | `0x800000` (untouched) |
| Forced, both, OSR ×1, ×10 | `0x70` | `0x00` | spread 1,024–3,072 LSB | `0x000000` every time |
| Forced, both, OSR p×32/t×8, ×5 | `0x70` | `0x00` | spread 40–112 LSB | `0x000000` every time |
| Normal mode, OSR ×1, ODR 12.5 Hz | `0x70` | `0x00` | `0x8030xx`–`0x8036xx` | `0x000000` |

What that shows:

- **The commands are right.** Each enable bit gates exactly its own channel,
  the matching drdy bit sets, ERR_REG stays 0, and the sensor-time counter
  runs. Every channel that was *not* enabled keeps the reset value `0x800000`,
  so a rejected or missing command would leave `0x800000`, not `0`. The chip
  reports that it finished a temperature conversion, and the result is zero.
- **The pressure channel responds to real pressure.** Blowing on the lid
  (2026-09-23, OSR ×8, 50 Hz) moved the raw word by about +90,000 counts
  (about 1.4 kPa). It returned to baseline within 0.5 s of stopping, and the
  temperature word stayed `0x000000` in all 63 half-second samples. The *raw
  level* proves nothing by itself: with these trims the compensated result
  moves only about 16 Pa per 1,000 counts, so any word near `0x80xxxx` comes
  out near 81–83 kPa. Absolute accuracy is unverified. At the IMU's 37.3 °C
  die temperature it reads 83.3 kPa against a phone's 80.42 kPa.
- **Both readout paths agree.** The FIFO (unfiltered, press+temp frames)
  carries temperature `0x000000` as well, so this is not a data-register
  readout effect.
- **NVM trims are plausible** (T1=27364, T2=19153, T3=−12; for comparison,
  `0x802A00` read as a temperature decodes to 24.8 °C). Uncompensated
  temperature `0` decodes to −127 °C: the temperature ADC is pinned at its
  bottom code.
- **Nothing on the board can cause this.** The package's only external
  connections are supply, I²C, straps and INT, all of which demonstrably work.
  The temperature path is entirely inside the die. That leaves a damaged part
  (ESD, reflow, or cleaning at assembly, or a bad die) rather than the
  schematic. One exception is not yet excluded: the rail's power-up ramp,
  which the datasheet constrains (§3.2, added in its rev 0.91) and nobody has
  measured. A cold power cycle did not clear it (check 4), so a POR fault would
  have to recur on every power-up to explain this, so check 1 (a second board) decides whether rev 2.1 needs a
  *process* note.

**Decision (operator, 2026-09-23): treat U5's temperature channel as broken
and compensate its pressure with U6's die temperature.** Investigation stops
here; checks 1–5 below remain available but are not scheduled. The BMP384
driver, which does not exist yet, takes its compensation temperature from U6
(`src/collection/lsm6dsox.py` already batches die temperature at 1.6 Hz) and
never from U5's temperature word. The surrogate is a property of this board
revision, not of the BMP384, so it belongs in board or profile config and not
in the driver's code path. Until the known-answer check below passes, the
channel carries its provenance and is not trusted as absolute pressure.

**Salvaging U5 on rev 2.0 without rework.** Compensation needs a temperature,
not specifically U5's own. U6 (LSM6DSOX) is 11 mm away on the same board and
has a die temperature sensor. Pressure sensitivity to the assumed temperature
is about **188 Pa/°C** at this raw value, so a 1 °C gap between the two dies
costs about 0.19 kPa (about 17 m of altitude). That is usable as an absolute
barometer only after it passes a known-answer check: record the ECM's
barometric PID (Mode 01 PID 0x33) alongside it through a drive with real
altitude change, and compare the pairs with `compare_channels`. It is not good
enough for `derived:GRADE`, where the two dies' temperature drift would show up
as grade. The ~3 kPa gap to the phone at the IMU temperature is what that check must
explain: a die-temperature gap between U5 and U6, or an offset in U5's
pressure path.

**Already ruled out (checked against the PCB file, 2026-09-23):**

- **Pinout.** Pads match the BMP384 datasheet: VDDIO=1, SCK=2 (SCL1), VSS=3/8/9,
  SDI=4 (SDA1), SDO=5→+3V3 (addr 0x77), CSB=6→+3V3 (I²C), INT=7 (GPIO17),
  VDD=10. A rotated or mirrored footprint would move SCK/SDI off the bus, and
  the part answers, so orientation is right too.
- **Decoupling present.** C10 and C13 (100 nF, +3V3↔GND) sit about 3 mm either
  side of U5.

**Still to check, cheapest first:**

1. **Is it this part or every part?** Five boards were built. Run
   `checkout_hat.sh` on a second board. If every board fails the same way, it is
   design or process. If only one does, it is the part. This check decides the
   rest.
2. **Rails at the pins.** Measure VDD (pin 10) and VDDIO (pin 1) *at the
   package* while a conversion is running, on a scope as well as a meter. The
   core can answer I²C at a VDD that is too low for the analog front end (POR is
   1.58 V, operating minimum 1.71 V), so a high-resistance joint on pin 10 alone
   is not excluded just because the digital side works. X-ray or inspect
   pins 8–10 for voids and tombstoning; LGA joints can't be seen from the top.
3. ~~**The command sequence.**~~ **Ruled out 2026-09-23** — see the result
   below.
4. ~~**A real hard reset.**~~ **Done 2026-09-23, no change.** Pi unplugged
   (12 V left connected), cold boot. U5 came up with registers at defaults and
   `EVENT.por_detected` set, so it saw a genuine power-on reset. Temperature
   was still `0x000000` and pressure still converted.
5. **Assembly process.** Ask PCBWay whether the boards were washed (aqueous or
   ultrasonic) after reflow, and what reflow profile and moisture handling
   (MSL) the part got. Bosch forbids both washing and ultrasonic cleaning for
   its pressure sensors: the vent opening exposes the MEMS element, and
   ultrasonic cleaning can destroy it. A washed board would fail on every copy
   (see check 1).

**Not an option: replacing U5 by hand** (operator, 2026-09-23). A 2 × 2 mm LGA
with hidden pads is not reworkable at the operator's bench. Diagnosis stops at
checks 1–5, and the rev 2.0 boards either get a working barometer from one of
those or run without one. Check 1 (a second board) matters all the more,
because it is the only way left to separate "this part" from "every part".

**What rev 2.1 might change, depending on the answers:**

- *Process* → a "do not wash, no ultrasonic" line against U5 in `FAB-NOTES.md`
  and the assembly notes, plus a moisture-bake requirement if MSL is the cause.
- *A power cycle revives it* → a way to hard-reset U5 without taking the Pi
  down: feed its VDD/VDDIO from a small load switch, or directly from a GPIO
  (it draws ≤ 0.7 mA peak). **No GPIO is free** once rev 2.1 closes the header
  (§13.3.9), so this has to share an existing enable or wait for the rev 3.0
  supervisor. Decide this after the investigation, not before.
- *A marginal joint* → review the LGA-10 footprint's paste aperture and stencil
  thickness against Bosch's handling and soldering guide.
- *One bad part* → no design change. Keep U5 as the BMP384, or move to the BMP390
  if measurement 9 (noise) argues for it anyway.

#### J1 — the BOM let the assembler fit the wrong header

PCBWay fitted a **standard-height** 2×20 header at J1. The stack needs the
**13.5 mm stacking header** to clear the Pi 5 active cooler. **Reworked on the
rev 2.0 board: the operator desoldered it and fitted a 13.5 mm extended-height
header** (2026-10-03), so the stack now sits at its intended height and is the
configuration §13.5.7's clearance figure was measured on. The board defect is
closed; the **BOM defect is not** — the rev 2.0 BOM line is ambiguous. Its value says `2x20 stacking hdr 13mm` and it is marked
operator-sourced / do-not-fit, but its notes still suggest Adafruit 2223, which
has an 8.5 mm body. It also has no manufacturer part number. **Fix in rev 2.1:**
give J1 an exact MPN for the 13.5 mm part, delete the conflicting candidates,
and choose one of two options, then say it the same way in `BOM.csv`,
`FAB-NOTES.md` and `pcbway-assembly-notes.txt`:

- have the assembler fit that MPN, or
- keep J1 do-not-fit, and put the reason on the order.

A do-not-fit line with no MPN is what the assembler filled with a generic part
this time. The first choice is the one that can't be misread.

#### U8 — the stale schematic note goes away with the part

Rev 2.0's U8 carries a `Note` claiming the ±4.096 V FSR is needed for *"the
2.92V full scale the divider plus D1 clamp can produce."* That reasoning was
wrong — 2.92 V is 16.2 V × 0.18, the Macchina M2's zener, which §13.2.3
deliberately did not copy — though the FSR choice it defended was right.

**Rev 2.1 deletes the note along with the ADS1015** (§13.5.3 item 5). The
ADS8688 has programmable per-channel ranges, so there is no single FSR to
justify; CH0's range is a per-channel setting in the driver, documented where
the channel map is. Nothing to carry over.

#### Y1 — the 40 MHz Pierce loop has no start-up margin on paper

> **Relocated.** This was §13.5.4's "one oscillator for both controllers"
> (operator decision 2026-09-14). CAN1 is gone, so the *share a clock between
> two controllers* argument is gone with it — but the rev 2.0 defect it also
> solved is still here, which is why the oscillator stays.

Rev 2.0's Y1 is the ABLS2 HC49/US can, 11.4 × 4.7 × 3.3 mm; §13.2.3 listed a
swap to a 3.2 × 2.5 mm crystal for rev 2.0 and it did not land. **Rev 2.1
replaces the crystal with a 40 MHz oscillator (X1) driving U3's OSC1.** Y1 and
its load caps C5/C6 are deleted.

```
+3V3 ── X1 (40 MHz, 3.3 V, CMOS out) ── R27 33 Ω ── U3 OSC1    (OSC2 open)
         │ C25 100n
```

- **Mode.** The MCP2518FD accepts an external clock on OSC1 with OSC2 left
  unconnected (datasheet: external clock input). `oscillator=40000000` in the
  overlay line is unchanged — the driver needs the frequency, not the source.
  No PLL.
- **Why it still earns its place with one controller.** The arithmetic below
  shows the 40 MHz Pierce loop is the one circuit on this board that is thin
  on paper, at the gm-min corner. An oscillator has no start-up margin, no
  load-cap derivation, no drive level to measure and no
  fundamental-vs-overtone trap. It replaces the hardest-to-verify circuit on
  the board with one that needs no verification — **that was always the larger
  half of the argument**, and dropping CAN1 only removes the smaller half.
- **Cost.** 2–4 mA on `+3V3` (a MEMS part) — the rail comes from the Pi header
  and dies with the Pi, so nothing is added to parked draw (§13.3.1).
  Accuracy ±25–50 ppm against the MCP2518FD's ±0.5 % — 100× margin.
- **R27 at the source.** R27 (22–33 Ω, at X1's output pin) damps the edge;
  OSC1 is a few pF. Route it as one short trace over solid ground, away from
  the analog front end (§13.5.3) and J4. With U10 gone this is a single short
  run rather than a trace with two stubs — simpler than the earlier plan.
- **Enable.** Tie X1's EN/ST pin to `+3V3` (or leave it per the datasheet's
  default-on), not to a GPIO. Rev 2.1 now has spare GPIOs (§13.5.4), but
  gating the CAN controller's clock behind one buys nothing and adds a way to
  kill `can0` in software.
- **MEMS preferred over quartz XO.** Rated shock/vibration, no ageing crack
  risk, and the same footprint. Either works electrically.
- **Footprint.** 2.5 × 2.0 mm or 3.2 × 2.5 mm 4-pad oscillator (stock KiCad
  `Oscillator:Oscillator_SMD_*`). Pin 1 is EN/ST, so a rotation error turns
  the part off — §12's pin-1 hazard applies here, unlike a crystal.
- **Not applicable now, kept because it would bite a future second
  controller:** branching U3's CLKO to clock another MCP2518FD does not work.
  The mainline `mcp251xfd` driver sets CLKO to SYSCLK ÷ 10 at init, and puts a
  chip into low-power mode whenever its interface is down, stopping the clock
  altogether. A driver fact, not a placement question.
**Candidate parts** — 40 MHz, 3.3 V, CMOS output, −40 … +85 °C. Headline
figures from memory, **each to be confirmed on its datasheet before the BOM**:

| Part | Type | Note |
|---|---|---|
| SiTime `SiT8008BI-…-33E-40.000000` (or `SiT8021` for lower current) | MEMS | Preferred; order code sets frequency, stability, temperature range |
| Abracon `ASDMB-40.000MHZ-LC-T` | MEMS | 2.5 × 2.0 mm |
| ECS `ECS-2520MV-400-BN-TR` | quartz XO | 2.5 × 2.0 mm, ±50 ppm |

**Fallback, recorded: a 3.2 × 2.5 mm 8 pF fundamental crystal.** If X1
fails at bring-up, the crystal design is this: NDK `NX3225GA-40.000M-STD-CRG-2`
or ECS `ECS-400-8-36-CKM-TR` (confirm fundamental at 40 MHz, CL, ESR max,
drive max, −40 … +85 °C), load caps 2·(CL − ~4 pF stray) = **8 pF C0G**, and
a 0 Ω 0603 in series with OSC2 to be sized after probing amplitude. Avoid
18 pF parts such as `ABM8G-40.000MHZ-18-D2Y-T` — they inherit the margin
problem below.

**The arithmetic that decided it — and what it says about rev 2.0.** A Pierce
oscillator starts only if its negative resistance beats the crystal's ESR,
usually by 3–5×:

```
|R_neg| ≈ gm / (ω² · C1 · C2)        C1, C2 = load caps + ~4 pF stray each
```

At 40 MHz (ω² ≈ 6.3 × 10¹⁶) with the MCP2518FD's datasheet gm of 2040–3060 µA/V:

| Crystal CL | Fitted caps | Effective C1 = C2 | \|R_neg\| (gm min … max) | vs ESR max |
|---|---|---|---|---|
| 18 pF (ABLS2, rev 2.0 as built) | 27 pF | ~31 pF | **34 … 50 Ω** | ABLS2 40 Ω → **0.8–1.3×** |
| 8 pF (fallback above) | 8 pF | ~12 pF | 224 … 336 Ω | 3225 ~60 Ω → ~4–5× |

§3.1.1 called the margin "comfortable" by looking at gm alone. On paper rev
2.0's Y1 has none at the gm-min corner. Rev 2.0 has not been powered, so this
is **§13.1 measurement 14** (does Y1 start, cold; probe OSC2 with ≤ 1 pF), not
a defect — and if the ABLS2 starts fine, the paper figure was conservative. With CAN1 dropped, rev 2.1
builds none of them: X1 removes the question rather than answering it.

### 13.5.7 Placement — and whether parts go on the bottom

**Rev 2.1 now adds about 8 parts net**, not the ~25 the CAN1 plan carried: an
HVSSOP regulator (U9), a 2.5 × 2.0 mm oscillator (X1) and a handful of
passives, against which it *deletes* Y1 + C5/C6 (the HC49's ~54 mm²) and six
divider resistors, and never adds U10, U11, FL2, the 1812 chokes, the split
termination or D7/R26. The one part that grew is U8: **ADS8688 in TSSOP-38
(9.7 × 4.4 mm) replacing an ADS1015 in VSSOP-10 (3 × 4.9 mm)**, about +30 mm²
in the same spot.

**So single-sided placement is now the expectation.** The earlier plan said
"components on B.Cu are likely"; with ~18 parts taken back out, B.Cu should be
unnecessary. The constraints are kept below because the ADS8688's footprint
could still force the question locally, and because §13.5.7's clearance figure
is worth recording either way:

- **Clearance is set by what sits under the HAT.** J1 is the only B.Cu part
  today; below it are the Pi 5 and its **active cooler**. Bottom-side height is
  the stacking-header height minus the cooler's height at that spot.
  **Measured on the stack: ~1/4" ≈ 6.3 mm above the fan** (operator,
  2026-10-03, eyeballed into the gap — not a caliper figure), **with the
  13.5 mm extended-height header fitted** (reworked onto the rev 2.0 board the
  same day, §13.5.6). That is the shipping configuration, so the figure
  carries straight into rev 2.1 placement, and it checks out against the
  geometry: 13.5 mm header over a ~7 mm cooler ≈ 6.5 mm.
  - **Height is not the binding constraint.** Budget **4 mm** usable — the
    measurement's own uncertainty plus header seating tolerance — and every
    SMT package rev 2.1 needs still clears it: 0603 ≤ 0.95 mm, SOT-23
    1.45 mm, VSSOP-10 / HVSSOP 1.1 mm, SOIC-8/14 1.75 mm, the 2.5 × 2.0 mm
    oscillator ~0.9 mm, 1812 chokes ~2–3 mm.
  - **The cooler is not flat, and the gap is not uniform.** 6.3 mm is the
    figure *above the fan*; the clearance over the heatsink fins and the
    cooler's tall region is less. Place against the cooler's actual profile,
    not one number.
  - **The fan inlet is an airflow keep-out, not a height one.** A 1 mm part
    0.5 mm above the inlet obstructs it as effectively as a tall one. Keep
    B.Cu copper and parts clear of the inlet footprint regardless of package
    height.
- **Only low-profile parts on B.Cu:** 0603 passives, SOT-23, SOIC. Keep tall
  and through-hole parts on top: C2 (electrolytic), J2/J3. X1 goes on top
  beside U3 so the clock trace stays short.
- **Keep the analog front end on top, next to U8.** With the dividers deleted
  this is now just C19–C22 and R19 — far less to place than the ~9 parts the
  earlier plan had, but the same rule: a filter cap belongs at the pin it
  filters, not across a 1.6 mm board from it. The good candidates for B.Cu
  are **digital-side passives**: decoupling for U8 and U9 (via-adjacent,
  directly under their pins) and pull-ups.
- **Keep-outs.** Through-hole pins of J1, J2, J3 and J4 protrude on B.Cu; no
  parts over them. Avoid the fan inlet region of the cooler.
- **Assembly cost.** Double-sided SMT means a second stencil and pass at the
  assembler. Hand-soldering a handful of bottom-side 0603s is the cheap
  alternative.
- **Planes stay solid.** Bottom-side parts must not break In1.Cu (§3.9: do not
  split it). Add GND stitching at every new IC, per §2's rev 2.0 note.
- **Try single-sided first**, with U8 packed near U3 so the shared SPI0 run
  stays short. Move to B.Cu only when the placement actually fails to close —
  which, at +8 parts net, it probably will not.

#### J4 (SMA) collides with the Pi 5's USB ports — two ways out

Found on the rev 2.0 stack (operator, 2026-10-02): **J4, the edge-mount SMA on
the right edge (x ≈ 160.5, y ≈ 68.3, launching outward), physically collides
with the Pi 5's USB ports** below it. Two options, not yet decided:

> **Re-check this on the reworked stack before choosing.** The collision was
> found on 2026-10-02, while J1 was still the **standard-height** header; the
> 13.5 mm part went on 2026-10-03 (§13.5.6) and raises the HAT by ~5 mm. Both
> options below are a re-layout, so a free fit check that might void the whole
> question is worth doing first. Don't assume it clears either — stacked
> USB-A is taller than the gain — but measure it rather than inheriting a
> pre-rework finding.

| Option | What changes | Cost |
|---|---|---|
| **A. Swap edges** (below) | U7 + J4 move to the bottom edge; some terminals move to the right edge | Re-layout of both edges; re-lands the harness; puts U7 next to the 12 V/K-line/CAN entry |
| **B. U.FL on the board, U.FL→SMA pigtail** | J4 becomes a U.FL receptacle in place; U7, J2, J3 and the harness stay where they are | A pigtail and a place to mount its SMA bulkhead; U.FL's retention in vibration |

**Option B — keep everything where it is.** Replace the edge-mount SMA with a
U.FL (IPEX MHF1-class) receptacle at the same launch, and run a short U.FL→SMA
bulkhead pigtail to the enclosure wall or a bracket. The SMA then sits wherever
it clears, not where the board edge happens to be.
- **What it keeps:** U7, BT1, the bias network, the K-line switch and both
  terminal blocks stay in place, and §13.5.5's harness pin map stands as
  written. This is the smaller change by far.
- **The RF launch gets simpler, not harder.** A U.FL pad is much smaller than
  the edge-mount SMA's footprint. Rework §3.6.1's launch (pads, anti-pad
  notch, solid-ground connection) for the U.FL footprint, keeping the 50 Ω
  trace to `U7-RF_IN` over solid In1.Cu.
- **Loss is small.** A ~10 cm 1.13 mm pigtail costs a few tenths of a dB at
  L1, which the active antenna's gain covers. The antenna bias passes
  through a pigtail unchanged.
- **Vibration is the real risk.** U.FL holds by friction and is rated for
  only about 30 mating cycles. In a vehicle, secure it: a retention clip or
  latching U.FL variant, or the pigtail tied down near the connector so cable
  strain never reaches the receptacle. Mate it once at assembly and leave it;
  connect and disconnect the antenna at the SMA bulkhead instead.
- **Needs a home for the bulkhead.** An enclosure hole or a small bracket off
  a standoff, clear of the USB ports.

**Option A — swap edges:**


- **GNSS moves to where the Phoenix terminals are now** (the bottom edge,
  y ≈ 93–96.5, J2 at x ≈ 109–139.5 and J3 at x ≈ 143–153 in rev 2.0). **U7
  (MAX-M10M) moves with J4**, so the 50 Ω feed from J4 to `U7-RF_IN` stays as
  short as it is today, along with the solid-ground SMA pads and the In1.Cu
  anti-pad notch at the launch (§3.6.1). BT1, the antenna bias and the
  TIMEPULSE/UART routing to GPIO4/5/18 follow U7.
- **Some terminals move to where the GNSS is now** (the right edge, which
  today also holds Q2/Q3, the K-line pull-up switch). Which positions move is
  not decided yet. Splitting the block across two edges also reopens
  §13.5.5's space problem: positions on the right edge free bottom-edge width,
  so the two-level MKKDS (option A) may no longer be the only fit.

**Constraints to check at placement:**
- **The bottom edge clears the Pi 5's USB-C side.** Checked on the rev 2.0
  stack (operator, 2026-10-02): there is plenty of space between the HAT and
  the USB-C ports, so the SMA is not at risk of the same collision there.
- **Keep the vehicle harness landing.** §13.5.5 holds rev 2.0's lower-level J2
  pin map fixed because the harness was already re-landed once. Moving
  positions to another edge re-lands the harness anyway, so do it once, in
  this revision, and update §13.5.5's pin map to match.
- **Keep the GNSS front end away from the noisy side.** Spec §8.4 (GNSS front
  end) already notes the M10 has no SAW/LNA and sits above the Pi. Moving U7
  next to the 12 V input, K-line and CAN terminals puts it nearer to
  ISO 7637 transients and CAN edges. Keep the RF path over unbroken ground,
  and leave room between U7 and those traces.
- **Terminals on the right edge need the same keep-outs.** Their through-hole
  pins protrude on B.Cu above the Pi's USB ports, and the wire entry must not
  face into the USB plugs.

### 13.5.8 Software implied (for later, not part of the board)

Listed so the hardware choices above are not made blind; none of it is
committed work.

- **U8 driver is new, not a modification.** The ADS1015 was I²C; the ADS8688
  is SPI with per-channel range registers, so this is a fresh driver rather
  than an extra address. It reads CH0–CH7, sets each channel's range once at
  init, and publishes MAP / IAT as ratios against CH3 plus fuel pressure from
  CH4. **It shares SPI0 with the vehicle CAN controller** (§13.5.3 item 5), so
  it must poll at the tens-of-Hz the signals need, keep each burst short, and
  never busy-read.
- **`V12_SENSE` moves ADCs.** The rev 2.0 12 V-sense path keeps its divider but
  changes the part reading it, so whatever reads it today is re-pointed and
  re-verified against `CANary-HAT-Revisions.md:119`'s measurements.
- **Fuel pressure is an analog channel now, not a CAN decoder.** The AEMnet
  work that landed 2026-09-21 — `hardware.sensor_can`,
  `data/sensor_can_signals.yaml`, `sensor:FUEL_PRESSURE` in kPa gauge — was
  built for a bus the gauge does not have (§13.5.4). **Keep the channel name
  and its units; re-source the value from CH4** so nothing downstream of
  `sensor:FUEL_PRESSURE` cares. The YAML layout stays as the record of the
  AEMnet decode in case a future device uses it.
- **The decode is shared with the rev 2.0 USB path, and that path comes
  first.** [`aem-fuel-pressure-analog-input.md`](aem-fuel-pressure-analog-input.md)
  specifies the same gauge into a USB ADC on today's hardware. Both paths use
  AEM's published `psig = 25.00·V − 12.50` and both owe the same **0.50–4.50 V
  fault gate**; only the counts-to-volts leg differs (a 10k/10k divider and the
  module's reference there, no divider and the ADS8688's reference here). Write
  the volts-to-psi conversion and the gate **once**, parameterised by the
  electrical leg — not twice, which is how the two boards would drift apart.
- **Nothing in `src/` reads an analog channel today.** The collector is new
  work for the USB path and the HAT inherits it, so the HAT is not what gates
  it.
- **Aux socketcan bus as a config role** — USB adapter only now (§13.5.4).
  `hardware.sensor_can` already binds by interface name from config, so no
  code changes; stable udev naming and a per-interface bring-up unit remain.
  Keep raw frames out of the vehicle-bus `can:` namespace — "which bus" must
  not be lost.
- **ADC transfer functions as pinned data**, not literals in code — `a`/`b` for
  MAP and the Steinhart–Hart coefficients for IAT live beside the other decode
  constants and are asserted by the ground-truth suite once measured.
- **Derived channels:** boost (MAP − baro) and injector ΔP (§13.5.1).
- **Hardware boundary for learned state.** Pre- and post-supercharger sessions
  share a VIN and profile. Training discovery must not mix them — enforced, not
  noted.

**Rev 3.0 consequences** are tracked in §13.3.9 of
[`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md), and dropping CAN1
changes them — that section needs updating to match: **CE1 is spent on the
ADC** rather than a second CAN controller (so it is still not free for the MCU
link, for a different reason), the ADC is now an **eight-channel** set,
**U11 no longer exists** so only U9 EN joins the MCU's safe-state nets, and
the header closes with **GPIO 12/13/19/20/21/27 uncommitted** instead of none
— which materially improves rev 3.0's options.

### 13.5.9 Open items before schematic entry

| # | Item | How it closes |
|---|---|---|
| 1 | `0 261 230 416` pinout, NTC curve, pressure range (`a`, `b`) | Meter + bench, before R19 and the calibration model are fixed |
| 2 | Sensor mating connector part number | Match against the sensor in hand |
| 3 | MKKDS 1,5/6-5,08 standard-colour item number, height, row offset; KiCad footprint | Phoenix datasheet |
| 4 | ~~Stack clearance under the HAT~~ — **closed: ~1/4" ≈ 6.3 mm above the fan**, measured with the 13.5 mm header fitted (operator, 2026-10-03). Height is not the binding constraint; every SMT package on the list clears it (§13.5.7) | **Closed** |
| 5 | TPS7A1650-Q1 capacitor requirements and EN threshold | TI datasheet |
| 6 | ~~AEMnet byte layout~~ — **off the critical path**, the gauge is analog (§13.5.4). Keep the note in case a future device uses AEMnet | Deferred indefinitely |
| 7 | Z1 rail port thread vs AEM sensor's NPT | Inspect rails; adapter if ORB |
| 8 | ~~GPIO19 / GPIO27 vs 20/21~~ — **closed**, CAN1 is dropped so no interrupt or STB GPIO is needed; the ADC uses CE1 natively. GPIO 12/13/19/20/21/27 all stay free | **Closed** |
| 9 | Board area closes single-sided? **Expected yes now** (+8 parts net, §13.5.7) | Placement attempt |
| 10 | X1 part: 40 MHz, 3.3 V, CMOS out, −40…+85 °C, EN pin polarity/default, current | Datasheet of the chosen part |
| 11 | Rev 2.0 Y1 starts cold? (§13.1 meas. 14) — informs whether the crystal fallback is ever viable | Bench, ≤ 1 pF probe on OSC2 |
| 12 | U5 root cause: part, process or design (§13.5.6) | Checks 1–5 in §13.5.6, starting with a second board (hand rework of U5 ruled out) |
| 15 | U5 pressure calibration against the U6 surrogate: fit `P = Bosch(raw, T_imu) + a + b·(T_imu − T0)` as pinned per-board data (deferred by operator 2026-09-23) | Cold-start warm-up log at the desk (constant pressure, phone as reference) separates a wrong temperature from a pressure offset; then the ECM baro PID 0x33 on a drive as the known answer |
| 13 | BT1 cell/lead part number with known polarity; `+` on silkscreen | Pick the lead, meter it against the swapped footprint |
| 14 | J1 13.5 mm stacking header MPN; assembler-fit or DNF decided. The rev 2.0 board is reworked to a 13.5 mm part (§13.5.6), so this is now a **BOM/notes defect only** — capture the MPN of the part actually fitted | Read the MPN off the part in hand, then make `BOM.csv`, `FAB-NOTES.md` and `pcbway-assembly-notes.txt` agree |
| 16 | J4 fix (§13.5.7): edge swap or U.FL pigtail. If swap: which terminals move, U7 spacing from the 12 V/K-line/CAN entry. If U.FL: receptacle + retention part number, pigtail length, bulkhead mount | Decide, then placement and a fit check on the rev 2.0 stack |
| 17 | **ADS8688 datasheet confirmation, before anything else in the analog front end**: 0–5.12 V range availability, the ±20 V input-protection spec **and whether it holds with AVDD dead**, internal reference, TSSOP-38 pinout, AVDD/DVDD rails, SPI mode and max clock, 500 kSPS | TI datasheet. The no-divider simplification (§13.5.3 items 2–4) collapses if the protection spec is conditional — resolve this first |
| 18 | ~~Is the AEM gauge output sensor-ratiometric or buffered?~~ — **closed from the install manual, 2026-10-03.** Buffered, with a published fixed transfer function (`psig = 25.00·V − 12.50`): one channel, no divider, no reference, 6 terminal positions. The proposed ~0.5 V discriminator would have answered this **wrong** (§13.5.4) | **Closed** |
| 25 | ~~Does the gauge have separate power and signal grounds?~~ — **closed: yes, and they are not tied internally.** BROWN (analog −) is separate from power ground, which is why BROWN must be bonded at the board (§13.5.3 item 7) | **Closed** |
| 26 | ~~`MKKDS 1,5/7-5,08` fit between MH3 and MH4~~ — **moot**, item 18 closed as buffered so 6 positions suffice (§13.5.5) | **Closed** |
| 28 | **Gauge-ground offset under load** — does the gauge's output stage drive correctly against our ground when that sits tens-to-hundreds of mV off its own power ground? 20 mV = 0.5 psi | Meter BROWN against the gauge's power ground, gauge powered, engine running (§13.5.3 item 7) |
| 29 | **The 0.50–4.50 V fault gate is a decoder requirement, not a note** — out-of-window must return a fault state, never a clamped number (§13.5.4) | Implement with the collector; a broken WHITE at 0 V must not decode as 0.0 psig |
| 27 | **DNF conditioning pads: which channels, and `R_series` populated at 0 Ω?** (item 8) | Falls out of item 17's protection answer |
| 19 | ~~Which AEM wire is signal and which is ground~~ — **closed, and the question was mis-framed.** WHITE is analog **+**, BROWN analog **−**; neither is a ground (§13.5.3 item 7). Meter once as a build check | **Closed** |
| 20 | **MAP sensor pull-down resistor** — deleting the divider removed the ≥ 10 kΩ Bosch requires (§13.5.3 item 2); lands on a DNF pad per item 8 | Value from the Bosch datasheet with item 1's pinout |
| 21 | ~~Fuel-pressure transfer function~~ — **closed: AEM publishes it**, `psig = 25.00·V − 12.50`, valid 0.50–4.50 V. Only the *electrical* chain needs calibrating, and on rev 2.1 that is just the ADS8688's reference (no divider, no resistor tolerance). Gauge face remains the independent cross-check | **Closed**; pin in `catalog_pins` |
| 22 | **SPI0 contention: does ADC polling cost CAN0 frames?** New risk, created by putting the ADC on CE1 | Bring-up measurement — CAN0 frame loss and latency, ADC idle vs polling (§13.5.3 item 5) |
| 23 | **Re-verify `V12_SENSE` on the new ADC** — same divider, different part | Repeat `CANary-HAT-Revisions.md:119`: 0.000 V no vehicle, ~12.68 V key-off, ~12.43 V KOEO |
| 24 | **Update §13.3.9 of `CANary-HAT-Revisions.md`** for the dropped CAN1 — rev 3.0's GPIO and safe-state assumptions changed (§13.5.8) | Edit that section |
