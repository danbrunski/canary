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
   port at the back of the Z1 fuel rails. It outputs **AEMnet**, a private CAN
   bus with the gauge as its only other node.

Rev 2.0 has one CAN channel (vehicle bus) and one spare, unrouted ADC input.
Rev 2.1 adds **three analog inputs with a protected sensor supply**, a **second
CAN channel**, and the **terminal positions** for both.

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
| 2 | MAP input on U8 AIN1 | R17/R18 divider, C19 |
| 3 | IAT (NTC) input on U8 AIN2 | R19 pull-up, R20/R21 divider, C20 |
| 4 | Sensor-supply sense on U8 AIN3 (ratiometric reference) | R22/R23 divider, C21 |
| 5 | Un-ground AIN2/AIN3 | — (net change) |
| 6 | Second CAN channel (CAN1) on SPI0 CE1 | U10 MCP2518FD, U11 TCAN1042, FL2, decoupling (clock from X1, row 11) |
| 7 | **Jumper-selectable 120 Ω termination on CAN1 only** (split: 2 × 60.4 Ω + 4.7 nF); CAN0 gets no termination footprint | R24/R25 60.4 Ω, C24 4.7 nF, JP2 |
| 8 | CAN1 activity LED | D7 + R26 |
| 9 | `J2` → two-level terminal block, 12 positions | J2 MKKDS 1,5/6-5,08 |
| 10 | Correct U8's stale schematic note | — (text) |
| 11 | **One 40 MHz oscillator clocks both controllers**; Y1 + C5/C6 deleted, no Y2 | X1 MEMS oscillator, C25 100n, R27 33 Ω |
| 12 | **BT1 polarity reversed** — swap BT1's GND and D2-anode pins (§13.5.6) | BT1 (net change) |
| 13 | **U5 barometer: fix whatever the rev 2.0 investigation finds** (§13.5.6) | U5 — TBD |
| 14 | **J1 BOM line names the 13.5 mm stacking header by part number** (§13.5.6) | J1 (BOM only) |
| 15 | **Swap edges: GNSS (U7 + J4 SMA) moves to the terminal-block edge, some terminals move to the right edge** — rev 2.0's edge-mount SMA collides with the Pi 5's USB ports (§13.5.7) | U7, J4, J2/J3 (placement) |

Roughly **+25 components** on a 65 × 56 mm board that already carries 57 — see
§13.5.7 for where they go.

### 13.5.3 MAP/IAT sensor interface

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
  A chafed 5V wire on the Pi's `+5V` would brown out the Pi and the TCAN1042s;
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
- **Cranking** drops U9 out of regulation briefly. That is why AIN3 measures
  the rail instead of assuming 5.00 V.
- Input/output capacitors per the TPS7A16 datasheet (≥ 50 V rating on the input
  side, by the same rule).

#### 2. MAP → AIN1

```
J2-U2 (MAP) ── R17 10k ──┬── U8 AIN1
                         ├── R18 15k ── GND
                         └── C19 100n ── GND
```

- **Ratio 0.60.** 4.65 V span top → 2.79 V; 4.85 V diagnostic clamp → 2.91 V;
  a 5.5 V supply fault → 3.30 V. All inside the **±4.096 V FSR** already
  required for AIN0 (U8's PGA is set per conversion, so one FSR serves all four
  channels). **The PGA range does not raise the pin limit** — an input must
  stay below VDD + 0.3 V whatever FSR is selected, which is why the sensor's
  0–5 V output cannot go to a 3.3 V-powered ADS1015 undivided.
- **Fault cases.** Harness shorted to 12 V: the node would reach 7.2 V; U8's
  input protection holds it near 3.6 V and R17 limits current to
  (12 − 3.6)/10k = **0.84 mA**. At D1's 38.9 V clamp: **3.5 mA**. Both inside
  TI's ±10 mA continuous input rating.
- **Loading.** Thevenin 6.0 kΩ against the ADS1015's ~6 MΩ input: ~0.1 % gain
  error, and it cancels in the ratio against AIN3 anyway. The 25 kΩ to GND
  satisfies Bosch's ≥ 10 kΩ pull-down.
- **Filter — sized to the sample rate, not the signal.** 6.0 kΩ × 100 nF →
  ~265 Hz. The ADS1015's per-channel rate here is at most a few hundred SPS
  (§13.5.3 item 5), so the anti-alias corner has to sit there, not at kHz.
  Boost changes on a 100 ms scale; nothing useful is lost.
- **Tolerance.** 1 % parts are enough *provided* the MAP and AIN3 dividers use
  the same two values from the same reel — what matters for the ratio is
  matching, not absolute accuracy, and the key-on baro check calibrates the
  residual. 0.1 % parts (`R17/R18/R22/R23` as a matched set) cost little and
  remove that dependence; either way the calibration in item 6 is still done.
- **Resolution.** 2.0 mV LSB at the ADC = 3.3 mV at the sensor. Across a
  typical 4.25 V sensor span that is ~1275 counts — for a 3.5-bar sensor about
  0.27 kPa per count, for a 2.5-bar sensor about 0.19 kPa. Adequate for boost
  logging in either case.

#### 3. IAT → AIN2

```
U9 5V_SENS ── R19 ──┬── J2-U3 (IAT) ── NTC (in sensor) ── J2-U6 (SENS_GND)
                    │
                    └── R20 10k ──┬── U8 AIN2
                                  ├── R21 15k ── GND
                                  └── C20 100n ── GND
```

- **R19 pull-up — proposed 4.99 kΩ, final value after measuring the NTC.** At
  4.99 kΩ the worst-case NTC current (NTC → 0 Ω) is 1.0 mA, which respects the
  family's ~1 mA measuring-current limit. At 20 °C (2.5 kΩ) the node sits near
  1.6 V, mid-range. Bosch's own example uses 2.61 kΩ, which buys hot-end
  resolution at up to ~1.9 mA of self-heating current. Post-intercooler air
  should rarely exceed ~80 °C, so favour low self-heating.
- **Pulled up to `5V_SENS`, not `+3V3`.** The IAT reading is then ratiometric
  against the same rail AIN3 measures.
- **The divider loads the node** (25 kΩ in parallel with the NTC). That is a
  fixed, known network — software solves it with the real resistor values;
  there is no need to buffer.
- **Open sensor is detectable.** With the NTC unplugged, the node is
  5 V × 25/(25 + 4.99) ≈ 4.17 V → **2.50 V at AIN2**, above any in-range
  reading.
- **Curve.** The catalogue gives one point (2.5 kΩ @ 20 °C), not a table.
  Measure the NTC at three known temperatures (ice water, room, hot water with
  a reference thermometer) and fit Steinhart–Hart; that beats hunting for a
  table that may belong to a different Bosch NTC.

#### 4. Sensor-supply sense → AIN3

Same 10k/15k/100n network (R22/R23/C21) on `5V_SENS`. MAP and IAT are computed
as **ratios against AIN3**, so U9's tolerance, cranking dropout and temperature
drift cancel, and so does the divider ratio itself:

```
ratio    = code_AIN1 / code_AIN3            # dimensionless, dividers cancel
MAP_kPa  = a · ratio + b                     # a, b: THIS sensor's transfer function
boost    = MAP_kPa − baro_kPa (U5)
```

**`a` and `b` are not known yet and must not be guessed.** For a 0.5–4.5 V,
50–400 kPa (4-bar) sensor they would be 437.5 and 6.25; for a 2.5-bar part they
are different numbers, and the `0 261 230 416`'s range is unconfirmed. Any
placeholder that lands in code becomes a confident wrong reading — exactly the
failure `AGENTS.md` records six retractions for. Fix them from the key-on baro
point and the leak-tester point (item 6), or from a datasheet for this exact
part number, and pin them in `catalog_pins` like every other decode constant.

Sensed at the board, so harness drop (≤ 12.5 mA through a few metres of 22 AWG)
is not included — small, and a calibration term if it ever matters.

#### 5. U8 channel map and rate

| Input | Rev 2.0 | Rev 2.1 |
|---|---|---|
| AIN0 | `V12_SENSE` | unchanged |
| AIN1 | unconnected | **MAP** |
| AIN2 | **GND** | **IAT** |
| AIN3 | **GND** | **5V_SENS** |

The ADS1015 has one converter and a multiplexer: **3300 SPS total**, shared.
Round-robin across four channels is ≤ ~825 SPS each before I2C overhead;
daemon polling will set the real rate. That is plenty for boost and IAT; it is
not per-cylinder pulsation. **Alternative, same footprint:** `ADS1115IDGSR` is
pin-compatible 16-bit at 860 SPS total — more resolution, a quarter of the
rate. Not recommended unless 3.4 mV MAP steps prove too coarse.

**AIN0 stays `V12_SENSE`.** It is routed and populated on rev 2.0; the three
new signals take the three inputs that were free, so the rev 2.0 12 V-sense
software path is untouched.

**BOM commonality.** 10 kΩ is R14's part (+3), 100 nF is the C1 group part (+3).
New values: 15 kΩ (×3), 4.99 kΩ (×1).

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

### 13.5.4 Second CAN channel (CAN1) — AEM fuel pressure gauge

#### The AEMnet message (AEM `10-0301` manual, p. 8)

| Field | Value |
|---|---|
| Bit rate | **500 kbps**, classic CAN (no FD) |
| Identifier | **29-bit, `0x000A0301`** |
| DLC / rate | 8 / **20 Hz** |
| Signal | Pressure (gauge), **16-bit unsigned, big-endian, 0.01 bar/bit**, offset 0 |
| Termination | **None in the gauge** |
| Wires | CANH white/black, CANL green/black |

**The manual's table is internally inconsistent:** it places a 16-bit value in
"byte 0" and marks bytes 1–7 unused. It is almost certainly bytes 0–1. **Confirm
with `candump` against the gauge face on the bench before writing a decoder.**

#### Termination rule (operator decision 2026-09-14)

- **CAN1 (aux, Linux `can1`): jumper-selectable 120 Ω termination on board.**
  JP2 closed = terminated (the default, for the two-node AEMnet bus); JP2 open
  = unterminated, for joining a bus that already has both terminators.
- **CAN0 (vehicle, Linux `can0`): no termination and no jumper, by design.**
  The vehicle bus is already terminated at both ends; a footprint here would
  only ever be a fault waiting for someone to populate it.

Board channels are numbered from 0 so one index runs through the whole stack:
**CAN0 = vehicle = SPI0 CE0 = `spi0.0` = `can0`**, **CAN1 = aux = SPI0 CE1 =
`spi0.1` = `can1`**. The Linux names only hold once udev pins them by SPI
path (below); until then they follow probe order, and the termination
decision is per *board channel*, not per interface name.

#### Circuit — a copy of CAN0, with one deliberate difference

| Block | CAN0 (rev 2.0) | CAN1 (rev 2.1) |
|---|---|---|
| Controller | U3 MCP2518FDT-E/SL, SPI0 **CE0** (GPIO8) | **U10** MCP2518FDT-E/SL, SPI0 **CE1** (GPIO7, pin 26) |
| Interrupt | GPIO25 | **GPIO27** (pin 13) |
| Clock | ~~Y1 ABLS2-40.000MHZ-D4YF-T, 27 pF C0G~~ → **X1 40 MHz oscillator on OSC1, OSC2 open** (see *Clock* below) | **same X1**, second stub to U10 OSC1 — no crystal |
| Transceiver | U4 TCAN1042VDRQ1, VDD `+5V`, VIO `+3V3` | **U11**, identical |
| STB | GPIO22 | **GPIO19** (pin 35) |
| CM choke | FL1 1812CAN-513NRC | **FL2**, identical |
| Activity LED | D6 + R16 off U4 RXD | **D7 + R26** off U11 RXD |
| Termination | **none — vehicle bus is already terminated** | **split 2 × 60.4 Ω + 4.7 nF, JP2 default closed** |

- **CE1 was reserved for exactly this.** §4.1: *"GPIO7 (CE1) and GPIO12/13
  (`uart4`) were deliberately not used, to keep a second CAN controller and a
  spare UART available."* This spends CE1 and keeps `uart4` spare.
- **GPIO picks.** After rev 2.0 claimed GPIO22/23/24, the free header GPIOs are
  7, 12, 13, 19, 20, 21, 27. GPIO27 and GPIO19 are proposals; placement may
  prefer 20/21, and 12/13 stay reserved for `uart4`.
- **STB on a GPIO, for the same reason as U4** (§13.2.1): the pin's internal
  pull-up puts the transceiver in standby when the GPIO is high-Z, so the
  daemon **must drive GPIO19 low** or CAN1 is silently dead.
- **Why CAN1 terminates on board when CAN0 refuses to.** Two 60.4 Ω in
  series present 120.8 Ω across the pair — this *is* the 120 Ω terminator, with
  the centre tap to ground for common-mode rejection. §3.2's argument is
  that a vehicle bus already has two terminators and a third is a fault. AEMnet
  here is a private two-node bus with **the HAT always at one end** and a gauge
  that has no termination of its own. Split termination (two 60.4 Ω with 4.7 nF
  to GND at the centre) is the standard arrangement. **JP2** lets CAN1 join an
  already-terminated bus later without desoldering. The gauge end still needs
  **120 Ω in the harness**.
- **`+5V` budget.** A second TCAN1042 adds up to ~70 mA dominant on the Pi's
  5 V. Negligible against the X1202.
- **One clock source for both chips** — an oscillator, not U3's CLKO: next
  section.

#### Clock — one oscillator for both controllers (operator decision 2026-09-14)

Rev 2.0's Y1 is the ABLS2 HC49/US can, 11.4 × 4.7 × 3.3 mm; §13.2.3 listed a
swap to a 3.2 × 2.5 mm crystal for rev 2.0 and it did not land. Rev 2.1 needs
a clock for a second controller and is short of area. **A crystal cannot be
shared** — it is a passive resonator that works inside exactly one amplifier
loop — so the choice was two small crystals or one oscillator. **Rev 2.1 uses
one 40 MHz oscillator (X1) driving OSC1 of both U3 and U10.** Y1 and its load
caps C5/C6 are deleted; Y2 is never added.

```
+3V3 ── X1 (40 MHz, 3.3 V, CMOS out) ── R27 33 Ω ──┬── U3 OSC1   (OSC2 open)
         │ C25 100n                                └── U10 OSC1  (OSC2 open)
```

- **Mode.** The MCP2518FD accepts an external clock on OSC1 with OSC2 left
  unconnected (datasheet: external clock input). `oscillator=40000000` in both
  overlay lines is unchanged — the driver needs the frequency, not the source.
  No PLL.
- **Why an oscillator beats two crystals here.** The start-up-margin
  arithmetic below (kept because it bears on rev 2.0) shows a 40 MHz Pierce
  loop is the one circuit on this board that is thin on paper. An oscillator
  has no start-up margin, no load-cap derivation, no drive level to measure
  and no fundamental-vs-overtone trap: 3 parts replace 8, and the two
  hardest-to-verify circuits on the board become none.
- **Cost.** 2–4 mA on `+3V3` (a MEMS part) — the rail comes from the Pi header
  and dies with the Pi, so nothing is added to parked draw (§13.3.1).
  Accuracy ±25–50 ppm against the MCP2518FD's ±0.5 % — 100× margin.
- **R27 at the source, U3 and U10 side by side.** Both controllers sit on SPI0
  and belong next to each other anyway; the clock run is then a couple of cm.
  R27 (22–33 Ω, at X1's output pin) damps the edge; each OSC1 is a few pF, so
  one output driving two inputs is not a fan-out question. Route it as one
  short trace with a stub to each OSC1, over solid ground, away from the
  analog front-end (§13.5.3) and J4.
- **Enable.** Tie X1's EN/ST pin to `+3V3` (or leave it per the datasheet's
  default-on), not to a GPIO — the header has none spare (§13.3.9).
- **MEMS preferred over quartz XO.** Rated shock/vibration, no ageing crack
  risk, and the same footprint. Either works electrically.
- **Footprint.** 2.5 × 2.0 mm or 3.2 × 2.5 mm 4-pad oscillator (stock KiCad
  `Oscillator:Oscillator_SMD_*`). Pin 1 is EN/ST, so a rotation error turns
  the part off — §12's pin-1 hazard applies here, unlike a crystal.

**Candidate parts** — 40 MHz, 3.3 V, CMOS output, −40 … +85 °C. Headline
figures from memory, **each to be confirmed on its datasheet before the BOM**:

| Part | Type | Note |
|---|---|---|
| SiTime `SiT8008BI-…-33E-40.000000` (or `SiT8021` for lower current) | MEMS | Preferred; order code sets frequency, stability, temperature range |
| Abracon `ASDMB-40.000MHZ-LC-T` | MEMS | 2.5 × 2.0 mm |
| ECS `ECS-2520MV-400-BN-TR` | quartz XO | 2.5 × 2.0 mm, ±50 ppm |

**Rejected: branching U3's CLKO to U10.** The mainline `mcp251xfd` driver sets
CLKO to SYSCLK ÷ 10 at init — U10 would see 4 MHz — and it puts a chip into
low-power mode whenever its interface is down, which stops the clock
altogether. U10 could never probe while `can0` was down. Not a placement
question; a driver fact.

**Fallback, recorded: two 3.2 × 2.5 mm 8 pF fundamental crystals.** If X1
fails at bring-up, the crystal design is this: NDK `NX3225GA-40.000M-STD-CRG-2`
or ECS `ECS-400-8-36-CKM-TR` (confirm fundamental at 40 MHz, CL, ESR max,
drive max, −40 … +85 °C), load caps 2·(CL − ~4 pF stray) = **8 pF C0G**, and
a 0 Ω 0603 in series with each OSC2 to be sized after probing amplitude. Avoid
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
a defect — and if the ABLS2 starts fine, the paper figure was conservative. It
is also the reason rev 2.1 does not build two more of them.

#### Gauge wiring

- **Gauge power does not come from the HAT.** `+12V` here is OBD pin 16 —
  permanent battery. The gauge wants **switched, fused 12 V** (AEM: 5 A inline
  fuse on pin 1). Powering it from the HAT would leave it lit with the truck
  parked.
- **Ground reference.** Gauge power ground to chassis. The HAT is bonded to
  chassis through J2 pin 3 (§3.9), so both CAN1 nodes share a reference; the
  TCAN1042's ±12 V common-mode range covers the offset. No separate CAN1 ground
  terminal.

#### Linux

```
dtoverlay=mcp251xfd,spi0-0,interrupt=25,oscillator=40000000
dtoverlay=mcp251xfd,spi0-1,interrupt=27,oscillator=40000000
```

Both interfaces use the same driver, so `can0`/`can1` naming follows probe
order. Pin the names with udev by **SPI device path** (`spi0.0` vs `spi0.1`),
not by driver. The current `systemd/70-canable-can0.rules` and `can0.service`
assume a single interface and must change.

#### The aux bus is a role, not a chip

**Requirement:** the AEMnet bus must work on **either** the HAT's CAN1 **or a
USB CAN FD adapter** (CANable 2, candleLight / `gs_usb`,
`docs/canable-firmware.md`), whichever is present — on rev 2.0 today, as a
spare if CAN1 fails at bring-up, or on a Pi with no HAT at all. Both present
the same thing to Linux: a socketcan netdev. So:

- **Software binds the aux bus to an interface name from config**, exactly as
  `hardware.can` does for the vehicle bus today — no "HAT" or "USB" branch in
  the collector. The transport is a config value.
- **udev pins names by hardware path**, so the name never depends on probe
  order: `spi0.1` → e.g. `canaux`, the gs_usb by USB serial → the *same*
  role name when it is the one fitted, or a distinct name with the config
  pointing at it. Either works; what must not happen is `can1` meaning
  different hardware on different boots.
- **The link-bring-up unit is per-interface**, replacing the single-interface
  assumptions in `systemd/can0.service` and `70-canable-can0.rules`.

Two constraints differ between the transports and belong in config/docs, not
in code that editorialises about hardware:

- **`restart-ms`:** the `mcp251xfd` driver accepts it; `gs_usb` on this
  firmware does not, so bus-off does not self-recover on the USB path
  (`docs/can-setup.md`).
- **Both must run active, not listen-only.** With the gauge as the only other
  node, something must ACK its frames or it retransmits until error-passive.
  This is true of both transports; it is called out because the vehicle bus is
  often sniffed listen-only and the habit must not carry over.

Neither transport needs CAN FD for AEMnet (classic 2.0B, 500 kbps); both
support it, so the aux bus is not FD-limited if a future device wants it.

#### No third channel on the board (operator decision 2026-09-14)

Some vehicles carry a second bus on the OBD connector (a 125 kbps medium-speed
bus on pins 3/11; GM's single-wire CAN on pin 1 needs a different transceiver
altogether). Rev 2.1 stays at **two** on-board channels. A third MCP2518FD
*would* fit the Pi — SPI0 shared, GPIO20 as a `cs-gpios` chip select, GPIO21
for nINT, STB shared with CAN1 — but it spends the last two uncommitted GPIOs,
needs two more terminal positions J2 does not have (§13.5.5), and adds a dozen
parts to a board that may already go double-sided (§13.5.7).

**Additional buses are USB CAN adapters** (CANable / candleLight `gs_usb`),
bound by the same config-role mechanism as the aux bus above: one socketcan
netdev per bus, named by udev from the USB serial, no header cost. The
principled home for three or more on-board channels is the rev 3.0 MCU, whose
class carries two or three CAN FD controllers natively behind one link to the
Pi.

### 13.5.5 Terminal block — `J2` goes two-level

#### The space problem

Rev 2.1 needs **6 new positions**: `5V_SENS`, `MAP`, `IAT`, `SENS_GND`,
`CAN1H`, `CAN1L`. From the rev 2.0 PCB:

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
| 4 | `CANH_RAW` (OBD 6) | `CAN1H` — AEMnet+ (white/black) |
| 5 | `CANL_RAW` (OBD 14) | `CAN1L` — AEMnet− (green/black) |
| 6 | `KLINE` (OBD 7) | `SENS_GND` — sensor return |

Why this arrangement:
- **Column 1 = supply** (12 V below, 5 V above) and **columns 4–5 = CAN** (vehicle
  pair below, aux pair above).
- **The analog signals sit over ground** (columns 2–3), away from both the
  K-line and the CAN pairs.
- **`SENS_GND` sits over `KLINE`.** The K-line carries ISO 7637-2 pulses to
  +100 V / −150 V (§3.9); putting a ground above it rather than a
  high-impedance analog input is the conservative choice.
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
**13.5 mm stacking header** to clear the Pi 5 active cooler. The rev 2.0 BOM
line is ambiguous. Its value says `2x20 stacking hdr 13mm` and it is marked
operator-sourced / do-not-fit, but its notes still suggest Adafruit 2223, which
has an 8.5 mm body. It also has no manufacturer part number. **Fix in rev 2.1:**
give J1 an exact MPN for the 13.5 mm part, delete the conflicting candidates,
and choose one of two options, then say it the same way in `BOM.csv`,
`FAB-NOTES.md` and `pcbway-assembly-notes.txt`:

- have the assembler fit that MPN, or
- keep J1 do-not-fit, and put the reason on the order.

A do-not-fit line with no MPN is what the assembler filled with a generic part
this time. The first choice is the one that can't be misread.

#### U8 — stale schematic note

**U8's schematic `Note` is stale.** It says the ±4.096 V FSR is needed for
*"the 2.92V full scale the divider plus D1 clamp can produce."* 2.92 V is
16.2 V × 0.18 — the Macchina M2's zener, which §13.2.3 deliberately did not
copy. D1 here clamps at 38.9 V, which would try to put ~7.0 V on the node,
held near 3.6 V by U8's input protection. The FSR choice is right; the note's
reason is wrong. Text-only, no electrical change. Update it when the other
three channels are added.

### 13.5.7 Placement — and whether parts go on the bottom

The board is already dense, and rev 2.1 adds about 25 parts net: two
SOIC-14-class ICs, two SOIC-8s, an HVSSOP, two 1812 chokes, a 2.5 × 2.0 mm
oscillator and passives — minus Y1 and C5/C6, which gives back the HC49's
~54 mm².
**Components on B.Cu are likely.** The constraints:

- **Clearance is set by what sits under the HAT.** J1 is the only B.Cu part
  today; below it are the Pi 5 and its **active cooler**. Bottom-side height is
  the stacking-header height minus the cooler's height at that spot. **Measure
  it on the rev 2.0 stack** before placing anything — add it to the §13.1
  list.
- **Only low-profile parts on B.Cu:** 0603 passives, SOT-23, SOIC. Keep tall
  and through-hole parts on top: C2 (electrolytic), J2/J3, the chokes. X1
  goes on top between U3 and U10 so the clock trace stays short.
- **Keep the analog front-end on top, next to U8.** R17–R23 and C19–C21 are
  short high-impedance nodes; putting them on the far side of a 1.6 mm board
  from U8 adds vias and loop area for nothing. The good candidates for B.Cu
  are **digital-side passives**: decoupling for the new ICs (via-adjacent,
  directly under their pins), pull-ups, LED resistors, the CAN1 split
  termination.
- **Keep-outs.** Through-hole pins of J1, J2, J3 and J4 protrude on B.Cu; no
  parts over them. Avoid the fan inlet region of the cooler.
- **Assembly cost.** Double-sided SMT means a second stencil and pass at the
  assembler. Hand-soldering a handful of bottom-side 0603s is the cheap
  alternative.
- **Planes stay solid.** Bottom-side parts must not break In1.Cu (§3.9: do not
  split it). Add GND stitching at every new IC, per §2's rev 2.0 note.
- **Try single-sided first**, with CAN1 packed near U3 so SPI0 stays short.
  Move to B.Cu only when the placement actually fails to close.

#### J4 (SMA) collides with the Pi 5's USB ports — swap the GNSS and terminal edges

Found on the rev 2.0 stack (operator, 2026-10-02): **J4, the edge-mount SMA on
the right edge (x ≈ 160.5, y ≈ 68.3, launching outward), physically collides
with the Pi 5's USB ports** below it. Rev 2.1 fixes it by swapping edges:

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

- **U8 driver:** read AIN0–AIN3, publish MAP / IAT as ratios against AIN3,
  apply calibration.
- **Aux socketcan bus as a config role** (HAT CAN1 *or* USB adapter, §13.5.4),
  stable interface naming, and an AEMnet decoder producing engineering-unit
  channels. *Decoder and config role landed 2026-09-21:* `hardware.sensor_can`
  (interface from config), `data/sensor_can_signals.yaml` (AEM layout,
  `verified: false` until open item 6 closes), `sensor:FUEL_PRESSURE` in kPa
  gauge. Stable udev naming and a per-interface bring-up unit remain. Keep raw frames out of the vehicle-bus `can:` namespace — "which
  bus" must not be lost.
- **ADC transfer functions as pinned data**, not literals in code — `a`/`b` for
  MAP and the Steinhart–Hart coefficients for IAT live beside the other decode
  constants and are asserted by the ground-truth suite once measured.
- **Derived channels:** boost (MAP − baro) and injector ΔP (§13.5.1).
- **Hardware boundary for learned state.** Pre- and post-supercharger sessions
  share a VIN and profile. Training discovery must not mix them — enforced, not
  noted.

**Rev 3.0 consequences** are tracked in §13.3.9 of
[`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md): CE1 is no longer free for
the MCU link, the ADC becomes a four-channel ratiometric set, U11 STB and U9 EN
join the MCU's safe-state nets, and the header closes with no spare GPIO.

### 13.5.9 Open items before schematic entry

| # | Item | How it closes |
|---|---|---|
| 1 | `0 261 230 416` pinout, NTC curve, pressure range (`a`, `b`) | Meter + bench, before R19 and the calibration model are fixed |
| 2 | Sensor mating connector part number | Match against the sensor in hand |
| 3 | MKKDS 1,5/6-5,08 standard-colour item number, height, row offset; KiCad footprint | Phoenix datasheet |
| 4 | Stack clearance under the HAT | Measure on the rev 2.0 stack |
| 5 | TPS7A1650-Q1 capacitor requirements and EN threshold | TI datasheet |
| 6 | AEMnet byte layout (0–1 big-endian?) | `candump` against the gauge on the bench |
| 7 | Z1 rail port thread vs AEM sensor's NPT | Inspect rails; adapter if ORB |
| 8 | GPIO19 / GPIO27 vs 20/21 | Placement |
| 9 | Board area closes single-sided? | Placement attempt (§13.5.7) |
| 10 | X1 part: 40 MHz, 3.3 V, CMOS out, −40…+85 °C, EN pin polarity/default, current | Datasheet of the chosen part |
| 11 | Rev 2.0 Y1 starts cold? (§13.1 meas. 14) — informs whether the crystal fallback is ever viable | Bench, ≤ 1 pF probe on OSC2 |
| 12 | U5 root cause: part, process or design (§13.5.6) | Checks 1–5 in §13.5.6, starting with a second board (hand rework of U5 ruled out) |
| 15 | U5 pressure calibration against the U6 surrogate: fit `P = Bosch(raw, T_imu) + a + b·(T_imu − T0)` as pinned per-board data (deferred by operator 2026-09-23) | Cold-start warm-up log at the desk (constant pressure, phone as reference) separates a wrong temperature from a pressure offset; then the ECM baro PID 0x33 on a drive as the known answer |
| 13 | BT1 cell/lead part number with known polarity; `+` on silkscreen | Pick the lead, meter it against the swapped footprint |
| 14 | J1 13.5 mm stacking header MPN; assembler-fit or DNF decided | Measure on the rev 2.0 stack (item 4), order to MPN |
| 16 | Edge swap (§13.5.7): which terminal positions move to the right edge; U7 spacing from the 12 V/K-line/CAN entry | Placement, then a fit check on the rev 2.0 stack |
