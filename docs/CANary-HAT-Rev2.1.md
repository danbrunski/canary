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
| 6 | Second CAN channel (CAN1) on SPI0 CE1 | U10 MCP2518FD, Y2, C22/C23, U11 TCAN1042, FL2, decoupling |
| 7 | **Jumper-selectable 120 Ω termination on CAN1 only** (split: 2 × 60.4 Ω + 4.7 nF); CAN0 gets no termination footprint | R24/R25 60.4 Ω, C24 4.7 nF, JP2 |
| 8 | CAN1 activity LED | D7 + R26 |
| 9 | `J2` → two-level terminal block, 12 positions | J2 MKKDS 1,5/6-5,08 |
| 10 | Correct U8's stale schematic note | — (text) |

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
| Crystal | Y1 ABLS2-40.000MHZ-D4YF-T, 27 pF C0G | **Y2**, same part and caps — §3.1.1's derivation applies verbatim |
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
- **Crystal alternative.** Sharing U3's CLKO into U10's OSC1 would delete Y2,
  but it makes CAN1 depend on U3's clock-out configuration, which the
  `mcp251xfd` driver does not manage for another chip. Rejected. If area forces
  it, a 3.2 × 2.5 mm SMD fundamental crystal needs its own CL/load-cap
  derivation per §3.1.1 — do not reuse 27 pF blindly.

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

### 13.5.6 Carry-over fix

**U8's schematic `Note` is stale.** It says the ±4.096 V FSR is needed for
*"the 2.92V full scale the divider plus D1 clamp can produce."* 2.92 V is
16.2 V × 0.18 — the Macchina M2's zener, which §13.2.3 deliberately did not
copy. D1 here clamps at 38.9 V, which would try to put ~7.0 V on the node,
held near 3.6 V by U8's input protection. The FSR choice is right; the note's
reason is wrong. Text-only, no electrical change. Update it when the other
three channels are added.

### 13.5.7 Placement — and whether parts go on the bottom

The board is already dense, and rev 2.1 adds about 25 parts: two SOIC-14-class
ICs with an HC49 crystal, two SOIC-8s, an HVSSOP, two 1812 chokes, and passives.
**Components on B.Cu are likely.** The constraints:

- **Clearance is set by what sits under the HAT.** J1 is the only B.Cu part
  today; below it are the Pi 5 and its **active cooler**. Bottom-side height is
  the stacking-header height minus the cooler's height at that spot. **Measure
  it on the rev 2.0 stack** before placing anything — add it to the §13.1
  list.
- **Only low-profile parts on B.Cu:** 0603 passives, SOT-23, SOIC. Keep tall
  and through-hole parts on top: Y1/Y2 (HC49), C2 (electrolytic), J2/J3, the
  chokes.
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

### 13.5.8 Software implied (for later, not part of the board)

Listed so the hardware choices above are not made blind; none of it is
committed work.

- **U8 driver:** read AIN0–AIN3, publish MAP / IAT as ratios against AIN3,
  apply calibration.
- **Aux socketcan bus as a config role** (HAT CAN1 *or* USB adapter, §13.5.4),
  stable interface naming, and an AEMnet decoder producing engineering-unit
  channels. Keep raw frames out of the vehicle-bus `can:` namespace — "which
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
