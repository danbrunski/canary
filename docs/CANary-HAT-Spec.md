# CANary-HAT — Board Specification

> **Revision status.** This document describes the board **as drawn in the
> KiCad project at `C:\Users\thebo\Documents\canary-hat`** (schematic and PCB
> both dated 2026-08-10), not the original design intent. Everything in §2–§7
> was read back out of the schematic netlist, the PCB file, and the project's
> design rules. Where the as-built circuit contradicts the design intent that
> earlier revisions of this document recorded, the contradiction is called out
> in place; §8.1 logs the defects that were found this way and have since been
> fixed.
>
> Board state: **rev 1.0 was submitted to PCBWay 2026-08-11 and never
> fabricated** — the quote stalled, and the submission was withdrawn 2026-08-22
> in favour of **rev 2.0**, which is now the board that will be built first
> (§13.2). Rev 1.0 remains the reference design this document describes: it is
> complete and fully routed, 0 unconnected items, 0 DRC violations, 0
> schematic-parity issues, ERC **0 errors / 3 benign warnings**, and no schematic
> defects remain (§8.1). *(The "5 warnings" this document carried came from
> `ERC-fresh.rpt` dated 2026-08-11; the schematic was revised on 2026-08-15 and
> a fresh `kicad-cli sch erc` run on 2026-08-22 — before and after the rev-2.0
> edits alike — reports 3. Corrected rather than re-derived next time.)* Rev 2.0 is a delta against it, so §2–§12 stay accurate
> for everything rev 2.0 does not change.
>
> **Nothing was ever measured on a physical board.** §8.3's "confirm before
> fabrication" list and §13.1's bring-up measurements now both land on rev 2.0,
> which changes how several rev-2.0 items had to be decided — see §13.2.

> **Part of the CANary-HAT board documentation**, split across four files.
> Section numbers are **stable across the split** — a bare `§3.7.5` means the
> same thing in every file; this table says which file holds it.
>
> | Sections | File |
> |---|---|
> | §1, §2, §4, §5, §8–§11 | [`CANary-HAT-Spec.md`](CANary-HAT-Spec.md) — what the board is: overview, stackup, pin map, open items, implied software work |
> | §3 | [`CANary-HAT-Circuits.md`](CANary-HAT-Circuits.md) — why each part is wired this way, per subsystem |
> | §6, §7, §12 | [`CANary-HAT-Fabrication.md`](CANary-HAT-Fabrication.md) — KiCad implementation, manufacturing notes, assembly readiness |
> | §13 | [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md) — bring-up measurements, rev 2.0, rev 3.0, lessons from prior art |

## 1. Overview

The CANary-HAT is a 4-layer Raspberry Pi 5 HAT integrating a CAN FD interface,
K-line/LIN transceiver, 6-axis IMU, barometric pressure sensor, GNSS/GPS
receiver, HAT ID EEPROM, and a protected 12V vehicle input. The board is
designed for vehicle telematics, diagnostics, and asset-tracking applications
requiring simultaneous CAN, K-line, motion, altitude, and positioning data on a
Raspberry Pi 5 host.

**Role in the CANary stack.** This board **replaces the Waveshare 2-CH CAN HAT**
as the in-vehicle CAN interface, and additionally folds in the GNSS receiver,
IMU, and barometer that are currently unbuilt or externally attached. It does
**not** replace the X1202 UPS HAT, which continues to power the Pi and own
battery-backed shutdown. See §3.7 for the resulting power topology and §11 for
the software work each subsystem implies.

**The buck converter is gone.** Earlier revisions specified an AP63203Q
generating a board-local 3V3_BUCK rail from vehicle 12V, and then argued
(§3.7.4/§3.7.5, retained below) that the rail should be depopulated in favour of
the Pi's 3V3. The current schematic goes further and **omits the buck and its
footprint entirely**: every 3.3V device on the board is fed from the Pi header's
3V3 pins. That decision is now settled and is reflected throughout this
document; §3.7.4/§3.7.5 are kept because the reasoning still governs any future
attempt to re-add a local rail.

## 2. Board Stackup

| Layer | Assignment | As-built |
|---|---|---|
| F.Cu | Signal + GND pour | Components, primary routing (341 segments), **GND pour** |
| In1.Cu (`Gnd.Cu`) | Ground plane | **Solid GND pour**, board-wide, with one anti-pad notch at the SMA launch (§3.6) |
| In2.Cu (`Power.Cu`) | Power plane | **Single `+3V3` pour** (zone named `Pwr`) — see note |
| B.Cu | Signal + GND pour | Secondary routing (104 segments), **GND pour** |

**Outer-layer GND pours (added 2026-08-11)** — one zone spanning F.Cu and B.Cu,
priority 2, filling to 9 regions. Two pad-connection settings are deliberate, and
both were arrived at by failing DRC first:

- **J4's SMA ground pads connect *solid*, not thermal relief.** Thermal spokes add
  inductance exactly at the RF launch, and solid connections are exempt from the
  minimum-spoke check they were failing.
- **J1's pads connect with *None*.** At 2.54mm pitch with 1.7mm pads the edge gap
  is 0.84mm, while a 0.5mm thermal gap needs 1.0mm — so the pour physically cannot
  pass between the header pins and fragments into isolated pockets that then catch
  the pads' spokes. J1's GND pins reach ground through their plated barrels into
  In1.Cu regardless, so the outer pours were never carrying that current. This
  took the fill from 15 fragments to 9.

**What the outer pours are — and are not.** The primary return path is **In1.Cu**,
a solid unbroken plane (§3.9). The F.Cu/B.Cu pours are secondary: local shielding,
thermal spreading, and a convenient GND landing for component pads. **A pad
touching the outer pour does not by itself have a low-inductance return** — its
return runs through the pour to the nearest via down to In1.Cu, so an outer pour
is only as good as its stitching. That distinction is what makes "give C4 a via"
(§8.3) a real instruction rather than a restatement of "put it on the pour".

**Adjacent vias, never via-in-pad.** Via-in-pad requires filling and capping (or
plugging) and is a cost adder not every fab quotes cheaply — and nothing on this
board needs it. There is no exposed thermal pad anywhere: U5 and U6 are LGAs
without one, U8 is a plain TSSOP-10, and Q1's DPAK tab dissipates 0.11W at the 3A
budget, so it needs no thermal via field either. Place stitching vias *beside*
the pad with the shortest stub the annular ring and clearance allow.

**The numbers say a short stub is plenty here.** A trace stub runs roughly
1 nH/mm, so 1–2mm is 1–2 nH. Against C4's own ~10Ω at 70 MHz — where a 5 ns
ISO 7637 edge has its content — 1 nH contributes ~0.44Ω. Immaterial. The board
also has an unusual advantage: the **0.1mm outer prepreg** required for the RF
launch (§7) means F.Cu to In1.Cu is a very short electrical hop, so a via's own
contribution to reaching the ground plane is smaller here than on a typical
4-layer stackup. Where stub length does start to matter is the 100n decoupling
caps, which are inductance-dominated well before 70 MHz — those want the tightest
placement, and two vias rather than one halves the inductance for free.

**Stitching is thin and uneven, measured 2026-08-24 on the rev 1.0 layout: 21 GND
vias, ~13mm average spacing, but 14 of them in the right third (x 148–164),
leaving the left half — J2, U4, J1, the K-line and CAN region — with seven.** That
is adequate at DC and audio but coarse for anything fast, and it is the half of
the board where the transient-carrying nets live. **Add GND stitching during the
rev 2.0 routing pass**, particularly at every IC's GND pad and through the
left-hand region; vias are nearly free and this is the cheapest time to place
them. Note the connectors themselves are unaffected — J2 and J3 are through-hole,
so their ground pins reach In1.Cu through their own plated barrels, the same
argument that applies to J1 above.

- Board outline: **65.0mm × 56.0mm** (X 100–165, Y 44–100 in board coordinates),
  standard Raspberry Pi HAT form factor.
- Board thickness: 1.6mm. Stackup as entered in the project:
  0.035 / **0.1 prepreg** / 0.035 / 1.24 core / 0.035 / **0.1 prepreg** / 0.035 mm.
  The thin outer dielectrics matter: they are what make a 0.2mm F.Cu trace over
  In1.Cu land near 50Ω (§3.6).
- 1oz copper per layer, **64 through-hole vias** (stitching added at the RF launch
  and through the outer pours), no blind/buried vias, no microvias.
- Mounting holes at (103.5, 47.5), (161.5, 47.5), (103.5, 96.5), (161.5, 96.5) —
  58mm × 49mm, the standard HAT hole pattern.
- All components are on **F.Cu**; only the 40-pin socket J1 is on **B.Cu**, as a
  HAT requires.

**Plane note (revised).** The earlier plan for a mixed-rail Power.Cu carrying
+12V plus derived rails did not survive the removal of the buck. Power.Cu is now
a **single-net +3V3 pour**, so B.Cu signals crossing it keep a continuous return
— but the return is a *power* plane, not ground. Fast signals (SPI up to 20 MHz,
the GNSS RF feed) still belong on F.Cu over the solid Gnd.Cu pour. **+12V is
routed as tracks, not as plane copper** (see §3.7.3 for why the width matters).

### 2.1 Design rules and net classes (from `canary-hat.kicad_pro`)

| Net class | Track | Clearance | Via (dia/drill) | Pattern |
|---|---|---|---|---|
| Default | 0.20mm | 0.20mm | 0.6 / 0.3 | — |
| Signal | 0.20mm | **0.15mm** | 0.6 / 0.3 | `*GPIO*`, `*SPI0*`, `*SDA*`, `*SCL*`, `*RXD*`, `*TXD*` |
| CAN | 0.25mm | 0.20mm | 0.6 / 0.3 | `*CAN*` |
| Power | 0.35mm | 0.20mm | 0.9 / 0.5 | `+3V3*`, `+5V` |
| Power_High | **0.40mm** | 0.20mm | 0.9 / 0.5 | `*12V*` |
| GND | 0.60mm | 0.20mm | 0.7 / 0.4 | `*GND*` |
| **RF** | 0.20mm | 0.20mm | — | `*J4-In*`, `*U7-RF_IN*` |

**Two custom rules live in `canary-hat.kicad_dru`**, both scoped rather than
global — each exists because a blanket rule collided with a manufacturer land
pattern, which happened twice:

- **`U6 LGA-14L land pattern`** — 0.14mm clearance inside U6's courtyard. The
  LSM6DSOX land pattern has 0.1952mm pad-to-pad gaps against the 0.2mm default
  (§3.4). Scoped so board-wide clearance stays 0.2mm.
- **`RF feed`** — 0.5mm between the RF netclass and any *zone*. The RF feed is
  0.2mm over a 0.1mm dielectric; a GND pour at the default 0.2mm sits at
  gap/height = 2, which turns the microstrip into a grounded coplanar waveguide
  and drags Z₀ from ~45Ω toward the low 30s. **This constrains the pour only.**
  Putting the 0.5mm on the netclass instead breaks U7's land pattern, where RF_IN
  (pad 11) sits **0.262mm** from its neighbouring ground pads — no netclass
  clearance above 0.262mm can ever pass there.

The general lesson, recorded because it cost two rounds: *constrain the thing you
actually mean*. "Keep pours off this trace" is a relationship between a pour and a
trace; expressing it as a property of the net drags in every pad-to-pad pair on
that net, and land patterns are not yours to change.

Board minima: track 0.15mm, via 0.4mm, annular ring 0.15mm, hole-to-hole
0.25mm, PTH 0.3mm, copper-to-edge 0.05mm. Measured widths on the routed board
generally match the classes: CANH/CANL at 0.25mm, K-line and the RF feed at
0.20mm, GND up to 0.60mm. The two 12V nets are routed **well above their
`Power_High` class** wherever it matters: the `+12V_RAW` pass-through leg from J2
to J3 is **1.3mm** because it feeds the Pi (§3.7.4), while the mA-level branches
— up to D4's anode, and all of `+12V` past it — sit at 0.6mm with a 0.4mm stub.

## 4. Raspberry Pi 5 GPIO Header Mapping

Read directly from the netlist, with header pin numbers taken as authoritative.
Every net now lands on the pin its label names.

| Header pin | Pi function | Net label | Connected to |
|---|---|---|---|
| 1, 17 | 3V3 | `+3V3` | Every logic device on the board |
| 2, 4 | 5V | `+5V` | U4 (TCAN1042) VDD, C8 |
| 3 | GPIO2 / SDA1 | `GPIO2/SDA1` | U5.4 (BMP384 SDI), U6.14 (LSM6DSOX SDA), R9 |
| 5 | GPIO3 / SCL1 | `GPIO3/SCL1` | U5.2 (BMP384 SCK), U6.13 (LSM6DSOX SCL), R7 |
| 7 | GPIO4 / `uart2` TX | `GPIO4/TXD1` | U7.3 (MAX-M10M RXD) |
| 29 | GPIO5 / `uart2` RX | `GPIO5/RXD1` | U7.2 (MAX-M10M TXD) |
| 8 | GPIO14 / `uart0` TX | `GPIO14/TXD0` | R6 (1 kΩ) → U2.4 (TLIN1027 TXD); R8 (4.7 kΩ pull-up to +3V3) |
| 10 | GPIO15 / `uart0` RX | `GPIO15/RXD0` | U2.1 (TLIN1027 RXD), R3 (4.7 kΩ → +3V3) |
| 37 | GPIO26 | `GPIO26` | U2.2 (TLIN1027 EN), R4 (4.7 kΩ pull-down to GND) |
| 19 | GPIO10 / SPI0 MOSI | `GPIO10/SPI0.MOSI` | U3.11 (MCP2518FD SDI) |
| 21 | GPIO9 / SPI0 MISO | `GPIO9/SPI0.MISO` | U3.12 (MCP2518FD SDO) |
| 23 | GPIO11 / SPI0 SCLK | `GPIO11/SPI0.SCLK` | U3.10 (MCP2518FD SCK) |
| 24 | GPIO8 / SPI0 CE0 | `GPIO8/SPI0.CE0` | U3.13 (MCP2518FD nCS) |
| 22 | GPIO25 | `GPIO25` | U3.4 (MCP2518FD nINT) |
| 36 | GPIO16 | `GPIO16` | U6.4 (LSM6DSOX INT1) |
| 11 | GPIO17 | `GPIO17` | U5.7 (BMP384 INT) |
| 12 | GPIO18 | `GPIO18/PPS` | U7.4 (MAX-M10M TIMEPULSE) |
| 27 | ID_SD (GPIO0) | `ID_SDA` | U1.5, R1 |
| 28 | ID_SC (GPIO1) | `ID_SCL` | U1.6, R2 |
| 6, 9, 14, 20, 25, 30, 34, 39 | GND | `GND` | Ground pour |

**Unconnected header pins:** 13 (GPIO27), 15 (GPIO22), 16 (GPIO23), 18 (GPIO24),
26 (GPIO7 / SPI0 CE1), **31 (GPIO6 — left free for the X1202)**, 32 (GPIO12), 33
(GPIO13), 35 (GPIO19), 38 (GPIO20), 40 (GPIO21).

### 4.1 Pin conflicts and naming hazards

- **GPIO6 is claimed by the X1202 UPS and this board leaves it alone.**
  `scripts/ups_shutdown_watch.py` (`UPS_PLD_GPIO=6`) holds it as the power-loss-
  detect input, and `docs/roadmap/mcp-and-power-hardening.md` records that two
  processes claiming that line via `lgpio` collide with EBUSY. The barometer INT
  was drafted onto GPIO6 and was moved to **GPIO17 (pin 11)** on 2026-08-10.
  GPIO17 is a plain GPIO with no alternate function, and it is also the closest
  free header pin to U5 — 10.5mm from the INT pad against 31.4mm for pin 31, so
  the fix shortened the route as well. GPIO7 (CE1) and GPIO12/13 (`uart4`) were
  deliberately not used, to keep a second CAN controller and a spare UART
  available.
- **PPS is on header pin 12 = GPIO18** (corrected 2026-08-10; it previously landed
  on pin 18, i.e. GPIO24). GPIO18 is the `pps-gpio` overlay's default, so the
  overlay needs no `gpiopin=` override.
- **The `TXD1`/`RXD1` suffixes on the GPIO4/5 labels are Pi 4-era names.** On a Pi
  5 (RP1), GPIO4/5 is **`uart2`**. The pins are right; the labels will mislead
  whoever writes the overlay.
- **GPIO0/GPIO1 are the ID bus** and may carry nothing else — this also rules out
  Pi 5 `uart1`, which shares those pins.
- **GPIO14/15 (`uart0`) are consumed by K-line.** The serial console must be
  disabled on `uart0` *and* on the GNSS UART.
- **GPIO7 (pin 26, SPI0 CE1) is free**, as is its neighbourhood — the reservation
  for a possible second CAN controller survives (§10), but no footprint was left
  for one.

## 5. Interfaces Summary

| Signal Bus | Connected To | Protocol |
|---|---|---|
| SPI0 (CE0) | MCP2518FDT-E/SL | Up to 20 MHz |
| CAN | MCP2518FDT-E/SL → TCAN1042VDRQ1 → J2 (no on-board termination) | ISO 11898-1:2015 / ISO 11898-2:2016, CAN 2.0B / CAN FD up to 2 Mbps |
| K-Line / LIN (`uart0`) | TLIN1027DRQ1 | ISO 9141 (K-line), LIN 2.x — 20 kbps TX / 100 kbps RX, no dominant timeout |
| I2C1 | BMP384 (**0x77**), LSM6DSOX (**0x6B**) | Standard/fast mode |
| I2C (dedicated ID bus) | CAT24C256 (0x50) | Standard I2C, 100 kHz |
| UART (`uart2`, GPIO4/5) + time pulse | MAX-M10M-00B | NMEA / UBX |
| Power in | Vehicle 12V → D4/D1/C2/C3 → `+12V` → TLIN1027 VSUP + J3 pass-through | — |
| Power in | Pi header → `+3V3` (all logic), `+5V` (CAN transceiver VDD) | — |

**I2C1 addressing.** The X1202 fuel gauge sits at 0x36 on bus 1 and does not
collide with 0x77 or 0x6B.

**I2C pull-ups are doubled.** R7 and R9 (4.7 kΩ to +3V3) sit on SCL and SDA — on
top of the Pi's own 1.8 kΩ pull-ups on GPIO2/3, giving ~1.3 kΩ effective and
~2.5 mA sink per line at VOL. Not fatal, but it is more than any device on the
bus needs, and earlier revisions of this document explicitly said not to add
them. Depopulating R7/R9 is a zero-risk change (§8.3). **Decided for rev 2.0:
footprints kept, shipped DNP** — measurement 6 was meant to settle this against a
physical board, and there is no longer one to measure first (§13.2.2).

## 8. Open Items

### 8.1 Defects found and fixed (schematic clean as of 2026-08-10)

**No known defects remain.** Every issue below was read out of the netlist rather
than inferred from the drawing, and each is verified fixed in the current
netlist. Kept as a log because several were non-obvious and the reasoning is
worth having when someone next reads the schematic.

1. **GNSS time pulse on the wrong header pin.** TIMEPULSE landed on pin 18 =
   GPIO24 while the net was labelled `GPIO18`; every other net on the board was
   correct by physical pin. Now on pin 12 = GPIO18, the `pps-gpio` default, so the
   overlay needs no `gpiopin=` override.
2. **R4 pulled the TLIN1027 EN pin up to +12V.** An abs-max violation of the ±6V
   `VLOGIC` rating, which also inverted the intended default — the transceiver was
   permanently enabled rather than defaulting to low-power — and pushed ~1.8 mA
   through the Pi's GPIO26 clamp into a rail that is down in the HAT+ STANDBY
   state. Now a pull-down to GND, backing the part's internal pull-down (§3.3.2).
3. **The commander termination was wired in series with the K-line bus**
   (`J2 pin 4 → R5 → D5 → LIN`), which broke the path in *both* directions: the
   driver could not sink the bus dominant through R5's 1 kΩ against the vehicle's
   own pull-up, and D5 blocked the LIN pin from discharging when the ECU pulled
   dominant, leaving the internal 45 kΩ termination holding it recessive forever.
   Now `+12V → R5 → D5 → LIN` with the K-line connector pin direct to the LIN pin,
   per §11.1 Pin 6 and Figure 8-1. *(That connector pin was **J2 pin 4** when this
   defect was fixed; rev 2.0 moved the K-line to **J2 pin 6** — see §3.9 for the
   current pin map, which is what a harness must be built to.)* D5 also changed from an SS54 Schottky to a 1N4148W, because the
   diode's job is blocking reverse current into an unpowered VSUP and Schottky
   leakage undermines exactly that (§3.3).
4. **R8 added** (4.7 kΩ, TXD to +3V3, Pi side of R6). Not a defect in the original
   drawing so much as a missing guard: TXD is internally pulled to ground, so with
   EN already raised, a collector that dies or releases the tty would leave the
   vehicle's K-line held dominant. §3.3.2 has the placement and value reasoning.
5. **BMP384 interrupt on GPIO6**, which `ups_shutdown_watch.py` holds as the
   X1202's power-loss-detect input — two `lgpio` claimants collide with EBUSY.
   Moved to **GPIO17** (§4.1).

**A sixth, found later and of a different kind (2026-08-13).** The five above are
netlist defects, all fixed before submission, and the heading above still holds
for the schematic. This one lived in the **BOM**, which is why no schematic
review could have caught it: **C2 was specified as 100µF/50V into a Ø6.3mm /
2.5mm-pitch footprint, and that part does not exist** — the smallest standard can
for 100µF at 50V is 8 × 11.5mm on a 3.5mm pitch. Corrected to **47µF** in §3.7.1,
which also records why the capacitance is not load-bearing here and why the
obvious workaround (25V, which does fit) fails on the first load dump.

It survived every review pass and reached a fab quote before anyone noticed,
because **both halves were correct in isolation** — 100µF is a sensible bulk
value, 6.3mm is a sensible can — and the contradiction only appears when someone
tries to buy the part.

The general form is worth keeping, because it is the same shape as the
dielectrics gap (§3.1.1): **a value and its footprint are one constraint recorded
in two places, with nothing checking them against each other.** §12 verifies the
schematic against the PCB and the netlist against both; nothing verified BOM
values against the footprints they land in. §12.5 now carries that check.

### 8.2 Decisions taken (2026-08-11)

Recorded so they are positions rather than omissions, and so nobody re-opens them
at fab time.

- **The X1202 leg gets no protection on this board.** J3 taps `+12V_RAW` ahead of
  D4 (§3.7.4). The X1202 already runs from a cigarette-lighter adapter in this
  vehicle and is fine there — electrically, an accessory socket and OBD pin 16 are
  the same environment, both fused in the 10–20A range off the same battery. Adding
  a series element and TVS purely because the feed now arrives via OBD would be
  protecting against nothing new. *Residual difference worth knowing:* a cigarette
  plug is keyed, whereas J2 is a hand-wired terminal block, so reverse polarity is
  possible here in a way it is not with the adapter — D4 protects the HAT, and the
  X1202 would take it.
- **No fuse on the board.** The harness-side/vehicle fuse is the whole story.
- **No CAN bus protection beyond the transceiver.** The TCAN1042's own ±58V bus
  fault rating and ±15kV IEC ESD are the defence (§3.2). Defensible for a device
  that lives permanently inside a vehicle harness.
- **U6 lands stay as imported** (0.3048 × 0.5334mm, ~19% under TN0018 — §12.4).
  DRC is clean via the scoped rule; the undersized land is an accepted
  solder-joint reliability position, not an outstanding error.

**Two of these are reversed in rev 2.0**, and deliberately so rather than by
drift: the **no-fuse** decision is re-taken (§3.7.4 contradicted §3.7.1, and the
second statement was written after the first), and the **U6 lands** are corrected
to TN0018. Both were positions taken to avoid disturbing a locked board; rev 1.0
is no longer locked, so the reason for accepting them is gone. The X1202-leg
decision is *also* revisited, but by being fixed rather than reversed — §13.2.1's
P-FET protects that leg without the drop that made the original trade
necessary.

### 8.3 Confirm before fabrication

> **None of this was ever confirmed against a board.** Rev 1.0 was withdrawn from
> PCBWay before fabrication (2026-08-22), so this list transfers wholesale to
> **rev 2.0** and is part of that submission's checklist, not history.
>
> Several items also appear in §13.2 because rev 2.0 acts on them rather than
> merely confirming them — the `KLINE_RAW` rename, R7/R9, C4's placement, the
> antenna bias feed, and the BMP384 noise question.

- **Fit 27pF for C5/C6**, not the 22pF currently drawn — the ABLS2's standard CL
  is 18pF (§3.1.1). The frequency effect is negligible; do it because it is
  correct and free.
- **Do not lose the `F` in `ABLS2-40.000MHZ-D4YF-T`** — at 40 MHz the ABLS2
  standard product is 3rd overtone, which will not run in the MCP2518FD's Pierce
  oscillator (§3.1.1).
- **Rename the `KLINE_RAW` net to `KLINE`.** On this board `_RAW` means "upstream
  of protection" (`+12V_RAW`), but this net *is* the protected, terminated bus at
  the transceiver pin — the name implies a filtered version downstream that does
  not exist.
- ~~**C4 placement**~~ — **closed 2026-08-24: leave it where it is.** *(The
  coordinates this item quoted were stale; C4 is at (137.25, 65.50), 11.4mm from
  U2's LIN pad and 27.5mm from the connector — already on the transceiver side,
  which is where TI's layout example puts it.)* Two facts make the move
  unnecessary. **The transceiver does not need protecting**: the TLIN1027 is rated
  for ISO 7637-2 Pulse 1 at −100V and Pulse 2 at +75V on LIN, ±45V absolute max,
  so C4 is not what saves it. And **C4 is a modest filter wherever it sits** —
  220pF is ~10Ω at 70 MHz, roughly where a 5 ns ISO 7637 edge has its content,
  against that pulse's 10Ω source impedance: about 6 dB, the same at either end.

  Two things that *do* matter, for the layout pass: **C4's GND pad needs its own
  via to In1.Cu placed immediately beside it** — *not* via-in-pad, which means
  filled-and-capped vias and a cost adder no fab quotes cheaply; a via adjacent to
  the pad with the shortest stub the annular ring and clearance allow is what is
  wanted. Shunt-path inductance dominates C4's high-frequency behaviour far more
  than its position along the net, and landing on the F.Cu pour is not the same
  thing (§2). And **keep the K-line route away from the CAN pair**.
  The K-line now runs from J2 pin 6 while CAN runs from pins 4/5 through FL1; not
  running them alongside each other addresses the coupling concern directly, which
  moving C4 would only have addressed indirectly.
- **Depopulate R7/R9** (duplicate I2C pull-ups, §5) unless there is a measured
  reason to keep them.
- ~~**V_BCKP fallback**~~ — **closed 2026-08-24: fit the cell, treat replacement
  as maintenance, add no hardware.** Which figure applies depends on an item still
  open below — whether the harness lands on always-hot OBD pin 16:

  | Pi state | I_V_BCKP | CR2032, 225 mAh |
  |---|---|---|
  | running | ~3 µA | **~8.6 years** — shelf-life limited, not load limited |
  | off (hardware backup) | 32 µA | **~9.6 months** |

  Rev 2.0 has no sleep architecture, so the Pi is on whenever it has power. If the
  feed is switched you are at ~10 months; if it is always-hot the Pi never sleeps
  and battery drain (~3 days, §13.3.1) is the larger problem by far.

  **A diode-OR from `+3V3` was considered and rejected.** It needs a diode in
  *each* leg, and after two drops the `+3V3` path (~3.0V through a Schottky)
  barely beats a fresh cell at 3.0–3.3V — so the cell keeps discharging and the
  parts buy nothing. It also creates exactly the cross-domain path §3.7.5 designed
  out. §13.3.4 has the right version for rev 3.0: OR it from the always-on rail,
  where the source is genuinely higher than the cell.

  **One free mitigation, software only.** Put the nav-rate configuration in the
  M10's **OTP** (74 bytes, permanent, applied at every startup) rather than BBR. A
  dead cell then costs only hot-start ephemeris, not configuration — removing the
  worst consequence of the cell running out.
  **Now costed, 2026-08-22 from `MAX-M10M_DataSheet_UBX-22028884.pdf`:** hardware
  backup mode (VCC = V_IO = 0, i.e. the Pi off) draws **32 µA typ**, against the
  "~20 µA" this document has been assuming; with the Pi up it is ~3 µA. A CR2032
  at 225 mAh and 32 µA is roughly **10 months**, so "primary cell always fitted"
  is a maintenance schedule, not a decision — and it only bites once rev 3.0
  actually leaves the Pi off, which is exactly when it will. This strengthens
  §13.3.4's case for diode-ORing the always-on rail into V_BCKP instead. Abs max
  on that pin is **3.6 V**, which constrains the OR leg.
- **Antenna bias protection** on the L1 feed (§3.6) — **still open, and the
  original wording was right.** A provisional note on 2026-08-22 guessed from the
  datasheet's feature list that the M10M's built-in "antenna supervisor with short
  detection" might make this free. **The Integration Manual (§3.3.1) refutes
  that**: the supervisor is a *host-side circuit*, not an internal function. Both
  variants need external parts — three-pin (open **and** short detection) or
  two-pin (short only) — and each requires T1/T2 switch MOSFETs, a comparator, a
  threshold divider, a pull-up, an `R8` current-limit/shunt resistor, plus
  open-drain level shifters when V_ANT ≠ V_IO. This board has none of them, and
  `LNA_EN` — which u-blox has already assigned to `ANT_OFF_N` — is unconnected.
  So the protection genuinely does not exist.

  Two details for whoever implements it. u-blox's reference value is **R8 = 10 Ω,
  5%, 0.25 W**, and the manual requires "both R8 and L3 [to] have sufficient
  current and power rating to withstand the short-circuit current". But **R8 alone
  is not a fix**: 3.3 V across 10 Ω is ~330 mA against `ICC_RF`'s 100 mA max
  source rating, so the resistor is the supervisor's *sense shunt*, not its
  protection — the FET switch is what actually cuts the supply. Fitting R8 without
  the rest would look like protection and provide none.

  *Logged as a retraction rather than edited away: the guess was made from a
  feature-list bullet in a product datasheet, with the document that defines the
  feature explicitly named as missing. That is the same shape as the dielectrics
  retraction in §13.4.1 — a confident reading of a partial source.*

  **Decided 2026-08-22: the supervisor goes to rev 3.0, and rev 2.0 accepts the
  exposure** — the same position rev 1.0 took, but now taken knowingly rather
  than by omission. What is being accepted is specific: a coax that shorts centre
  to shield drives `VCC_RF` past an **absolute maximum** rating, and u-blox states
  the product "is not protected against overvoltage or reversed voltages", so the
  failure mode is a dead U7 rather than a degraded fix. U7 is machine-placed, sits
  in the RF launch region, and is the hardest part on the board to rework.
  Mitigation until rev 3.0 is procedural: route the antenna coax so it cannot be
  pinched or chafed, and treat a sudden total loss of GNSS as "check the cable
  before suspecting the module".
- ~~**TCAN1042 VIO undervoltage behaviour** with VDD present and VIO absent~~ —
  **closed 2026-08-24 from the datasheet; the part handles it by design.** Table
  9-2 covers exactly this row — VCC above UV_VCC, VIO below UV_VIO, which *is*
  HAT+ STANDBY here since VDD comes from the Pi's +5V and VIO from +3V3:

  > **Protected mode · bus output High Impedance · RXD High Impedance**

  Precisely what STANDBY conformance requires: it cannot disturb the vehicle bus,
  and it cannot drive into the Pi's dead 3.3V domain. Recovery is "typically
  resumes normal operation within 50 µs" once supplies return. UV_VIO is
  1.3–2.75V with 80 mV hysteresis. **The row carries no STB qualifier**, so rev
  2.0 moving STB to GPIO22 does not change it.

  **This is what the `V` suffix buys, and it belongs on the do-not-substitute
  list.** `TCAN1042VDRQ1` lists "undervoltage protection on VCC and VIO (V
  variants only)" — non-V parts have no VIO at all. The variant choice is what
  makes §3.7.5's STANDBY argument true.
- ~~**Star-point placement** for the signal/chassis ground bond across J2 pins 2
  and 3~~ — **closed 2026-08-24 (§3.9).** No layout change: In1.Cu is a single
  unbroken plane and slotting it to make a star point would be worse than the
  ≈2.5 mV of IR drop it would prevent. The real issue was off-board — J3 carries
  the Pi's 3A, and OBD pin 5 is the ECM's own analog reference. **Land OBD pin 4
  (chassis) on J2 pin 3 and leave pin 5 unconnected.** Both pads stay, so it is a
  reversible harness choice.
- **RF stackup** — the 0.2mm feed assumes 0.1mm to Gnd.Cu (§3.6, §7).
- ~~**Verify no I2C clock stretching and whole-device WP** on the CAT24C256~~ —
  **closed 2026-08-22 from `CAT24C256-D.PDF`.** Both halves confirmed. **There is
  no clock stretching**: the part signals write-busy by *acknowledge polling* —
  it NAKs its own slave address for the duration of `tWR` (5 ms) — and stretching
  appears nowhere in the datasheet. That is the answer that mattered, because the
  Pi's HAT ID EEPROM bus is bit-banged by firmware at boot and does not handle a
  stretched clock. **WP is whole-device**: "With the WP pin held HIGH, the entire
  memory is protected against Write operations", and floating or grounded WP "has
  no impact", which is the writable state HAT+ ID programming needs. Note 6 also
  confirms the strap design: "when not driven, the WP, A0, A1, A2 pins are pulled
  down to GND internally", and the pull-down is deliberately strong — so the
  floating straps give 0x50 by construction, and the do-not-substitute note
  against Microchip's 24LC256 is justified rather than defensive.
- **Quiescent draw on +12V** with the vehicle parked, if the harness ever lands on
  always-hot OBD pin 16.
- **BMP384 RMS noise** against the grade budget; the BMP390 is footprint-
  compatible if it falls short (§3.5).
- **TLIN1027 20 kbps transmit ceiling** against the intended CONSULT baud rate.

### 8.4 Deferred / not in this revision

Recorded so they are decisions rather than discoveries. **Three of the five are
retired by rev 2.0** (§13.2): status LEDs and test points are added, the
common-mode choke gets a footprint bypassed by 0Ω jumpers alongside the DNI split
termination, and the MAX-M10S question is forced early because rev 2.0 is now the
first board — it is the one blind item on that list that is not free. The single
CAN channel and the missing L-line way stay deferred.

- **GNSS front end.** The MAX-M10M has no SAW/LNA and sits directly above a Pi 5
  running a WiFi hotspot (`docs/pi-hotspot.md`) — a real desense risk at 1.575
  GHz. The board relies entirely on the active antenna's integrated filtering.
  Moving to the MAX-M10S (integrated SAW + LNA) remains the robust option.
- **No status LEDs and no test points.** In-vehicle debugging still costs an SSH
  round trip.
- **Single CAN channel.** GPIO7/CE1 is free but no second-controller footprint was
  left. Vehicles with HS-CAN and MS-CAN on separate OBD pins need both.
- **No common-mode choke footprint** on CANH/CANL.
- **No L-line** way on J2 (§3.9).

## 9. What This Board Unlocks for CANary

The K-line interface is the reason to build this board. Two long-standing
blockers close with it:

- **Commanded AFR and ignition timing are not on the CAN bus**
  (`docs/roadmap/engine-tuning-support.md`, `data/bindings.yaml`), and Mode 01 /
  Mode 22 exclusivity means a single CAN tuning capture cannot produce them.
  On-board K-line puts spark and commanded AFR on the *same host clock* as the
  CAN capture.
- It retires the laptop-side UpRev import path (`tools/import_uprev_log.py`) and
  the cross-log clock alignment it requires (`src/analysis/clock_align.py`) for
  anything the Pi can poll itself.

The IMU, barometer, and GNSS consolidate hardware the roadmap already committed
to (`docs/roadmap/imu-gps-integration.md` Phase 2b, `docs/sensor-committees.md`)
and unblock `derived:GRADE` plus four of the six declared-but-dormant sensor
committees.

## 10. Capability Delta vs the Current Rig

| | Current (Waveshare 2-CH + X1202 + USB GPS) | CANary-HAT |
|---|---|---|
| CAN channels | 2 | 1 (CE1 free, no footprint) |
| CAN FD | No (MCP2515) | Yes |
| K-line | Laptop only (UpRev) | On-board (TLIN1027, commander-terminated) |
| GNSS | USB dongle, 1 Hz class | On-board M10, 10 Hz capable, SMA + active-antenna bias, backup cell |
| Time pulse | None | TIMEPULSE on GPIO18 (`pps-gpio` default) |
| IMU / baro | None | LSM6DSOX (0x6B) + BMP384 (0x77) |
| Power sense | X1202 PLD + fuel gauge | Unchanged (X1202 retained) |
| 3.3V domain | Pi rail | Pi rail (no on-board regulator) |
| Wiring | Multiple pigtails | Single 6-way terminal block + 2-way 12V pass-through |

## 11. Software Work Implied

None of this is blocking for fabrication, but none of it exists yet either.

- **Overlay delivery:** write a single board overlay covering `mcp251xfd`, the
  GNSS UART, and `pps-gpio`, and name it in the HAT+ ID EEPROM so firmware loads
  it at boot (§3.8.1). The 256 kbit EEPROM has room for it. That beats
  hand-edited `config.txt` entries, which `scripts/install_pi.sh` would otherwise
  have to re-apply on every fresh image.
- **CAN:** swap the `mcp2515` overlay for
  `dtoverlay=mcp251xfd,spi0-0,interrupt=25,oscillator=40000000`; `hardware.can`
  config (`interface: socketcan`, `channel: can0`) is unchanged.
- **CAN transceiver standby — new in rev 2.0, and it will look like a hardware
  fault if missed.** The TCAN1042's STB pin moves from a hard GND tie to a GPIO
  (§13.2.1), and **the pin has an internal pull-up that forces standby whenever it
  floats.** A Pi GPIO is high-Z at boot, so the board comes up with the
  transceiver in standby and **`can0` silently receives nothing** — link up, no
  errors, no frames. Something must drive STB **low** before CAN is expected to
  work, and it must do so on every boot.

  The failure mode is what makes this worth writing down rather than leaving to
  bring-up: it presents as "the CAN hardware is dead", which is exactly the
  conclusion §13.1's bring-up would otherwise reach about a board whose CAN path
  is fine. Note also that low-power-by-default is the *desired* behaviour —
  §13.3.1 depends on it — so this is a driver requirement, not a pin to re-tie.

  Cheapest correct implementation is the device tree, not the daemon: a
  `gpio-hog` on the STB pin in the board overlay (§11, first bullet) asserts it at
  boot before any userspace exists, and survives a daemon crash. Doing it in
  Python instead means CAN is dead for the whole boot window and dies again
  whenever the process does. If it *is* done in software, it belongs with the
  overlay work, not in `can_collector.py` — the collector should be able to assume
  a working `can0`.

  Wiring detail for whoever writes it: STB is on **GPIO22 (header pin 15)**, and
  the LSM6DSOX's INT2 lands on **GPIO23 (pin 16)**. **GPIO6 is not available**
  despite reading as free on this header — `ups_shutdown_watch.py` claims it as
  the X1202's power-loss-detect input, the same collision that moved the BMP384
  interrupt to GPIO17 in §8.1.
- **IMU:** add `lsm6dsox` to the `ImuConfig.source` Literal in `src/config.py`
  (currently `bno055 | mpu6050 | icm20948 | disabled`) and write
  `imu_collector.py` (roadmap task 2b.2) against I2C address **0x6B** with INT1 on
  **GPIO16**. The frame-resolution math in `imu_frame.py` already covers the
  raw-chip case. **Rev 2.0 also brings INT2 out to GPIO23** (pin 9 was unconnected
  on rev 1.0), which is what wake-on-motion and §13.3.3(d)'s parked monitoring
  need. Nothing has to use it yet — it is landed now because a missing net is a
  re-spin (§13.2.3).
- **Barometer:** no config, collector, or channel exists — only the
  `trainable=False` committee declarations from task 2a.8. Address **0x77**;
  interrupt on **GPIO17** (§4.1).
- **GNSS:** works as-is over a tty (`gps_collector.py` / gpsd) on **`uart2`
  (GPIO4/5)** — enable it and disable the console on it. The receiver needs its
  nav rate raised from the default. PPS is on **GPIO18**, so a bare
  `dtoverlay=pps-gpio` suffices, plus the chrony refclock change in
  `docs/time-sync.md`.
- **K-line:** entirely new — a source in the telemetry Literal, a
  `hardware.kline` config block (note `HardwareConfig` still carries a required
  `elm327_port`, which this is not), and a CONSULT/KWP collector on **`uart0`**.
  The hardware imposes a specific startup order (§3.3.2):
  1. **Disable the serial console on `uart0`.** Non-optional: a held-low RX is a
     serial break, and with magic SysRq on a console that can kill or reboot the
     host. Set GPIO15's bias explicitly while you are there.
  2. **Open the tty before raising EN on GPIO26**, never the reverse — R8 makes
     the released TXD state recessive, but only once the host is driving.
  3. **Blank RXD for ~50 µs after raising EN** (`tMODE_CHANGE` 2–15 µs +
     `tNOMINT` 35 µs), and 1.5 ms (`tPWR`) after power-up.
  4. **Expect a break, not idle, on first contact.** Power-up with EN low lands
     in *standby*, where RXD is driven low; the clean idle state needs one EN
     high-then-low cycle to reach sleep.
  5. **Do not read RXD-low as one thing** — dominant bit, bus wake-up request, or
     VSUP undervoltage, depending on commanded mode.
  6. **Drop EN before releasing the tty**, and on collector shutdown/crash paths.
  7. **Strip the local echo** — single-wire, so everything transmitted returns.
- **Shutdown on engine-off:** the software half of §3.7.6. Decision logic,
  Landed 2026-08-11: `src/daemon/idle_shutdown.py` (pure decider +
  `IdleShutdownMonitor`), `IdleShutdownConfig`, wired into the
  `run_live_inference` tick via `DaemonComponents.idle_shutdown`, with
  `tests/test_idle_shutdown.py` (35 tests). **Ships disabled**
  (`collection.idle_shutdown.enabled: false`); `dry_run: true` logs the
  decision without acting, which is how to try it on a live rig.

  The design point worth carrying forward: the bench case looks *identical* to
  the parked case from `vehicle_state` alone. A Pi on USB-C at a desk with the
  X1202's barrel jack out reads `no_data` — exactly an idle state — so a naive
  timeout powers off the operator's bench rig. The discriminator is the X1202
  watchdog's `input_lost` heartbeat field, read via `src/dashboard/ups.py`
  (never GPIO6 directly — the watchdog owns that line and a second `lgpio`
  claimant collides with EBUSY). That makes the two shutdown paths mutually
  exclusive rather than redundant:

  | X1202 input | Other evidence | Who acts |
  |---|---|---|
  | present | bus quiet for hours | `idle_shutdown` (this) |
  | lost | battery discharging | `ups_shutdown_watch.py` |
  | lost | battery idle | **nobody** — bench USB-C |

  A second, independent guard requires that a live vehicle (`engine_running`
  or `ignition_only`) has been seen this uptime, so a rig booted at a desk can
  never arm even if the watchdog is not running to supply the power signal.

