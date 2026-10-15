# CANary-PWR — Power Board Spec

> **Status: first-pass capture done 2026-10-13, functional placement
> 2026-10-15 (§P2), unrouted.** The
> KiCad project is [`hardware/canary-pwr-rev1.0/`](../hardware/canary-pwr-rev1.0/)
> (§P11). Opened 2026-10-04, when the operator retired the X1202 and moved every
> power function off the CANary HAT; [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md)
> §13.3.0 records that decision and what it does to HAT rev 3.0. The 2026-10-13
> sessions settled the stack order, the 5 V path, the terminals, the cap bank,
> field programming, the board outline, a parts trim and the parked budget;
> reversed choices are noted where they were reversed, so the reasoning is not
> re-run. Sections are numbered §P1–§P11 so they never collide with the HAT's
> §1–§13.
>
> Values come from each datasheet's design equations and are first-pass. Energy
> and current figures are estimates until §P10's measurements replace them.
>
> **The enclosure is not a constraint** (operator, 2026-10-13). It will be built
> around the finished stack, so nothing here is sized to a case.
>
> **HAT compatibility: rev 2.1 and later only** (operator, 2026-10-13). Rev 2.0
> is not supported (§P8).

## P1. What this board is

A board that sits **between the Pi 5 and the CANary HAT** and owns power for the
whole rig:

- vehicle 12 V input from always-hot OBD pin 16, and its protection;
- **supercapacitor hold-up**, in a separate cap bank on a lead, in place of the
  X1202's four Li-ion 18650s;
- the 5 V supply to the Pi, through the Pi's header;
- 12 V on to the HAT through a jumper, from the backed-up bus, so the HAT's
  vehicle-side front ends ride through cranking too (§P3);
- an **always-on supervisor MCU**: wake, the Pi's power switch, watchdog,
  power-fail signalling, parked battery monitoring, field-reflashable from the
  Pi.

**Why hold-up.** The whole point (operator, 2026-10-13): the Pi stays up
through engine cranking, and shuts down cleanly if the rig is unplugged. Both
need **seconds to a minute**, not hours.

**Why supercaps instead of a battery.** Li-ion is the wrong chemistry for a
truck cab: it sits at 60 °C+ parked in summer and gets charged below 0 °C in
winter. Automotive-grade supercaps are rated about −40 to +85 °C and charging
cold does not wear them.

**Why smart power management.** The rig stays plugged into the OBD port. A
halted Pi 5 still draws ~80 mA at 12 V (§13.3.1), so the Pi has to be genuinely
off while parked, and something always-on has to wake it. The Pi keeps the
*policy* it has today — it decides when to sleep, from `vehicle_state` — and
this board owns the *mechanism*: the Pi's 5 V switch, wake, the watchdog, the
low-battery refusal, and the call between a crank dip and an unplug (§P7).

**Why the supervisor is here and not on the HAT.** Whatever wakes the Pi has to
be alive while the Pi is off. The rev 3.0 HAT MCU runs from the Pi's 3V3
(§13.3.0, so the HAT stays one power domain) and is dark exactly then. It is a
data coprocessor and has no role in power.

**Why an MCU and not a comparator and a latch.** Without firmware every number
is a resistor: the holdoff window after key-off, the VIN thresholds, the
crank-versus-unplug timing, the watchdog period. The holdoff window alone — set
from watching how long the Xterra's bus stays busy after key-off — justifies
firmware, and the MCU also records what happened while parked.

**Parked draw: ~4.5 mA, accepted (operator, 2026-10-13).** The LTC3350 draws
4 mA quiescent and has **no shutdown pin** (datasheet IQ). The first-pass
design parked it behind a supervisor-controlled high-side switch to meet the
original < 1 mA budget; the operator traded that budget for 11 fewer parts.
At ~4.5 mA the rig takes ~3.2 Ah a month from the truck: nothing for a truck
driven weekly, worth watching across a long lay-up (the supervisor logs parked
battery voltage, §P7). The upside: the bank stays topped up, so key-on never
waits for a recharge (§P4).

## P2. Stack and mechanics

```
  CANary HAT        (rev 2.1 and later) — its 13.5 mm J1 plugs onto PWR's header tails
  CANary-PWR        ← this board, plugged directly on the Pi
  Pi 5 + active cooler
  NVMe base         ← in the slot the X1202 + 18650s used
```

**PWR goes under the HAT** (2026-10-13; the 2026-10-04 draft put it on top):

- **5V crosses one connector.** PWR plugs straight onto the Pi's header, the
  arrangement every Power HAT+ product uses (§P5).
- **PWR reads the Pi's pins directly**, so nothing depends on J1 tails protruding
  through the HAT.
- **The HAT stays on top**, so J2's screws, the GNSS antenna and the HAT's own
  test points are reachable without unstacking.

**Outline: the HAT+ 65 × 56 mm outline, no ribbon slot, no overhang**
(2026-10-13). The outline, the four M2.5 mounting holes and J1's position are
taken from the HAT rev 2.1 board; the HAT+ ribbon-cable slot is dropped
(operator) because nothing passes a ribbon up through PWR, and dropping it
frees the left edge. An earlier draft added an 85 × 28 mm overhang past the
Pi's header edge for the connectors; it went because everything a person
touches can be **edge-mounted and reached from the side of the stack**
(operator):

| Edge | Parts (all facing out) |
|---|---|
| Far long edge (y = 100, under the HAT's J2) | *Vehicle in* pluggable terminal, *HAT out* Micro-Fit, status LEDs (side-view) |
| Left edge (x = 100) | Cap-bank connector, bench button (side-actuated), SWD (right-angle) |
| Header edge | J1 only |
| USB/Ethernet end | nothing: the Pi's port stack sits just below PWR there |

The HAT-out connector sits directly under the HAT's J2, so the jumper between
them is a few centimetres.

**Two-sided placement.** The part courtyards alone (~2,870 mm² before the trim)
equal the usable top face of a 65 × 56 board, so the board is populated on both
sides:

- **Top (F.Cu):** the power stage — input protection, LTC3350 and its FETs,
  inductors and electrolytics, the 5 V buck, the ideal diode, the edge parts.
  The gap up to the HAT is ~13 mm (the HAT's 13.5 mm J1 socket sits on PWR's
  tails), enough for the tallest parts (7.7 mm electrolytics, 7 mm inductors)
  and the right-angle connectors.
- **Bottom (B.Cu):** the supervisor, the CAN wake transceiver, the I²C buffer,
  the crossing-net parts, the five cell-ballast resistors, the HAT-out polyfuse
  F1 and two passives — all ≤ 1.75 mm, so F1's part (§P10) must be too.
  The gap down to the cooler is **~6.3 mm** (measured under a 13.5 mm header,
  2026-10-03; operator confirms "a quarter inch", 2026-10-13).
- **Fan-inlet keep-out on B.Cu.** §13.3.13's rule moves to this board: no
  bottom-side parts over the fan inlet, whatever their height. The Active
  Cooler brief gives a 63.5 × 42.5 × 13.7 mm cooler with a 30 × 30 mm fan block
  at one end but not its position on the Pi; the board carries a B.Cu rule area
  at x 136.5–162.5, y 53–79 (the USB end, header side, where the fan cable
  reaches the FAN header) **marked as an assumption to verify** (§P10).

**Heat.** The 5 V buck dissipates ~2 W at full load; keep it and the LTC3350
FETs away from the cap-bank connector and the HAT's barometer/IMU above.

**Placement (2026-10-15)** replaces the generator's packed grid with
functional blocks. Board coordinates run x 100–165 from the left edge and
y 44–100 from the header edge.
- **5 V path, top, along the header edge.** LM61460 (U2) at x ≈ 133–139 with
  its two input-capacitor pairs above and below, L2 to its left, then the
  output capacitors, the R28 shunt and the Q7 ideal diode under J1 pins 2/4.
  5 V can reach pins 2/4 only through the gaps between pins 1, 3 and 5,
  because MH1 blocks the left side. Q7's drain therefore faces up into the
  pin field, and the routing has to use more than one layer.
- **LTC3350, top, centre**, rotated so its power pins face down. The charger
  FETs stack below it, with the switch node toward L1 and C20/C21 across the
  bus and ground. R15, Q5 and the VCAP capacitors sit at the cap-bank power
  pins. RSNSI and the input FET are to its right, and the analogue dividers
  are above it.
- **Input protection, top, lower right:** Q1's tab faces *Vehicle in*, with
  the TVS and the protected-node bulk beside it. The two bus electrolytics
  are at the top right.
- **Bottom:**
  - the I²C buffer and the Pi-crossing parts in a row under J1;
  - the MCU under the buck and LTC3350 analogue area;
  - the always-on LDO beside the fan keep-out;
  - the CAN wake transceiver between the HAT-out and Vehicle-in CAN pins;
  - the ballast resistors stacked beside the cap-bank sense pins. Each takes
    0.5 W while shunting, so spread them if the shunts run for long.
- **Edge order:** the far edge runs LEDs, *HAT out*, *Vehicle in* from left to
  right. The left edge runs button, SWD, cap bank from the header edge down.
- **For HAT rev 3.0** (its sensitive-parts rule, §13.3): the buck occupies
  x ≈ 121–148, y ≈ 51–66 (U2, L2, input capacitors). The charger FETs and L1
  occupy x ≈ 117–136, y ≈ 76–86.

The table is `gen/placement.py`, and `gen_pcb.py` applies it after packing,
so a regenerated board reproduces it. `hardware/tools/review_placement.sh`
scores a placement. Against the packed grid this one cuts the signal
ratsnest from 2,465 to 911 mm and the power nets from 945 to 460 mm, with no
courtyard overlaps.

**J1** is the same 2 × 20 stacking header with a 13.5 mm body that HAT rev 2.1
uses, operator-sourced and fitted (do-not-populate at the fab, §13.5.6), on
B.Cu at the HAT's J1 coordinates (checked pad-for-pad when the board is
generated).

## P3. 12 V input, CAN pass-through and HAT out

**The HAT's input chain moves here**: Q1 (SUD50P06-15 reverse block), its
gate clamp (BZT52C15) and pull-down, D1 (SMBJ24A TVS, 38.9 V max clamp) and
47 µF/50 V bulk. §3.7.1 is the design record. Every capacitor on the 12 V side is
50 V because of D1's clamp; the switching FETs are 60 V for the same reason.

**Vehicle in: a pluggable terminal** (Phoenix MC 1,5/4-G-3,81, order number
1803293; plug MC 1,5/4-ST-3,81). The OBD harness is field wiring, 20 AWG
throughout: its wires are screwed into the plug off the stack and the plug
pushes in from the side, no crimping.

**20 AWG on the power pair** (operator, 2026-10-14). RSNSI caps the input at
4 A (load plus charger), and that peak lasts only while the bank recharges; the
worst steady load (Pi at 5 A plus the HAT) is ~2.9 A at 12 V. A 1 m pigtail
drops ~0.27 V at 4 A, ahead of the buck and far above the 8.95 V PFI
threshold. The plug stays MC 1,5 (8 A, 28–16 AWG): Phoenix's 2.5 mm
FK-MC 0,5 would save ~5 mm of edge but is rated 4 A, no margin over RSNSI, and
RS lists its 4-way plug as discontinued.

| Pin | Net | Wire |
|---|---|---|
| 1 | `+12V_RAW` (OBD 16) | 20 AWG |
| 2 | GND, chassis (OBD 4) | 20 AWG |
| 3 | `CANH` (OBD 6) | 20 AWG |
| 4 | `CANL` (OBD 14) | 20 AWG |

**HAT out: Molex Micro-Fit 3.0, 2 × 3, right-angle (43045-0600)** (2026-10-13,
replacing a 5-way Phoenix MC). Every wire in the jumper is light-duty — 12 V at
≤ 0.5 A behind the polyfuse, CAN, the GNSS backup supply — so 20 AWG on
Micro-Fit's 18–24 AWG contacts is plenty. It is 14.2 mm along the edge against
21.6 mm, latches, and shares the cap-bank connector's family and crimp tool; a
2 × 3 plug cannot go into the bank's 2 × 5 header. The jumper is crimped at this
end and ferruled into the HAT's J2 at the other. Molex numbering, 1–3 on one
row and 4–6 on the other:

| Pin | Net | Pin | Net |
|---|---|---|---|
| 1 | `+12V_HAT` | 4 | `V_BCKP_AON` (rev 2.2/3.0; unused on rev 2.1) |
| 2 | GND | 5 | GND |
| 3 | `CANH` | 6 | `CANL` |

CANH and CANL share a column, so the pair runs side by side; 12 V sits beside a
ground.

**The harness lands once.** 12 V, ground and the CAN pair land on *Vehicle in*;
a short jumper (20 AWG) carries them from *HAT out* to the HAT's J2. The K-line, sensor
wires and everything else still go straight to the HAT. CAN passes through PWR
so the wake transceiver (§P7) sees it without a splice; NUP2105L clamps the pair.

**`+12V_HAT` comes off the backed-up bus through a 0.5 A polyfuse**
(2026-10-13). The 2026-10-13 first pass switched it with a TPS1HB35 smart
switch (11 parts); the trim replaced that with one PTC. It is fed from
`+12V_BUS`, not the raw input, on purpose: the HAT's *logic* already rides
through a crank (it runs on the Pi's rails), but its 12 V front ends — the
K-line transceiver, the K-line pull-up and U9's TMAP supply — sag with the
input, and rev 2.1 only *tolerates* that (ratiometric `5V_SENS`, §13.5.3), it
doesn't keep the readings valid. With the LTC3350 holding the bus ≥ 9 V, MAP
and K-line stay in spec through the crank itself, which is the event §13.3.3(b)
wants. Parked, the HAT's 12 V loads draw ~0.1 mA (mostly its `V12_SENSE`
divider).

**Consequence for software:** the HAT's `V12_SENSE` now reads the bus, not the
battery — identical in normal running, but it stops following the battery below
~9 V. PWR measures the battery directly (`VIN_SENSE` and the LTC3350's
`meas_vin`); `vehicle_state`'s supply-voltage input should come from PWR's
telemetry once PWR is fitted (§P9).

**No barrel jack.** A bench cable with a barrel plug on one end and ferrules
into *Vehicle in* does the same job with no circuitry.

**Ground enters once**, at *Vehicle in*, and reaches the HAT through the jumper
and the header.

## P4. Hold-up — LTC3350 and the cap bank on a lead

**What it must cover:**

1. **The crank dip.** OBD pin 16 sags during cranking. Size from measurement 16
   (§13.1), not from folklore. **Full Pi load must be carried through it**: that
   is what sets the controller choice below.
2. **Unplugged with the Pi running.** A clean shutdown, including the NVMe
   drive flushing its write cache (most consumer NVMe drives have no power-loss
   protection). Budget **≥ 15 s at ≤ 10 W** until measured (§P10).

**Store energy on the 12 V side.** 4 cells in series at 2.5 V (10 V): 4 × 25 F
gives ~234 J ideal, ~200 J delivered, ~25 s at 8 W. Run cells at 2.5 V, not
2.7 V, and pick 85 °C parts: life roughly halves per 10 °C and per 0.1 V.

**Controller: ADI LTC3350** (AEC-Q100). VIN → input ideal-diode FET →
`+12V_BUS` (the system bus every hold-up load hangs off) ↔ bidirectional
synchronous converter ↔ the stack; an output ideal-diode FET connects the stack
straight to the bus whenever the stack is higher. First-pass values (datasheet
Application Circuit 2, 11–20 V in / 10 V backup):

| Item | Value | Why |
|---|---|---|
| FETs (input, output, top, bottom) | 4 × CSD18543Q3A, 60 V, 8 mΩ, logic-level | D1's 38.9 V clamp |
| Input current limit (RSNSI) | 8 mΩ → 4 A, load + charger | the charger yields to the load |
| Charge / backup current (RSNSC) | 8 mΩ → 4 A (peak 7.25 A) | backup power from a low stack; ESR readout scales with it |
| Stack voltage (CAPFB) | 732k/100k → 9.98 V at full-scale DAC | 4 × 2.5 V; the DAC lowers it in firmware (e.g. when hot) |
| Backup bus (OUTFB) | 649k/100k → 9.0 V, 120 pF lead | the buck and the HAT's 12 V loads stay in regulation |
| Power-fail (PFI) | 665k/100k → 8.95 V falling | PFO goes to the **MCU**, not the Pi (§P7) |
| Switching frequency (RT) | 71.5k → 748 kHz | |
| Inductor | 4.7 µH XAL7070 (Isat > 7.2 A) | L = VIN(max)/(ICHG·fSW); same part as the buck's |
| Bootstrap diode | 1N4148W, not Schottky | datasheet: BST–SW pumps past 5.5 V with a Schottky beside the output ideal diode |
| Capacitance test (RTST) | 121 Ω; `ctl_cap_scale = 1` | ≈ 2 mF per LSB on a 6.25 F stack |
| Cell ballast | 5 × 2.7 Ω 2512 | 500 mA shunt at VSHUNT 2.7 V; ~0.5 W each while shunting |
| Thermistor | 10 kΩ 0.1 % from INTVCC to GPI; NTC on the bank | the LTC3350 reads it (`meas_gpi`) and can alarm on it |

**Always on, always topped up.** With VIN straight from the protected input the
LTC3350 is never off; that is the 4 mA (§P1). It also answers the old
gate-versus-latency question: the bank is charged at every key-on, so the
supervisor need not hold the Pi off waiting for a recharge.

**The bank is a separate board on a lead.** Four 25 F/2.7 V cells
(~16 × 26 mm each), the NTC and a connector; no active parts. It lets the caps
sit somewhere cooler than between two boards over a Pi, and placed where the
enclosure wants them.

- **Connector: Molex Micro-Fit 3.0, 2 × 5, right-angle (43045-1000), keyed and
  latching.** Ten pins, not the seven first planned, because the stack's top
  and bottom get **Kelvin sense wires**: otherwise the cell-4 and cell-1
  measurements include the drop along the power wires at 4 A.

  Molex numbering: pins 1–5 on one row, 6–10 on the other.

  | Pin | Net | Pin | Net |
  |---|---|---|---|
  | 1 | Stack + (power) | 2 | Stack + (power) |
  | 3 | Stack − (power) | 4 | Stack − (power) |
  | 5 | Stack + sense | 6 | Stack − sense |
  | 7 | Tap 3 | 8 | Tap 2 |
  | 9 | Tap 1 | 10 | NTC (to stack − on the bank) |

- **Lead length ≤ ~30 cm**; record its resistance at bring-up so the ESR
  readout can subtract it.
- **Discharging the bank is firmware, not a circuit** (2026-10-13 trim: the
  FET-and-resistor discharge bank went). Lowering `vshunt` makes each cell's
  shunt bleed ~0.5 A through its ballast resistor (datasheet: the shunt setting
  "may also be used to discharge the capacitors"). It needs the LTC3350
  powered, which it always is while the rig is plugged in. **Rule: discharge
  before unplugging the bank** — 6 F at 10 V into a screwdriver is a welding
  event. The bench button held, or an I²C command, starts it; the red LED shows
  the bank is live.

**Alternative considered: LTC4041** (2026-10-13). A 2.5 A supercap backup
manager for 2.9–5.5 V rails: it would sit between the 5 V buck and the Pi with
1–2 cells (≤ 5.4 V).

| | LTC3350 (chosen) | LTC4041 |
|---|---|---|
| Rail | 12 V input, 4.5–35 V | 5 V rail, 2.9–5.5 V (abs max 6 V) |
| Cells | 4 in series, 10 V | 2 in series, ≤ 5.4 V |
| Through a crank | bus held ≥ 9 V, the buck delivers its full 6 A | **2.5 A rated backup** at 5 V — below a Pi 5 + NVMe peak |
| HAT's 12 V in a crank | held (polyfuse from the bus) | not held |
| Parked | 4 mA, no shutdown | 5.5 µA shutdown, ≤ 1 µA from the caps |
| Telemetry | I²C: per-cell voltages, currents, VIN/VOUT, NTC, capacitance and ESR, alarms | status pins + analog input-current output; no I²C, no capacitance/ESR |
| Qualification | AEC-Q100 | not claimed |
| Parts on PWR | ~42 incl. 4 FETs | ~15–18 (internal switches, 1 FET) |

Energy per cell is the same either way, so the same backup time needs about
the same cell volume. **Revisit only if** the INA226 shows the Pi's 5 V current
during cranking stays well under 2.5 A on real drives (§P10): then the LTC4041
saves ~25 parts and the 4 mA.

## P5. 5 V to the Pi — LM61460 through the header

**Buck: TI LM61460AASQRJRRQ1** (36 V, 6 A, auto mode, spread spectrum), fed
from `+12V_BUS`. Datasheet Table 10-2, 400 kHz / 5 V row: 4.7 µH (XAL7070),
RFBT 100k / RFBB 24.3k → 5.115 V, CFF 22 pF + RFF 1k, RT 33.2k, CBOOT 100 nF with
RBOOT tied straight to CBOOT, 2 × 47 µF out, split 10 µF + 100 nF at each
VIN/PGND pair. Its enable is the Pi's power switch: a 100k/27k divider from the
bus (on above ~5.9 V) pulled down by a BSS138 the MCU drives — **MCU high = Pi
off**, so a supervisor in reset, in its bootloader or hung leaves the Pi powered.

**Delivery: header pins 2 and 4** (decided 2026-10-13). The Raspberry Pi HAT+
spec defines this as a Power HAT+, requires ≥ 3 A at 5.1 V and recommends 5 A;
shipping Pi 5 PoE HATs deliver 4.5 A this way. With PWR directly on the Pi the
current crosses one connector and no HAT copper.

Between the buck and the header:

- **5 mΩ shunt + INA226-Q1** (private I²C, 0x40): the Pi's 5 V current and the
  voltage at the header — the number that diagnoses undervoltage with NVMe and
  USB loads, and the measurement that decides the LTC4041 question (§P4). Put it
  in power-down while parked.
- **LM74700-Q1 + CSD18543Q3A ideal diode.** A Power HAT+ "must only source
  current and tolerate accidental use while connected to USB-C power": the Pi's
  own USB-C is the bench fallback, and this keeps it from pushing into the buck.

About 50 mV is lost across the shunt and the ideal diode at 5 A, which the
5.115 V setpoint covers against the Pi's 4.63 V undervoltage threshold.

**No ID EEPROM.** The Power HAT+ classes (EEPROM 0x52/0x53) must not connect to
GPIOs 2–27, and PWR uses I²C1 and three GPIOs; the Standard class (0x50) belongs
to the HAT. Set `usb_max_current_enable=1` (or `PSU_MAX_CURRENT=5000`) in config
instead (§P9).

## P6. Connections

**To the Pi, directly on its header:**

| Net | Pin | Direction | Purpose |
|---|---|---|---|
| `+5V` | 2, 4 | PWR → Pi | Pi and HAT 5 V, through the ideal diode |
| GND | 6, 9, 14, 20, 25, 30, 34, 39 | | |
| I²C1 SDA/SCL | 3, 5 (GPIO2/3) | Pi master, PWR target | through a **TCA4307** powered from the Pi's 3V3 |
| `~PWR_FAIL` | 31 (GPIO6) | PWR → Pi | **HIGH = power OK**, the X1202's polarity, so existing software keeps its pin. MCU open-drain, 10k pull-up to the Pi's 3V3 |
| `PI_BOOT0` | 13 (GPIO27) | Pi → PWR | Holds the MCU's BOOT0 high for a reflash (10k pull-down on PWR) |
| `PI_NRST` | 18 (GPIO24) | Pi → PWR | Resets the MCU, through a BSS138: a resistor would let an unpowered Pi pin clamp NRST and hold the supervisor in reset |
| Pi 3V3 sense | 1, 17 | Pi → PWR | Halt detection: on a Pi 5, halt = 3V3 down. 10k series into the MCU |

GPIO24 is free on rev 2.1 (its K-line pull-up moved to GPIO21, §13.5.13);
GPIO27 is free on rev 2.1. Every other header pin passes through to the HAT
untouched.

**Crossings.** This board is always-on and the Pi is not, so every net into
the Pi's domain has a safe state with the Pi dark: the I²C buffer has
powered-off high-impedance I/O and its card-side pull-ups go to the **Pi's**
3V3, so the always-on side can never pull up, drive or hang I²C1 (which also
carries the HAT's IMU); `~PWR_FAIL`'s pull-up is on the Pi's rail too; BOOT0
idles low; NRST is behind a FET.

**MCU private I²C** (4.7k pull-ups to the always-on 3.3 V): LTC3350 (0x09),
INA226 (0x40). The Pi sees one target, the supervisor; the system bootloader
answers at 0x56 on the same pins (§P7).

**Edge connectors:** *Vehicle in* (Phoenix MC), *HAT out* (Micro-Fit 2 × 3)
(§P3), cap bank (Micro-Fit 2 × 5, §P4), SWD (Cortex 10-pin, right-angle, bench
only).

**Rule: no 3.3 V or 5 V logic rail goes to the HAT except through the Pi.** The
HAT's logic stays on the Pi's 3V3 so it powers up and down with the Pi (§3.7.5).

## P7. Supervisor

**MCU: STM32G031K8T6** (Cortex-M0+, 64 KB, LQFP-32) on an always-on 3.3 V
rail: a TPS7A1633-Q1 (60 V, ~5 µA Iq) fed by two BAT46WJ diodes from the
protected input and from `+12V_BUS`, so it stays up through hold-up after the
input is lost. 100 V diodes because one leg can sit at 0 V while the other sees
the 38.9 V clamp.

| Function | Pin | Notes |
|---|---|---|
| Pi-facing I²C (also bootloader I²C1) | PB6 SCL, PB7 SDA | AN2606 Table 105: STM32G03x bootloader I²C1 = PB6/PB7, target 0x56 |
| Private I²C | PA11 SCL, PA12 SDA | I2C2 |
| SWD / BOOT0 | PA13 SWDIO, PA14 SWCLK = BOOT0 | |
| Reset | PF2-NRST | 100 nF; also from the Pi via FET, and on the SWD header |
| Battery voltage | PA0 (ADC) | 1M/150k divider from the protected input, ~10 µA |
| Pi 3V3 sense | PA4 | |
| Pi's 5 V off | PB0 | to the buck's enable FET |
| `~PWR_FAIL` | PA8 | open-drain |
| LTC3350 PFO / SMBALERT | PB8 / PB9 | internal pull-ups |
| Buck PGOOD | PC14 | internal pull-up — **drop it while parked**: PGOOD sits low with the buck off |
| CAN wake: RXD / EN / nSTB / nFAULT | PB4 / PA15 / PB3 / PB5 | RXD with internal pull-up |
| Button, LEDs | PA5 / PA6 green, PA7 red | button with internal pull-up |
| Spare | PA1–PA3, PB1, PB2, PC6, PC15 | freed by the trim |

**Field reflash from the Pi.** The Pi holds GPIO27 (BOOT0) high and pulses
GPIO24 (NRST); the MCU starts its system bootloader as an I²C target on I²C1 at
0x56 (AN2606); the Pi writes the image (AN4221 protocol) and releases both. One
SWD session at bring-up sets the option bytes: `nBOOT_SEL = 0` (honour the BOOT0
pin — AN2606 "Pattern 11") and the hardware IWDG on (the bootloader refreshes
it). During a reflash the 5 V stays on by the default-on rule, so the Pi cannot
cut its own power by updating the supervisor.

**Wake.** A **TCAN1043-Q1** on the CAN pass-through, TXD tied to VIO so it is
**physically incapable of transmitting** (§13.3.5's receive-only rule, enforced
by wiring). Its VCC comes from the buck, so while parked it sits in sleep; a
bus wake-up pattern pulls RXD low (datasheet 8.4.6.1), which wakes the MCU.
Also: VIN rising past ~13.2 V (alternator), and the bench button.

**States:**

```
 PARKED ──wake──► RUN ──Pi 3V3 drops──► OFF_HOLDOFF ──bus quiet──► PARKED
                   │
                   └──PFO──► DIP? ──input back within the window──► RUN
                               └──input still gone──► HOLDUP ──Pi halted / bank empty──► PARKED
```

- **PARKED.** Buck off, Pi off; the LTC3350 keeps the bank topped up (4 mA);
  the HAT's 12 V loads idle (~0.1 mA); the transceiver sleeps.
- **RUN.** The Pi decides when to shut down (sustained `engine_off` in
  `vehicle_state`, or an I²C sleep command). When Pi 3V3 drops, the supervisor
  cuts the buck — required, not a nicety, at 80 mA halted.
- **DIP vs HOLDUP.** The LTC3350's PFO comes to the MCU, not the Pi: a crank
  drops VIN below the 8.95 V PFI threshold for a second or two and the bank
  carries the bus through it, invisibly. The MCU asserts `~PWR_FAIL` only when
  the input stays gone past a window, or VIN reads like an unplug (near 0 V).
  The Pi then shuts down at once.
- **Crank summary** (2026-10-14). For every dip judged to be a crank, record
  the resting VIN just before it, the plateau minimum, the duration and the
  recovery time from `VIN_SENSE`. This board is the only one that sees the
  battery below ~9 V: the HAT's `V12_SENSE` is on the held bus (§P3). This is
  the battery-health trend from HAT §13.3.3(b). `VIN_SENSE` is ~12 Hz wide
  (1 MΩ ∥ 150 kΩ into C3, 100 nF), enough for the plateau but not the first
  inrush spike. Keep it a summary, or at most a ~100 Hz waveform (~400 B per
  crank): no DMA buffers and no timebase work on the supervisor.
- **OFF_HOLDOFF.** After key-off the bus stays busy while modules wind down;
  re-arm wake only after it has been quiet for a window set from observation.
- **Watchdog.** Heartbeat over I²C; on timeout, power-cycle the Pi.
- **Low-battery refusal.** Do not power the Pi from a resting VIN below
  ~11.8 V. A no-start is the one failure this rig could inflict on the truck.
- **Default-on.** In reset, in the bootloader or hung, the MCU's pins float and
  the pull-downs leave the buck enabled: a dead supervisor degrades to an
  always-on Pi, never a dark rig.

**Telemetry.** VIN, bus and stack voltages, per-cell voltages, charge/input
current, measured capacitance and ESR, bank temperature (LTC3350); Pi 5 V
voltage and current (INA226); state, last wake source, last shutdown cause,
firmware version, parked battery-voltage history, crank summaries. The Pi ingests the parked
history at each boot.

## P8. Running with HAT rev 2.1

- **12 V and CAN.** The OBD harness's 12 V, ground and CAN pair land on PWR's
  *Vehicle in*; the 20 AWG jumper from *HAT out* (Micro-Fit pins 1, 2, 3, 6) lands
  on rev 2.1's J2: `+12V_HAT` → pin 1 (`+12V_RAW`), GND → pin 2, CANH/CANL →
  the HAT's CAN pair. The HAT's own
  Q1/D1 stay in circuit behind PWR's. J2 pins 3–4 (the old X1202 feed) go
  unused.
- **Header.** The HAT's J1 plugs onto PWR's stacking header; nothing on the
  HAT's header changes.
- **GNSS backup.** Unchanged (BT1). `V_BCKP_AON` waits for rev 2.2/3.0; the
  terminal position is there already.
- **Rev 2.0 is not supported** (operator, 2026-10-13): it uses GPIO24 for its
  K-line pull-up, which PWR now uses for the MCU reset.

### Running with HAT rev 3.0

**Required (operator, 2026-10-15).** HAT §13.3.14 holds the check and the
rules. In short:
- The rev 3.0 header map uses none of this board's signal pins (1/17, 3/5, 13,
  18, 31).
- The HAT leaves GPIO2/3, 6, 24 and 27 unconnected, with no I²C1 pull-ups.
- HAT out pin 4, `V_BCKP_AON`, lands on rev 3.0's 12-position J2 and feeds the
  GNSS V_BCKP (BT1 retired). The HAT adds its own series 1 kΩ and a clamp
  against a harness short to `+12V`.
- The HAT keeps rev 2.1's outline, holes and J1 position, holds its B.Cu parts
  to ≤ 2 mm, and keeps B.Cu clear above this board's far-edge connectors until
  §P10 item 8 is measured.
- Keep the HAT's IMU, ADC and crystal off the area above the 5 V buck.

Nothing on this board changes for rev 3.0.

## P9. Software implied

- **Replace `scripts/ups_shutdown_watch.py`'s logic.** `~PWR_FAIL` (GPIO6, HIGH =
  OK) is now unambiguous — the supervisor already filtered out crank dips — so
  the watcher becomes: on LOW, shut down now. `src/daemon/ups.py`'s `input_lost`
  heartbeat field maps onto it directly.
- **Key-off shutdown**: escalate sustained `engine_off` in `vehicle_state` to a
  poweroff (§3.7.6, §11).
- **Battery voltage source.** With PWR fitted the HAT's `V12_SENSE` reads the
  backed-up bus (§P3); `vehicle_state`'s `supply_voltage` should come from PWR's
  telemetry.
- **Ingest** the supervisor's telemetry and parked history: a small I²C reader
  feeding a status field and channels.
- **LTC3350 setup** (through the supervisor): `vshunt` below 2.7 V for margin,
  `vcapfb_dac` (lower when hot), `ctl_cap_scale = 1`, periodic capacitance/ESR
  runs, alarm thresholds; the bank-discharge command (§P4).
- **Reflash tool**: drives GPIO27/GPIO24, talks the STM32 I²C bootloader
  protocol (AN4221), verifies, releases. Refuses while a capture is recording,
  like `scripts/restart_canary.sh`.
- **Pi config**: `usb_max_current_enable=1` (or `PSU_MAX_CURRENT=5000`) — no
  EEPROM sets it (§P5); `BOOT_ORDER=0xf416` once the NVMe base is fitted;
  `PCIE_PROBE=1` and `dtparam=pciex1` if the NVMe base has no HAT+ EEPROM.

## P10. Open items and measurements

| # | What | Decides |
|---|---|---|
| 1 | Fan-inlet position on the Pi 5 (centre and diameter relative to a mounting hole) | The B.Cu keep-out, now an assumption (§P2) |
| 2 | Crank dip profile at OBD pin 16 (§13.1 measurement 16) | Bank size; whether 8.95 V PFI is right |
| 3 | Shutdown time and peak power on NVMe, `~PWR_FAIL` to Pi 3V3 down | The ≥ 15 s / ≤ 10 W budget |
| 4 | Pi 5 V current through cranks on real drives (INA226) | Whether the LTC4041 would do (§P4) |
| 5 | Pi 5 3V3 rise after 5 V is applied (§13.1 measurement 15) | 3V3 sense as the halt signal; the rev 3.0 split |
| 6 | Bus-quiet time after key-off on the Xterra (from existing captures) | OFF_HOLDOFF window |
| 7 | Crank-dip window: longest dip the bank should ride before `~PWR_FAIL` | DIP vs HOLDUP timing |
| 8 | Right-angle connector heights (Phoenix MC 1,5 G, Micro-Fit 43045 2 × 3 and 2 × 5, SWD) against the ~13 mm gap under the HAT | Edge placement |
| 9 | Side-view LED and side-actuated button orientation on the footprints | They face the edge |
| 10 | Part choices still open: the polyfuse (0.5 A hold, ≥ 40 V), the right-angle SWD header, the 25 F cells | BOM |
| 11 | The cap-bank lead's resistance at bring-up | Calibrates the ESR readout |
| 12 | The Vehicle-in plug's order number (KiCad's footprint confirms 1803293 for the header) and the Micro-Fit housings and crimps (43025-0600, 43025-1000, 43030) | BOM |
| 13 | Copper weight: TI recommends 2 oz outer layers for the LM61460 | Fab order |
| 14 | Alternator ripple at `+12V_HAT` against `VIN_SENSE`'s input, engine running | Whether the HAT coprocessor can measure ripple from `V12_SENSE` through the hold-up path (HAT §13.3.3b) |
| 15 | Routing pass on the 2026-10-15 placement: 5 V into J1 pins 2/4 through the pin-field gaps on several layers; the LTC3350 BG gate trace around Q3 (or through a via pair); Kelvin pairs to RSNSI, RSNSC and R28; silkscreen designators, which the denser packing put over neighbouring pads; the GND thermal spokes DRC reports as starved | Layout |

## P11. Hardware project

`hardware/canary-pwr-rev1.0/` — KiCad 10, a root sheet and five sub-sheets
(Input, Holdup, Pi_Supply, Supervisor, Pi_Interface), **120 parts**, 4 layers
(the HAT rev 2.1 stackup and rules). State at 2026-10-13: ERC clean at every
severity; the exported netlist checked pin-for-pin against the design data
(the checker was shown to fail on a deliberately broken label); placement done
on both sides (functional placement 2026-10-15, §P2), **unrouted**; DRC clean
of clearance, courtyard and parity errors, with silkscreen overlap and
zone-spoke items left for layout (§P10 item 15).

**Libraries** (`lib/`). Every vendor footprint was checked pad-by-pad against
its datasheet land pattern, and two of five were wrong:

| Part | Source | Result |
|---|---|---|
| LTC3350 | Ultra Librarian | Matches ADI's UHF land pattern (toe +0.1 mm, EP at max size) |
| LM61460 | SnapMagic, with STEP | Matches TI RJR0014A exactly, incl. the L-shaped corner pins. The Ultra Librarian version landed only the foot of those L pads and was discarded |
| TCA4307 | Ultra Librarian | Standard VSSOP-8 |
| TPS7A1633A | Ultra Librarian (DGN0008C) | Exposed pad renumbered `EPAD` → `9` so it joins GND |
| TPS1HB35 (since trimmed) | — | Both UL versions wrong (stencil-sized EP; thermal vias off the EP net) |

The UL symbols' pin numbers match the datasheets; their electrical pin types
were corrected (UL marked the LM61460's ground pins as power outputs).

**`gen/`** generated the first pass: `design.py` is the netlist with every
value's rationale in each part's Note field; `gen_proj.py` writes the project and
schematic, `verify_netlist.py` checks KiCad's netlist against it, `gen_pcb.py`
builds the board on the HAT rev 2.1 template (run with KiCad's own Python) and
places it from `placement.py`. `placement_review.json` configures
`hardware/tools/review_placement.sh` for this board. Since 2026-10-15 a
regenerated board matches the committed one footprint for footprint.
**Once the KiCad files are edited by hand they are the source of truth;
regenerating overwrites them.**
