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
> Board state: **rev A submitted to PCBWay 2026-08-11 and locked.** Schematic and
> PCB complete and fully routed, 0 unconnected items, 0 DRC violations, 0
> schematic-parity issues; ERC 0 errors / 5 benign warnings. No schematic defects
> remain (§8.1). Changes from here go to a future revision — see **§13**, which
> stages them as bring-up measurements (§13.1), small fixes and GPIO control
> (§13.2), and the sleep/supervisor architecture (§13.3).

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

## 3. Major Components

As-built reference designators are given for each block. Full designator list:
U1–U7, Y1, J1–J4, JP1, BT1, L1, D1/D2/D4/D5, R1–R9, C1–C16, MH1–MH4 — 48
footprints, with schematic and PCB in sync. There is no D3.

### 3.1 CAN FD Controller — U3

| Parameter | Value |
|---|---|
| Part number | MCP2518FDT-E/SL (MPN pinned on the symbol) |
| Manufacturer | Microchip Technology |
| Function | External CAN FD controller with SPI interface |
| Supply voltage (VDD) | 2.7V to 5.5V — **wired to +3V3 (Pi rail)**, decoupled by C7/C9 (100n) |
| Interface | SPI, up to 20 MHz clock speed |
| Arbitration bit rate | Up to 1 Mbps |
| Data bit rate | Up to 8 Mbps |
| Package | SOIC-14, tape-and-reel packing |
| Standards compliance | ISO 11898-1:2015, AEC-Q100 qualified |
| Active current | Max 20 mA at 5.5V, 40 MHz CAN clock |
| Sleep current | 15 µA typical |
| Support circuitry | **Y1 40 MHz crystal (ABLS2-40.000MHZ-D4YF-T, Abracon) with C5/C6 = 22pF** on OSC1/OSC2 |

TXCAN (pin 1) and RXCAN (pin 2) cross-connect directly to the TCAN1042's TXD and
RXD. CLKO/SOF (pin 3), nINT1/GPIO1 (pin 8), and nINT0/GPIO0/XSTBY (pin 9) are
left unconnected. nINT (pin 4) goes to GPIO25.

**The old "nRESET must not float" open item is void.** The 14-pin SOIC
MCP2518FD has no nRESET pin — the package exposes TXCAN, RXCAN, CLKO/SOF, nINT,
OSC2, OSC1, VSS, nINT1/GPIO1, nINT0/GPIO0/XSTBY, SCK, SDI, SDO, nCS, VDD and
nothing else. There is nothing to pull up.

#### 3.1.1 Crystal: resolved against both datasheets

Worked through the Abracon ABLS2 datasheet (rev. 11.15.2016) and MCP2518FD
DS20006027B on 2026-08-11.

**C5/C6 should be 27pF, not 22pF.** The part number `ABLS2-40.000MHZ-D4YF-T`
carries no load-capacitance field, and the ABLS2 ordering scheme treats an
omitted field as standard — **CL = 18pF**. The load caps follow from
C = 2·(CL − C_stray); with ~4pF of stray that is **28pF, so 27pF standard
value**. At 22pF the circuit presents CL ≈ 15pF to a crystal cut for 18pF.

**But the frequency consequence is negligible, and that is worth knowing before
anyone panics.** The pulling from a 3pF CL error is roughly **30ppm**, against
the MCP2518FD's `FOSC1,40M` requirement of **40 MHz ±0.5% — that is ±5000ppm**.
Even stacked with the crystal's own ±30ppm tolerance and ±30ppm stability, the
total is under 0.01% of a 0.5% budget. Fit 27pF because it is correct and free;
the secondary reason is drive level, since undersized load caps raise it toward
the ABLS2's 1000µW maximum.

**The part of this BOM line that actually matters is the `F`.** The ABLS2
frequency table makes 24.01–70.00 MHz **3rd overtone the *standard* product**, and
24.01–50.00 MHz fundamental only as an *option*. The `F` in `D4YF` is that option
("Fund. AT >24MHz"). The MCP2518FD's oscillator is a plain Pierce circuit with no
tank, so a 3rd-overtone crystal in this socket either fails to start or runs at
its ~13.3 MHz fundamental — and `mcp251xfd` would be told 40 MHz regardless, so
every bit time on the bus would be wrong by 3×. **One letter in the part number
prevents that.** Do not let it be dropped in BOM transcription.

Two other options in that part number are load-bearing:

- **`D` = −40°C to +85°C.** The ABLS2 *standard* temperature range is 0 to +70°C,
  which is not a vehicle-cabin part.
- **`4Y` = ±30ppm tolerance, ±30ppm stability.** Ample against ±0.5%.

Supporting numbers: ESR 40Ω max for 16–50 MHz fundamental, C0 ≤ 7pF, drive level
100µW typ / 1000µW max, against the MCP2518FD's specified transconductance of
2040–3060 µA/V for a 40 MHz crystal — comfortable margin. Oscillator
stabilisation is 3ms from power-on.

Two notes rather than problems: the ABLS2 is an HC49/US can, **11.4 × 4.7 ×
3.3mm**, which is physically large for a 65 × 56mm board where a 3.2 × 2.5mm SMD
crystal would do; and Abracon markets it as COTS commercial/industrial, not
AEC-Q200, unlike the AEC-qualified parts elsewhere on this board. Neither
matters for a single research rig.

Linux support is the mainline `mcp251xfd` driver, configured by device-tree
overlay — `dtoverlay=mcp251xfd,spi0-0,interrupt=25,oscillator=40000000`. The
driver expects a level-sensitive, active-low interrupt.

### 3.2 CAN FD Transceiver (Physical Layer) — U4

| Parameter | Value |
|---|---|
| Part number | TCAN1042VDRQ1 [cite:54][cite:57] |
| Manufacturer | Texas Instruments [cite:54] |
| Function | Fault-protected CAN transceiver with I/O level shifting, CAN FD capable [cite:54] |
| Supply voltage (VDD) | 4.5V to 5.5V — **wired to +5V (Pi header pins 2/4)**, decoupled by C8 (100n) [cite:57][cite:60] |
| I/O level-shifting voltage (VIO) | 3V to 5.5V — **wired to +3V3 (Pi rail)** [cite:54] |
| Data rate | Classic CAN and up to 2 Mbps CAN FD [cite:54] |
| Standards compliance | ISO 11898-2:2016, ISO 11898-5:2007, AEC-Q100 (Grade 1) [cite:54] |
| Protection | ESD up to ±15kV (IEC), bus fault protection ±58V, undervoltage protection on VDD/VIO, thermal shutdown [cite:54] |
| Package | SOIC-8 (footprint `D0008A_L`) [cite:54] |
| Loop delay | 110 ns typical [cite:54] |
| Support circuitry | CANH/CANL straight to J2 pins 5/6; **no on-board termination, no ESD array, no common-mode choke** |

**STB is resolved.** Pin 8 (STB) is tied directly to GND in the schematic, so the
transceiver is hard-wired into normal mode. The old open item is closed.

**Termination.** The board deliberately carries no 120Ω termination resistor. A
vehicle bus is already terminated at both ends by the vehicle's own nodes; a
third terminator is a fault. Bench use against an unterminated bus requires an
external terminator in the harness.

**Nothing else sits on the bus pins.** CANH and CANL run from the terminal block
to the transceiver with no series resistors, no bus TVS array, and no choke —
the transceiver's own ±58V fault rating and ±15kV IEC ESD rating is the entire
protection story on the CAN side. That is defensible for a device that lives
permanently inside a vehicle harness, but it is a deliberate position, not an
oversight to discover at bring-up (§8.2).

### 3.3 K-Line / LIN Transceiver — U2

| Parameter | Value |
|---|---|
| Part number | TLIN1027DRQ1 [cite:53][cite:56] |
| Manufacturer | Texas Instruments [cite:56] |
| Function | LIN physical layer transceiver, **without dominant state timeout**; also supports ISO 9141 K-line diagnostics [cite:53][cite:56] |
| Supply voltage (VSUP) | 4V to 36V — wired to **+12V** (downstream of D4/D1), 100nF (C3) at the rail [cite:56] |
| Input voltage threshold (VIH) | 2V to 5.5V (supports 2.5V/3.3V/5V logic) [cite:62] |
| Data rate | LIN **transmit up to 20 kbps**; LIN receive up to 100 kbps [cite:53][cite:56] |
| Standards compliance | LIN 2.0/2.1/2.2/2.2A, ISO/DIS 17987-4.2, SAE J2602, AEC-Q100 (Grade 1) [cite:53][cite:56] |
| Bus fault protection | ±45V [cite:56] |
| Package | SOIC-8 HV (footprint `D0008B_HV`) [cite:56] |

**As-built support network:**

| Ref | Value | Connection | Assessment |
|---|---|---|---|
| R3 | 4.7 kΩ | RXD (pin 1) → **+3V3** | ✅ correct — see §3.3.2 for why 4.7 kΩ specifically |
| R6 | 1 kΩ | GPIO14/TXD0 → TXD (pin 4), in series | ✅ series protection on the host drive line, per §11.1 Pin 4 |
| **R8** | 4.7 kΩ | GPIO14/TXD0 → **+3V3**, on the **Pi side** of R6 | ✅ added 2026-08-10 — holds TXD recessive when the host stops driving (§3.3.2) |
| R4 | 4.7 kΩ | **EN (pin 2) → GND** | ✅ corrected 2026-08-10 — external pull-down backing the internal one |
| C4 | 220 pF | LIN (pin 6) → GND | ✅ correct — Figure 9-1 shows 220pF on the commander node too |
| R5 + D5 | 1 kΩ + **1N4148W** | **+12V → R5 → D5 (anode→cathode) → LIN**, commander pull-up | ✅ corrected 2026-08-10 — see below |
| C3 | 100 nF | +12V → GND at the VSUP pin | ✅ required by §10, and now drawn on this sheet |
| — | — | NC pins 3 and 8 unconnected; GND pin 5 to GND | ✅ |

**Bus topology as built** — J2 pin 4 lands directly on the LIN pin, with the
commander termination across it, which is Figure 8-1:

```
+12V ──> R5 (1 kΩ) ──> D5 anode
                       D5 cathode ──┬── U2.6 (LIN)
                                    ├── C4 (220 pF) ── GND
                                    └── J2 pin 4  (vehicle K-line)
```

The pull-up is taken from `+12V` (post-D4) rather than `+12V_RAW` so it shares the
receiver's reference — the LIN thresholds are ratiometric (`VBUS_CNT` = 0.5 ×
VSUP). Two diodes sit in the pull-up path (D4 + D5), so recessive lands near
Vbat − 1.1V against VSUP ≈ Vbat − 0.4V; Figure 8-1 annotates exactly this drop,
and it stays far above the 0.6 × VSUP recessive threshold.

D5 is a **1N4148W, not a Schottky**. Its job is blocking reverse current from a
live bus into an unpowered VSUP (§8.3.4), and Schottky reverse leakage — tens to
hundreds of µA at 12V, worse hot — undermines exactly that. The silicon part
leaks <100 nA while carrying 12 mA forward.

#### 3.3.1 Datasheet-verified design notes (TI SLLSF58B, rev. May 2022)

Verified against the TLIN1027-Q1 datasheet, since the K-line path is the board's
highest-value new capability:

- **No dominant state timeout — this is the part's defining feature**, and it is
  the correct choice for K-line. Every other TI LIN transceiver enforces a TXD
  dominant timeout in the 10–20 ms range, which would abort an ISO 9141-2
  **5-baud slow init** (200 ms per bit, up to ~600 ms held dominant). The
  TLIN1027-Q1 imposes no such limit, so both 5-baud slow init and fast init are
  possible. The datasheet Features list states "Supports ISO 9141 (K-Line)"
  outright. *Caveat from TI's applications team: the family is specified as*
  compatible with *ISO 9141 systems, not* compliant with *the ISO 9141
  physical-layer levels and timings.*

  **Confirmed by counterexample, 2026-08-11.** The Macchina M2 — open hardware,
  sold for exactly this job — uses the **NXP TJA1021** on both its ISO9141/LIN
  channels, and NXP documents a TXD dominant timeout on that part: it "prevents
  the bus line from being driven to a permanent dominant state… if pin TXD is
  forced permanently LOW," plus a startup check that leaves the transmit path
  disabled if TXD is LOW on entry to Normal mode. So a well-regarded automotive
  interface board most likely *cannot* do 5-baud slow init, which is the
  clearest evidence available that this bullet is selecting on something real.
  The exact `t_dom` value was not obtained (NXP's datasheet URL 404s), so the
  margin is unconfirmed — only the feature's existence. Note the scope: at
  CONSULT's 9600 baud a byte is ~1 ms, far inside any plausible timeout, so this
  is a **cross-vehicle** capability the TLIN1027 preserves, not an Xterra
  requirement.
- **RXD is an open-drain output** and requires an external pull-up to the host
  I/O supply. The datasheet's "1 kΩ to 10 kΩ" range is qualified by a footnote to
  check `IOL` for the actual VIO — at 3.3V the real floor is ~2 kΩ, not 1 kΩ.
  R3's 4.7 kΩ is correct; §3.3.2 works the numbers.
- **Commander (master) mode needs external bus termination.** The internal
  termination is a **45 kΩ** pull-up (spec 20/45/60 kΩ) plus series diode to VSUP
  — that is *responder/slave* termination. A diagnostic tester is a commander
  node and requires an **external 1 kΩ pull-up plus series blocking diode from
  LIN to VSUP**, per the LIN spec and the datasheet's Figure 9-1. R5 + D5 provide
  it. §11.1 Pin 6 is explicit that the resistor and blocking diode go "between the
  LIN pin and the VSUP pin" — across the bus, not in series with it, which is how
  they were originally drawn.

  *Value note:* 1 kΩ is the LIN commander figure and what the datasheet calls
  for. ISO 9141-2 specifies ~510Ω on the **tester** side, and this board is a
  tester rather than a LIN master. The Xterra's ECU supplies its own pull-up so
  1 kΩ should be sufficient; if a real ECU shows sluggish rise, 510Ω is the
  ISO-correct value and doubles pull-up current to ~24 mA. That is a value
  change, not a layout change.
- **TXD is internally pulled to ground** (Table 5-1; `RTXD` = 125/350/800 kΩ), so
  the transceiver requests *dominant* whenever the host stops driving TXD high.
  Two independent guards now cover that, because neither covers every state on
  its own — see §3.3.2.
- **EN has an internal pull-down** (`REN` = 125/205/800 kΩ), and §8.3.6 states its
  purpose outright: to keep the device in low-power mode if EN floats. R4 backs
  it externally so the guarantee does not rest on the 800 kΩ corner.
- **Mode-transition blanking, with numbers** (§6.8). `tMODE_CHANGE` 2–15 µs;
  sleep/standby → normal additionally needs `tNOMINT` 35 µs, so allow **~50 µs
  after raising EN** before trusting RXD. From power-up, `tPWR` is 1.5 ms. The
  bus wake filter `tLINBUS` is 25–150 µs.
- **20 kbps transmit ceiling.** This covers ISO 9141/KWP2000 at 10.4 kbps and
  Nissan CONSULT at 9600 baud, with margin for 15625 baud. It does **not** cover
  any protocol needing >20 kbps *transmit* (receive is fine to 100 kbps).
- **Single-wire echo is unavoidable.** Everything transmitted appears on RXD. The
  collector must strip its own echo — a software requirement, not a hardware fix.

#### 3.3.2 The host interface: why EN, TXD and RXD are wired the way they are

Both logic pins default to the *unsafe* state on their own, so the resistor
choices here are load-bearing rather than cosmetic. Recorded because the values
are not obvious and the failure modes are silent.

**EN — R4 as a pull-down to GND (4.7 kΩ).** EN is the only thing standing between
"host not driving TXD" and "HAT holds the vehicle's K-line dominant," so it must
be low by default and it must be *reliably* low. The internal pull-down alone
would do the job at its 205 kΩ typical, but at the specified 800 kΩ corner just
2.5 µA of node leakage reaches `VIH` = 2V — not a margin worth trusting on an
uncoated board in a vehicle cabin. R4 makes it ~425 µA. It costs GPIO26 0.7 mA to
drive EN high, against a pin good for several mA.

*Rejected alternatives:* a pull-up to 3V3 is what §11.1 Pin 2 suggests, but only
*"if this feature is not used"* — the EN gate is used here. A series resistor for
fault-current limiting (also §11.1 Pin 2) buys nothing once R4 goes to GND, since
GPIO26 → EN is entirely internal to the HAT with no connector on the net.

**TXD — R6 1 kΩ series plus R8 4.7 kΩ pull-up on the Pi side.** R6 is the
datasheet's suggested input-current limiter and is otherwise invisible: at the
125 kΩ `RTXD` corner it costs 26 mV of high level, and its RC with the pin
capacitance is ~10 ns against a 96 µs bit.

R8 closes a gap EN cannot. EN low covers the Pi being off or still booting, but
**not** the case where the collector has already raised EN and then dies,
releases the tty, or lets the GPIO revert to an input — TXD then falls to its
internal pull-down and the bus is held dominant with the transmitter still
enabled. R8 makes the released state *recessive* instead.

The two placement/value decisions:

- **Pi side of R6, not the transceiver side.** On the Pi side, the host driver
  sits directly on the pull-up node and TXD simply follows it through the 1 kΩ /
  125 kΩ divider — driving low costs nothing (~0.1V at the pin against `VIL` =
  0.8V). On the transceiver side the pull-up fights R6 and lands around 0.4V,
  spending half the low-level budget for no gain.
- **4.7 kΩ, not 10–22 kΩ.** The sizing case is GPIO14 high-Z, where the pull-up
  must beat the *Pi's own* internal pull-down (tens of kΩ) in parallel with
  `RTXD`. At a pessimistic 30 kΩ host pull-down and the 125 kΩ `RTXD` corner,
  4.7 kΩ holds 2.76V — clear of `VIH` = 2V. 22 kΩ would land near 1.8V and fail.

R8 only works while the Pi's 3V3 is up; EN covers every state where it is not.
Neither guard is sufficient alone, which is why both are fitted.

**RXD — R3 4.7 kΩ to the Pi 3V3 rail.** Squeezed from both directions:

- *Lower bound:* `IOL` is 1.5 mA at VOL = 0.4V, and `VOL` ≤ 0.6V is quoted at
  `RPU` = 2.4 kΩ. 4.7 kΩ sinks 0.62 mA — 40% of the spec point — so VOL comes in
  below 0.6V against a host input threshold near 0.8V. Counter-intuitively, a
  *smaller* pull-up makes the low level worse, not better.
- *Upper bound:* GPIO15's default internal pull-down fights R3. At a pessimistic
  30 kΩ, 4.7 kΩ still holds 2.85V; 10 kΩ would give 2.48V.
- *Not a constraint:* rise time is 4.7 kΩ × ~20 pF ≈ 94 ns, 0.1% of a 96 µs bit.

Set GPIO15's bias explicitly in the overlay rather than relying on the default
pull-down being at the weak end.

**RXD idle level differs by mode** (Table 8-1), and this is the bring-up trap:

| Mode | RXD | What the host UART sees |
|---|---|---|
| Sleep | floating → R3 pulls high | clean idle |
| **Standby** | **driven low** | **continuous break** |
| Normal | LIN bus data | traffic |

Per §8.4.1 the device powers up with EN low into **standby**, not sleep — §8.4.2
makes sleep reachable only *from* normal mode. So the first state after plugging
into the vehicle is the break-y one, and it stays that way until the collector
raises EN once. Reaching the clean idle state takes a deliberate EN high-then-low
cycle.

**RXD low means three different things** — a dominant bit (normal), a bus wake-up
request (standby, §8.4.4.1), and a VSUP undervoltage indication (§9.2.2.2, which
drops the device to standby). The line alone cannot distinguish them; the
collector must track the mode it commanded and apply the ~50 µs blanking. A
sudden break during operation may mean the truck's voltage sagged, not that the
bus went quiet.

**No series resistor on RXD** (unlike TXD): its high level is bounded by the
pull-up rail, so there is no overvoltage path into the host. For the same reason
the STANDBY power case is clean — when the Pi's 3V3 is down, both ends of R3 sit
at 0 with no back-feed path, which is exactly what a board-local 3.3V rail would
have broken.

### 3.4 Inertial Measurement Unit (IMU) — U6

| Parameter | Value |
|---|---|
| Part number | LSM6DSOXTR |
| Manufacturer | STMicroelectronics |
| Function | 6-axis IMU (3-axis accelerometer + 3-axis gyroscope only — no onboard fusion, no magnetometer) |
| Supply | VDD (pin 8) and VDDIO (pin 5) both on **+3V3** |
| Interface | **I2C** — CS (pin 12) strapped to +3V3 selects I2C mode; SCL on pin 13, SDA on pin 14 |
| I2C address | **0x6B** — SDO/SA0 (pin 1) strapped to +3V3 |
| Aux bus | SDX (pin 2) and SCX (pin 3) tied to GND (sensor hub unused) |
| Interrupts | **INT1 (pin 4) → GPIO16**; INT2 (pin 9) unconnected |
| Accelerometer range | ±2/±4/±8/±16 g full scale |
| Gyroscope range | ±125/±250/±500/±1000/±2000 dps full scale |
| Supply current | 0.55 mA in combo high-performance mode |
| Package | LGA-14L, 2.5mm × 3.0mm × 0.83mm |

The LSM6DSOXTR was selected over a fusion-capable part such as the BNO085 to cut
sensor cost roughly 5-7×, deliberately dropping onboard sensor fusion and the
magnetometer entirely; orientation-fusion is instead handled in software running
on the Raspberry Pi 5 host.

**This is compatible with the existing CANary code path.**
`src/preprocessing/imu_frame.py` already implements the raw-chip route — a
gravity low-pass over quiescent windows, yaw-offset resolution from straight
segments, and longitudinal sign resolution against the OBD speed derivative —
with on-chip fusion (BNO055) handled as a `gravity_removed=True` parameter
rather than a separate code path. The absent magnetometer costs nothing: heading
already comes from GPS course, not from the IMU.

**Every outstanding DRC violation is under this part.** The imported LGA-14L
footprint has 0.1952mm pad-to-pad gaps against a 0.2mm default clearance; the
current run flags pads 1–2, 3–4, 9–10 and 12–13. This is a footprint-geometry
fact, not a routing error — fix it with a local clearance override on U6 (or a
footprint-level rule), not by moving copper.

### 3.5 Barometric Pressure Sensor — U5

| Parameter | Value |
|---|---|
| Part number | BMP384 [cite:52][cite:55] |
| Manufacturer | Bosch Sensortec [cite:55] |
| Function | Digital absolute pressure sensor for altitude tracking [cite:52] |
| Supply | VDD (pin 10) and VDDIO (pin 1) both on **+3V3** [cite:61][cite:64] |
| Interface | **I2C** — CSB (pin 6) strapped to +3V3 selects I2C; SCK (pin 2) = SCL, SDI (pin 4) = SDA [cite:61] |
| I2C address | **0x77** — SDO (pin 5) strapped to +3V3 |
| Interrupt | **INT (pin 7) → GPIO17** (header pin 11) — moved off GPIO6, see §4.1 |
| Pressure range | 300 hPa to 1250 hPa [cite:61] |
| Resolution | Up to 21-24 bit [cite:61][cite:64] |
| Relative accuracy | ±9 Pa (900-1100 hPa, 25-40°C) [cite:61] |
| Current draw | ~2 µA sleep, ~700 µA peak during measurement [cite:61][cite:64] |
| Package | 10-pin metal-lid LGA, 2.0mm × 2.0mm × 0.95mm [cite:55] |

**Range check:** the vehicle operates at ~80 kPa (800 hPa, ~2072 m), comfortably
inside the 300–1250 hPa range.

**Noise vs the roadmap's BMP390.** `docs/roadmap/imu-gps-integration.md`
specifies a BMP390 for the `derived:GRADE` channel. Grade is a *derivative* of
altitude, so what matters is short-term RMS noise and noise density, not the ±9
Pa relative-accuracy figure quoted above (BMP390: ±3 Pa). Confirm the BMP384's
RMS noise at the intended ODR/oversampling against a grade budget. Both parts are
2.0 × 2.0 mm 10-pin LGA — the footprint on the board is swap-compatible, so a
BMP390 can drop in if the 384 proves too coarse.

### 3.6 GNSS / GPS Module — U7

| Parameter | Value |
|---|---|
| Part number | **MAX-M10M-00B** [cite:35][cite:41] |
| Manufacturer | u-blox AG [cite:35] |
| Function | Standard precision GNSS/GPS receiver module [cite:35] |
| Supply | VCC (pin 8) and V_IO (pin 7) both on **+3V3**; VIO_SEL (pin 15) unconnected |
| Interface | **UART** — TXD (pin 2) → GPIO5, RXD (pin 3) → GPIO4 (Pi 5 `uart2`). DDC/I2C pins 16/17 unconnected [cite:34][cite:41] |
| Time pulse | TIMEPULSE (pin 4) → **header pin 12 = GPIO18**, the `pps-gpio` overlay default |
| Backup supply | V_BCKP (pin 6) ← D2 (BAT54) ← BT1 (2-pin JST-SH connector, external cell) |
| RF front end | RF_IN (pin 11) ← C16 (10nF DC block) ← J4 (SMA edge-mount, Molex 73251-1153); VCC_RF (pin 14) → L1 (27nH, LQG18HN27NJ00D) → antenna feed = **active-antenna bias** |
| Unused | EXTINT (5), RESET_N (9), LNA_EN (13), SAFEBOOT_N (18) all unconnected |
| GNSS support | Concurrent GPS, GLONASS, Galileo, BeiDou (default: GPS + Galileo with QZSS/SBAS) [cite:39] |
| Position accuracy | 1.5 m CEP (high performance mode) [cite:39] |
| Power | <25 mW continuous tracking; <10 mW in 1 Hz cyclic power-save [cite:35][cite:39] |
| Package | LCC, 9.7mm × 10.1mm × 2.5mm [cite:41] |
| Notes | Cost/power-optimised variant **without integrated LNA/SAW filter**; requires an external active antenna with integrated filtering [cite:39] |

**UART, not I2C — this is a hard requirement, not a preference.** CANary's GPS
path is `src/collection/gps_collector.py` (serial NMEA) or the gpsd path in
`docs/time-sync.md`. gpsd requires a tty; it has no u-blox DDC/I2C transport. The
as-built board honours this: DDC is unconnected and the module talks UART only.

**Nav rate.** The roadmap requires 10 Hz (1 Hz modules are "too slow to help
transients or grade"). The M10 meets this; the receiver must be configured off
its default rate at bring-up.

**Antenna bias network is now specified in hardware** (it was a gap in earlier
revisions): the module's own VCC_RF output feeds the antenna centre conductor
through L1 (27nH feed inductor), and C16 (10nF) DC-blocks the path into RF_IN.
There is **no bias fuse or short-circuit protection** on that feed — a shorted
antenna cable pulls VCC_RF down, which is the module's own supply. Consider a
small series resistor or resettable element if the antenna is field-swappable.

#### 3.6.1 The RF launch (reworked 2026-08-11)

The feed is 0.20mm on F.Cu, which computes to **~45Ω** over the 0.1mm
F.Cu→Gnd.Cu prepreg once 1oz copper thickness is included. Across the plausible
envelope — εr 4.0–4.6 and ±10% prepreg tolerance — it stays between **42 and
48Ω**, worst case 21dB return loss. The width is not worth retuning; 0.35mm
would give ~32Ω and 0.18mm ~48Ω, but prepreg tolerance moves it further than
either.

**The problem was the SMA pad, not the trace.** J4's signal pad is
**5.08 × 2.29mm** (X 159.64–164.72, Y 67.11–69.39 — note the footprint is rotated
180°). Over a 0.1mm dielectric that is a **7Ω** transmission line 5mm long, and
length is what makes a discontinuity bite: at 0.05λ it reflects hard.

| Reference under the pad | Z₀ | Return loss |
|---|---|---|
| In1.Cu intact (0.100mm) | 7Ω | **2.9 dB** |
| **In1.Cu voided → In2.Cu (1.375mm)** | **54Ω** | **25.3 dB** |
| Both inner layers voided → B.Cu | *nominally 57Ω* | *fiction — see below* |

**The fix, as built:** a notch in the In1.Cu zone outline spanning **X 159.65 →
165.50, Y 66.90 → 69.60** — the full pad with ~0.21mm margin top and bottom,
opening out to the board edge. It is a concave notch in the single zone outline,
not a separate keepout, which is the natural shape for an edge-mount connector.

**Void In1.Cu only.** Voiding both inner layers looks similar on paper but is
wrong: B.Cu carries no copper beneath that pad, so the pad would reference
nothing and land somewhere uncontrolled around 70–90Ω. And the obvious objection
to using In2.Cu — that it is a *power* plane, not ground — does not survive the
arithmetic: 65 × 56mm across a 1.24mm core is about **112pF of interplane
capacitance, 0.9Ω at 1.575 GHz**. In2.Cu is an RF ground whatever DC sits on it.

**Bias the void inward from the pad, never outward.** The trace needs its 0.1mm
ground reference right up to where it widens into the pad — void under the trace
and it jumps to ~150Ω. The pad has 5mm of length to spare; the trace has none.

**Stitching:** five GND vias now flank the launch at (156,72), (159,75), (159,61),
(164,75) and (164,61), bracketing the void band so the return current runs down
each side rather than around the hole. Before this there were none within 10mm,
and the only layer transitions were the connector's own two plated posts.

**Fab dependency:** all of the above assumes the **0.1mm outer prepreg**. If the
fabricator substitutes 0.2mm — an ordinary thing to do when unspecified — the
0.2mm trace becomes **~66Ω**. State the outer dielectric thickness in the fab
notes; that matters far more than εr, which the envelope above already absorbs.

**Backup supply has no fallback.** V_BCKP is fed *only* from BT1 through D2. If
the JST connector is empty the pin floats, and there is no path from +3V3, so the
receiver cold-starts every boot — the exact cost PPS exists to avoid. Note also
that a series Schottky from BT1 means **no charge path**: BT1 must be a primary
cell, not a supercap. u-blox expects V_BCKP to be supplied (commonly diode-OR'd
with the main rail) rather than left open.

### 3.7 Power Input, Protection, and System Power Topology

**There is no on-board regulator.** The AP63203Q buck of earlier revisions is not
in the schematic, not in the BOM, and has no footprint on the PCB. The board
takes 3.3V and 5V from the Pi header and 12V from the vehicle, and generates
nothing.

#### 3.7.1 12V input protection chain (sheet `OBD_IO`)

| Ref | Part | Role |
|---|---|---|
| J2 pin 1 | terminal block | Vehicle 12V in → net `+12V_RAW` |
| **J3** | 2-pos terminal block | Pass-through to the X1202, tapped off **`+12V_RAW`, upstream of D4** |
| D4 | SS54 (SMB, 5A/40V Schottky) | Series reverse-blocking, `+12V_RAW` → `+12V` |
| D1 | SMBJ24A (SMB, 600W TVS, 24V standoff) | `+12V` → GND transient clamp |
| C2 | 100µF radial THT (D6.3mm) | Bulk on `+12V` |
| C3 | 100nF 0603 | HF decoupling on `+12V`, at the TLIN1027 VSUP pin |

**The protection chain now guards only the HAT.** As of 2026-08-10 the
pass-through taps the raw connector node, so `+12V_RAW` fans out to J3 and to
D4's anode in parallel. Everything downstream of D4 — the TVS, the bulk cap, the
TLIN1027 — sees only the HAT's 12V load, which is tens of mA (VSUP quiescent,
R4, and the K-line pull-up when the bus is dominant). The ~150 mA whole-board
figure quoted elsewhere is almost entirely 3.3V draw and does not appear here.
See §3.7.4 for what that buys and what it costs.

**There is no PTC fuse.** Earlier revisions listed one in the protection chain;
the as-built board has none. Decide deliberately whether the vehicle-side fuse in
the harness is the whole story (§8.2 — decided: it is).

#### 3.7.2 Rails

| Rail | Source | Feeds |
|---|---|---|
| `+12V_RAW` | J2 pin 1, straight off the vehicle | D4's anode, **and J3 (pass-through to the X1202)** — unprotected by anything on this board |
| `+12V` | `+12V_RAW` via D4, clamped/bulked by D1/C2 | TLIN1027 VSUP (decoupled by C3), and the K-line commander pull-up R5+D5 |
| `+3V3` | **Raspberry Pi header pins 1 and 17** | Everything else: MCP2518FD VDD, TCAN1042 VIO, BMP384, LSM6DSOX, MAX-M10M VCC + V_IO, EEPROM, all pull-ups and straps |
| `+5V` | Raspberry Pi header pins 2 and 4 | TCAN1042VDRQ1 VDD only (~70 mA) |

The HAT does **not** generate 5V and does **not** power the Pi. One 3.3V domain,
sourced from the host — which is what §3.7.5 argued for.

#### 3.7.3 System topology

```
Vehicle OBD 12V ──> J2 (6-way block) ──> +12V_RAW ──┬──> D4 (SS54) ──> +12V ──┬──> D1 TVS / C2 / C3
                                                    │                        ├──> TLIN1027-Q1 VSUP
                                                    │                        └──> R5+D5 → K-line pull-up
                                                    │
                                                    └──> J3 (2-way block)   [unprotected tap]
                                                             │
                                                   barrel-jack pigtail
                                                             │
                                                             v
                                                    X1202 UPS HAT ──> Raspberry Pi 5 (5V)
                                                             │
                                                     (battery backup + GPIO6 PLD +
                                                      I2C fuel gauge at 0x36)

Raspberry Pi 5 header ──> +3V3 (pins 1,17) ──> every logic device on this board
                      └─> +5V  (pins 2,4)  ──> TCAN1042 VDD
```

The X1202 remains the sole power source for the Pi and the sole owner of
power-loss detection and battery-backed shutdown (`scripts/ups_shutdown_watch.py`,
`systemd/ups-shutdown.service`).

#### 3.7.4 The pass-through is an unprotected tap (changed 2026-08-10)

The 12V pass-through leg (J3) branches off `+12V_RAW` **upstream of D4** — it is
a parallel tap on the connector node, not a load on the protected rail. This is
the alternative earlier revisions named, and taking it resolves the thermal and
conductor problems it was weighed against:

- **D4 no longer carries the Pi.** It passes only the HAT's 12V load — TLIN1027
  VSUP plus the commander pull-up, tens of mA — so the SS54 dissipates milliwatts
  instead of ~1.5W, and the earlier "confirm the pad can sink 1.5W in a hot
  cabin" worry is void. The part is now generously oversized, which is fine.
- **No diode drop in the Pi's supply.** The X1202 sees the connector voltage
  directly, so cranking sag is not made worse by a series Schottky.
- **The copper was widened to match, and the widening is complete.** A Pi 5 at
  full tilt is ~25W, so budget on the order of **3A at 12V** worst case. The
  whole J2 → J3 path is **1.3mm on B.Cu, 37.3mm end to end, with no via in it**
  (both terminal blocks are through-hole, so their pads span all layers). 1.3mm
  of 1oz external copper is roughly a 2.9A-at-10°C-rise / 3.9A-at-20°C-rise
  conductor, which covers the budget with margin.

  The narrower copper on these nets is **not** in that path: the branch from the
  `+12V_RAW` node up to D4's anode (0.6mm, plus a ~1mm 0.4mm stub and one
  0.9/0.5mm via) and everything on `+12V` past D4 (0.6mm) carry only the HAT's
  own load — TLIN1027 VSUP and the K-line pull-ups, tens of mA. 0.4mm is already
  ~1.6A of headroom there.

**What it costs: nothing on this board protects the X1202 leg.** Reverse polarity
at the terminal block is the clearest case — D4 protects the HAT, and J3 passes
the reversed voltage straight through to the UPS. Transients are subtler: a
positive surge on `+12V_RAW` is clamped only indirectly, because D1 sits behind
D4 and any clamping current has to flow through the Schottky. Two things to
settle before this is fabricated — both since decided, see §8.2:

1. **Confirm the X1202 tolerates reverse polarity and automotive transients on
   its barrel jack**, or accept that a miswired terminal block kills the UPS and
   possibly the Pi.
2. **If it does not, move the protection rather than the tap** — a TVS and a
   series element (ideal-diode controller, or a Schottky sized for 3A) on
   `+12V_RAW` itself, ahead of the fan-out to both D4 and J3.

**There is still no fuse anywhere on the board**, so nothing on the HAT limits
fault current on a leg that now feeds the whole system directly.

#### 3.7.5 Why there is one 3.3V rail (retained rationale)

The reasoning that killed the buck, kept because it governs any future attempt to
re-add a local rail.

**Current was never the problem.** Itemised against datasheet figures, the whole
board is ~35 mA typical and ~90 mA peak on 3.3V (MCP2518FD ≤20 mA; TCAN1042 VIO a
few mA — its ~70 mA VDD sits on 5V; MAX-M10M ~8 mA tracking plus the active-
antenna LNA bias at 5–20 mA, easily the largest single item; LSM6DSOX 0.55 mA;
BMP384 0.7 mA peak; the 4.7 kΩ K-line RXD pull-up 0.7 mA when low; EEPROM µA).
Against that, **there is no official Pi 3V3 limit to be near** — the Raspberry Pi
HAT+ specification (RP-008281-DS) sets no maximum current for a HAT+ on the 3.3V
rail, and the widely-cited 500 mA is an unofficial rule of thumb dating to the
B+, not a spec. Even taken at face value, this board's peak is under a fifth of
it.

**A second rail was an active liability.** The HAT sits between a vehicle 12V
feed and a UPS-backed host, so the two supplies can each outlive the other. With
split rails, both directions are a partial-power-down case: a device is powered
while the domain driving its I/O is not, and current flows through the parts' ESD
clamps into the dead rail — which can also hang a shared I2C or SPI bus.

| State | Vehicle +12V | Pi 5V | Pi 3V3 | With one rail (as built) |
|---|---|---|---|---|
| Normal run | up | up | up | fine |
| **UPS battery run** (key off, X1202 carrying the Pi) | **down** | up | up | fine — only the K-line transceiver's VSUP drops, and its logic pins stay referenced to a live host |
| **STANDBY** (`sudo halt`, vehicle still connected) | **up** | up | **down** | HAT logic dies with the host, as HAT+ requires |

The STANDBY row is a HAT+ conformance requirement: *"HAT+ boards must be
electrically compatible with the STANDBY power state, where the 5V power rail is
powered, but the 3.3V rail is not powered,"* and *"HAT+ boards must not assume
any particular sequencing timing between the 5V and 3.3V rails."* The as-built
single-rail arrangement satisfies this by construction.

**Two residual cross-domain paths survive and must be checked:**

1. The TCAN1042's VDD stays on 5V while its VIO drops in STANDBY — that is
   exactly what its VIO undervoltage protection exists for, so confirm the
   behaviour in the datasheet rather than assuming it.
2. The TLIN1027's VSUP stays on vehicle 12V while the host 3V3 domain is down. Its
   EN and TXD then face an unpowered host — and R4 as drawn actively pulls EN up
   toward 12V in that state — which is why R4 was changed to a pull-down (§3.3.2).

#### 3.7.6 Consequences of having no power sense on this board

The CANary-HAT has no ignition sense, no 12V voltage sense, and no power-loss
detect of its own. Accepted consequence: **the rig is unplugged when not in use,
or** the daemon infers engine-off from bus activity and commands a
shutdown/suspend. The second path is software work that does not exist yet —
`vehicle_state` already classifies `engine_off` / `no_data` (see `AGENTS.md`), and
`RecordArmingConfig.disarm_after_seconds` already detects the quiet period, but
nothing currently escalates that to a system shutdown.

### 3.8 HAT ID EEPROM — U1

| Parameter | Value |
|---|---|
| Part number | **CAT24C256WI-GT3 (onsemi)** — 256 kbit, MPN pinned on the symbol |
| Function | Raspberry Pi HAT ID EEPROM |
| Interface | I2C, address **0x50** (A0/A1/A2 all left unconnected → internal pull-downs → 000) |
| GPIO connection | ID_SD (header pin 27) / ID_SC (header pin 28), isolated from I2C1 |
| Package | SOIC-8 |
| Supply | **+3V3 (Pi rail)**, decoupled by C1 (100n) |
| Pull-ups | R1, R2 = **3.9 kΩ** to +3V3 on ID_SDA / ID_SCL ✅ per HAT+ |
| Write protect | WP (pin 7) → **JP1, a normally-open solder jumper to +3V3** |

**Density changed: 24C32 → 24C256.** Both are 16-bit addressable, so the HAT+
rule that disqualified the 24C02 is satisfied either way; the larger part simply
leaves room for a device-tree overlay blob alongside the ID record.

#### 3.8.1 HAT+ conformance rules (RP-008281-DS)

- **16-bit addressing is mandatory** — *"Use 16-bit addressable EEPROM. Do not use
  EEPROMs with 8-bit addressing."* The CAT24C256 complies. The spec notes these
  restrictions "exclude many smaller I2C EEPROMs," so check the datasheet of
  whatever part is actually sourced.
- **WP guidance is a recommendation, not a requirement** — *"connect the EEPROM
  write protect pin to a test point on the board pulled-up to 3.3V with a 1KΩ
  resistor."* The board instead fits **JP1, an open solder jumper to +3V3**: the
  default state is *writable* (WP low via the part's internal pull-down), and
  bridging JP1 hard-ties WP high to protect the device. That is a defensible
  swap — it costs less and is unambiguous — but note the difference: a soldered
  jumper is not a probe-drivable test point, so a production programmer cannot
  temporarily unprotect a protected board.
- Also required: a 3.3V part (✅), a WP pin covering the **entire** memory
  (verify), **no I2C clock stretching** (verify), and no paged EEPROM where the
  low address bits select the page.
- Only an EEPROM and its 3.9 kΩ pull-ups may connect to ID_SD/ID_SC — nothing
  else, ever. ✅ as built.

#### 3.8.2 Floating-low WP is correct — but only for onsemi CAT24Cxx

With JP1 open, WP relies on the part's internal pull-down. From the onsemi
datasheet:

> **WP:** When pulled HIGH, the Write Protect input pin inhibits all write
> operations. **When left floating, this pin is pulled LOW internally.**

> *Note 5:* When not driven, the WP, A0, A1 and A2 pins are pulled down to GND
> internally. For improved noise immunity, the internal pull-down is relatively
> strong; therefore the external driver must be able to supply the pull-down
> current when attempting to drive the input HIGH.

The same note is why JP1 is a **hard short** rather than a resistor: `IWP` is
**120 µA at VCC = 3.3V**, so a weak pull-up would lose to the internal pull-down.
The same logic applies to A0/A1/A2, which are also left floating here to select
address 0x50 — defined behaviour on this part, undefined on many others.

**This is a BOM constraint, not a schematic one**, and it now covers *four* pins.
Microchip's 24AA256/24LC256, the natural second source, says the opposite:

> **2.4 Write-Protect (WP).** This pin must be connected to either VSS or VCC.

A drop-in substitution to that part turns four floating inputs from "defined low"
into out-of-spec floating CMOS inputs — a corruption and mis-addressing risk that
no schematic review would catch, because the schematic never changed. The MPN is
pinned on the symbol for exactly this reason; keep it pinned on the BOM line,
with the reason attached.

**Stacking.** HAT+ explicitly supports stacking: permitted EEPROM addresses take
the form `101_00XY`, where the `XY` suffix encodes the HAT class. Confirm the
X1202 presents no ID EEPROM (it predates HAT+ and almost certainly does not), and
keep 0x50 for this board.

**The EEPROM can carry the board's device-tree overlay name**, which firmware
loads at boot — the cleanest way to bring up `mcp251xfd`, the GNSS UART, and
`pps-gpio`. See §11.

**Not a Power HAT+.** This board sources no 5V to the Pi, so the Power HAT+ rules
(`hat_current_supply`, the MODE1 address that makes firmware set
`usb_max_current_enable=1`) do not apply.

### 3.9 Terminal Blocks and Connectors

| Ref | Part | Footprint | Function |
|---|---|---|---|
| J2 | 6-position screw terminal, 5.08mm | `TerminalBlock_Phoenix_MKDS-1,5-6-5.08_1x06_P5.08mm_Horizontal` | Vehicle side |
| J3 | 2-position screw terminal, 5.08mm | `TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal` | +12V pass-through to the X1202 |
| J1 | 2×20 2.54mm socket, **on B.Cu** | `PinSocket_2x20_P2.54mm_Vertical` | Pi 5 header |
| J4 | SMA edge-mount | `SMA_Molex_73251-1153_EdgeMount_Horizontal` | GNSS active antenna |
| BT1 | JST-SH 2-pin, 1.0mm | `JST_SH_SM02B-SRSS-TB_1x02-1MP_P1.00mm_Horizontal` | GNSS backup cell |
| JP1 | Solder jumper, normally open | `SolderJumper-2_P1.3mm_Open_Pad1.0x1.5mm` | EEPROM write protect |
| MH1–MH4 | Mounting holes | — | 58 × 49mm HAT pattern |

The footprint family is **Phoenix MKDS**, not the MC/MCV series earlier revisions
named. Both are 5.08mm pitch; the BOM must say which.

**J2 pin map, as wired:**

| J2 pin | Net | OBD-II pin |
|---|---|---|
| 1 | `+12V_RAW` | 16 (battery +12V) |
| 2 | GND | 5 (signal ground) |
| 3 | GND | 4 (chassis ground) |
| 4 | `KLINE_RAW` | 7 (K-line) |
| 5 | `CANH_RAW` | 6 (CAN High) |
| 6 | `CANL_RAW` | 14 (CAN Low) |

Note J2's *value* field still reads `Screw_Terminal_01x05` while the symbol and
footprint are 6-position — cosmetic, but it will propagate into a BOM.

**Signal ground and chassis ground are tied together on the board** — pins 2 and
3 are the same GND net, so the HAT is the bonding point between the two returns.
That is fine on a vehicle where they are already bonded at the battery. The
layout consequence: bring the 12V return in on one pin and reference the bus
receivers to the other, joining them at a **single star point** near the
connector rather than letting them merge across the whole Gnd.Cu pour. Otherwise
the HAT's power-return current — including the pass-through leg feeding the X1202
and therefore the entire Pi — shares copper with the CAN and K-line voltage
reference. **With a board-wide solid GND pour this is a placement/stitching
question, and it has not been explicitly resolved** (§8.3).

The block has no spare way for an ISO 9141 **L-line**. The dev vehicle does not
need one (Nissan CONSULT inits with a byte sequence, not an L-line pulse), but
some pre-CAN vehicles do.

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
them. Depopulating R7/R9 is a zero-risk change (§8.3).

## 6. KiCad Implementation — as built

- **Hierarchy: root plus four sub-sheets** — `Sensors`, `CANFD_Interface`,
  `KLine_Transceiver`, `OBD_IO`. The root sheet carries the 40-pin header (J1),
  the ID EEPROM block (U1, R1, R2, JP1, C1), and the sheet interconnect. After the
  K-line rework, `OBD_IO` holds only connectors and the 12V protection cluster
  (J2, J3, D4, D1, C2); the whole K-line network — U2, R3–R6, R8, C3, C4, D5 —
  now lives in `KLine_Transceiver`. Earlier
  revisions described six sheets including separate `Power`, `GPIO_Header`, and
  `ID_EEPROM` sheets; that structure is gone.
- **Two orphaned files remain in the project directory**: `Power.kicad_sch` and
  `Inputs.kicad_sch` are earlier revisions of what is now `OBD_IO.kicad_sch`.
  They are not instantiated by the root sheet and contribute nothing to the
  netlist, but they will confuse the next reader — delete them (housekeeping,
  not a defect).
- **Symbols and footprints** for TLIN1027DRQ1, TCAN1042VDRQ1, BMP384, LSM6DSOX,
  MAX-M10M-00B, MCP2518FDT-E/SL, and BAT54 are imported from SnapEDA/
  UltraLibrarian into date-stamped project libraries (`2026-08-09_*`,
  `2026-08-11_*`) with footprints in a project-local `footprints` library.
  Passives, connectors, and the EEPROM come from the KiCad standard libraries.
- **The crystal symbol is a 12 MHz part with the 40 MHz MPN overridden**
  (`ABLS2-12.000MHZ-D4Y-T` symbol, `MPN=ABLS2-40.000MHZ-D4YF-T`). The value field
  reads 40MHz. Harmless electrically, but the SnapEDA metadata fields still
  describe the 12 MHz part and will pollute a generated BOM.
- **PCB status: fully routed.** 368 F.Cu segments, 105 B.Cu segments, 44 vias, 0
  unconnected items. `freerouting` was used at some point
  (`temp-freerouting.dsn` is present).
- **DRC: 0 violations, 0 unconnected, 0 schematic-parity issues** (2026-08-11).
  The U6 pad-to-pad clearance errors are cleared by the scoped rule in
  `canary-hat.kicad_dru` rather than by moving copper (§2.1, §12.4).
- **Run CLI DRC with `--refill-zones`.** `kicad-cli pcb drc` does *not* refill by
  default, so it checks new vias against stale zone fills and reports phantom
  clearance violations — 10 of them, the first time this bit. The GUI refills
  automatically, which is why the two disagree.
- **Fiducials:** FID1 (103.50, 52.50), FID2 (161.50, 54.70), FID3 (103.50, 91.25),
  all `Fiducial_1mm_Mask2mm` on F.Cu, references on F.Fab, attributes
  `board_only` + `exclude_from_bom`. Non-collinear, 3.5mm from the board edge,
  clear of the mounting holes, and asymmetric under 180° rotation by ~1.95mm so
  orientation is unambiguous. `board_only` matters: without it, *Update PCB from
  Schematic* with "delete footprints with no symbols" would remove them.
- **ERC: 5 warnings**, all benign — a `SolderJumper_2_Open` library mismatch, and
  four pin-type complaints where power flags meet strapped bidirectional pins
  (U5 SDO, U6 SDX/SCX/CS tied to rails as mode/address straps).
- **Mechanical:** screw terminals J2 and J3 mount on the **top** side at the
  board's lower edge (Y ≈ 94.5), facing away from the stack; the SMA (J4) is on
  the right edge. The Pi 5 active cooler sits below the HAT; stack height is set
  by the cooler plus the X1202 below, so size the standoffs and header extension
  to clear both.
- **Layout as placed:** the CAN group (U3, U4, Y1, C5–C9) sits centre-left near
  the terminal block; the sensors (U5, U6) sit top-left, away from the crystal;
  the GNSS module (U7) sits at the right edge with a short RF run to J4 through
  C16/L1; the K-line transceiver (U2) and the 12V protection cluster (D4, D1, C2)
  sit along the bottom edge near J2.

## 7. Manufacturing Notes

- Gerbers for all 4 copper layers (F.Cu, In1.Cu/Gnd.Cu, In2.Cu/Power.Cu, B.Cu),
  soldermask and silkscreen both sides, plus Edge.Cuts.
- Drill files in Excellon; through-hole vias only (0.6–0.9mm diameter / 0.3–0.5mm
  drill per net class), no blind/buried, no microvias.
- Confirm the fabricator's actual stackup matches the 0.1mm outer prepreg
  assumed by the GNSS RF trace (§3.6).
- Gnd.Cu is a solid board-wide GND pour; Power.Cu is a single +3V3 pour. Stitch
  both to component pads generously — particularly under the sensors and along
  the RF path.
- MAX-M10M-00B requires an **external active antenna with integrated SAW filter
  and LNA**, since this variant omits onboard RF front-end filtering [cite:39].
  The board supplies bias for it through L1.
- BT1 requires an external **primary** backup cell on a JST-SH lead (no charge
  path on the board — §3.6).
- EEPROM WP: JP1 open at build = write-enabled. Bridge JP1 to protect.
- Pick-and-place/centroid file to be exported alongside Gerbers if outsourcing
  partial assembly for the LGA parts (U5 BMP384, U6 LSM6DSOX) and the module (U7);
  the SOIC parts are hand-solderable.
- BOM hygiene has moved to **§12**, which is now the fabrication and assembly
  readiness checklist. The items this section used to list — the CAT24C256 part
  pin, J2's wrong value, Y1's 12 MHz metadata, C2's voltage rating — are all
  resolved there.

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
   Now `+12V → R5 → D5 → LIN` with J2 pin 4 direct to the pin, per §11.1 Pin 6 and
   Figure 8-1. D5 also changed from an SS54 Schottky to a 1N4148W, because the
   diode's job is blocking reverse current into an unpowered VSUP and Schottky
   leakage undermines exactly that (§3.3).
4. **R8 added** (4.7 kΩ, TXD to +3V3, Pi side of R6). Not a defect in the original
   drawing so much as a missing guard: TXD is internally pulled to ground, so with
   EN already raised, a collector that dies or releases the tty would leave the
   vehicle's K-line held dominant. §3.3.2 has the placement and value reasoning.
5. **BMP384 interrupt on GPIO6**, which `ups_shutdown_watch.py` holds as the
   X1202's power-loss-detect input — two `lgpio` claimants collide with EBUSY.
   Moved to **GPIO17** (§4.1).

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

### 8.3 Confirm before fabrication
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
- **C4 placement.** It sits at (141.0, 77.0), out by the connector end rather than
  at U2's LIN pin at (129.25, 77.41). Defensible under §11's "place filtering at
  the connector", but TI's own layout example puts it at the pin — make it a
  decision, not an accident.
- **Depopulate R7/R9** (duplicate I2C pull-ups, §5) unless there is a measured
  reason to keep them.
- **V_BCKP fallback** — no path from +3V3, no charge path, floating if BT1 is
  absent (§3.6). At minimum decide "primary cell always fitted" and record it.
- **Antenna bias protection** on the L1 feed (§3.6).
- **TCAN1042 VIO undervoltage behaviour** with VDD present and VIO absent — the
  STANDBY row depends on it (§3.7.5).
- **Star-point placement** for the signal/chassis ground bond across J2 pins 2
  and 3 (§3.9).
- **RF stackup** — the 0.2mm feed assumes 0.1mm to Gnd.Cu (§3.6, §7).
- **Verify no I2C clock stretching and whole-device WP** on the CAT24C256
  (§3.8.1).
- **Quiescent draw on +12V** with the vehicle parked, if the harness ever lands on
  always-hot OBD pin 16.
- **BMP384 RMS noise** against the grade budget; the BMP390 is footprint-
  compatible if it falls short (§3.5).
- **TLIN1027 20 kbps transmit ceiling** against the intended CONSULT baud rate.

### 8.4 Deferred / not in this revision

Recorded so they are decisions rather than discoveries:

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
- **IMU:** add `lsm6dsox` to the `ImuConfig.source` Literal in `src/config.py`
  (currently `bno055 | mpu6050 | icm20948 | disabled`) and write
  `imu_collector.py` (roadmap task 2b.2) against I2C address **0x6B** with INT1 on
  **GPIO16**. The frame-resolution math in `imu_frame.py` already covers the
  raw-chip case.
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

## 12. Fabrication and Assembly Readiness

Everything here is about getting boards *made*, not about whether the circuit is
right. Worked through on 2026-08-11; the schematic edits described are done.

### 12.1 What the schematic now carries

Component attributes were the real gap, not part numbers. A 12V board whose
capacitors carry no voltage rating will be built with 16V parts, and the failure
arrives on the first load-dump clamp rather than at assembly.

| Ref | Rating now specified | Why it is not the default |
|---|---|---|
| C2 | 50V, 105°C, 20% | D1 (SMBJ24A) clamps at ~38.9V, so a 25V part is overstressed on every transient. 105°C for a cabin. |
| C3 | 50V X7R | Sits on +12V at the TLIN1027 VSUP pin. A 16V 0603 dies on the first clamp. |
| C4 | **100V** C0G | On the K-line bus: ±45V fault rating, ISO 7637-2 pulses to +100V/−150V. 50V is marginal. |
| C5, C6 | C0G/NP0, 50V | Crystal load caps. X7R shifts with temperature and pulls the 40 MHz oscillator — which `mcp251xfd` derives its bit timing from. |
| C16 | C0G/NP0, 50V | GNSS RF DC block; X7R has poor Q at 1.575 GHz. |
| C1, C7–C15 | X7R, 25V, 10% | Explicitly not Y5V/Z5U. C15 (10µ 1210) also carries a DC-bias derating note. |
| R5 | **0.2W** | Dissipates **151 mW** during a stuck-dominant bus fault (14.1V rail − 0.8V diode − 1V dominant, squared over 1 kΩ). A standard 0.1W 0603 is under-rated. Normal 10.4 kbps traffic averages ~75 mW. |
| C5, C6 | **27pF**, C0G | Currently 22pF. The ABLS2's standard CL is 18pF → C = 2·(CL − C_stray) ≈ 28pF (§3.1.1). |
| C16 | 15pF C0G, **0402** | SRF-tuned DC block — sized so self-resonance lands at 1.575 GHz, where the series impedance nearly vanishes (§3.6). |
| R1–R4, R6–R9 | 0.1W, 1% | Unremarkable. |

MPNs are pinned on U1, U2, U4–U7, Y1, L1, D1, D2, D4, D5, J2, J3, J4 and BT1.
U6 is `LSM6DSOXTR` (tape and reel). J1 and J5 are deliberately left without —
they are commodity parts, and a plausible-but-wrong part number is worse than a
blank field.

**Do-not-substitute notes** are attached as schematic fields so they survive a
BOM export, on the three parts where a substitution is silent:

- **U1** — floating WP *and* A0/A1/A2 rely on onsemi's internal pull-downs
  (§3.8.2). Microchip's 24LC256 requires WP tied to a rail.
- **D5** — must stay silicon. A Schottky's reverse leakage defeats the whole
  point of the blocking diode (§3.3).
- **D2** — the **plain single-diode BAT54, marking KL1**. BAT54A/C/S are duals
  with the common node on pin 2 and will not conduct pin 1 to pin 3 at all.
  Note the naming trap: `BAT54-A` is the AEC-Q101-qualified *single*, while
  `BAT54A` is the common-anode *dual*. One hyphen apart, and the wrong one gives
  a GNSS that cold-starts every boot with no other symptom.

**Y1's footprint name is still a misnomer** — `XTAL_ABLS2-12.000MHZ-D4Y-T`, from
the imported library, while the part is the 40 MHz `ABLS2-40.000MHZ-D4YF-T`. The
stale SnapEDA metadata fields that also named the 12 MHz part have been stripped;
a note on the symbol now says to order by MPN and never by footprint or symbol
name.

### 12.2 Generating the BOM — one trap

**Group by `Value,MPN,Voltage,Dielectric,Power,Tolerance`, not by `Value`.**
Grouping on Value alone silently merges parts that differ only in rating: it
collapses C3 into `C1,C3,C7-C14` with a Voltage column reading `25V,50V`, and
merges R5's 0.2W with R6's 0.1W. Either produces one order line for two different
parts, and the wrong one lands in the position where it matters.

```
kicad-cli sch export bom \
  --fields 'Reference,Value,Footprint,MPN,Manufacturer,Voltage,Dielectric,Power,Tolerance,Height,Note' \
  --group-by 'Value,MPN,Voltage,Dielectric,Power,Tolerance' \
  -o BOM.csv canary-hat.kicad_sch
```

`BOM-offboard.md` in the project directory covers what the schematic cannot: the
J5 shunt, the GNSS backup cell and antenna, the barrel-jack pigtail, the OBD-II
Y-cable, ferrules, and standoffs.

### 12.3 Assembly inputs

- **Fiducials: done.** Three, correctly attributed — see §6 for positions and why
  `board_only` matters.
- **Assembly drawing with pin 1 marked, for U5, U6 and U7.** Their footprints are
  SnapEDA/UltraLibrarian imports whose zero-rotation convention often disagrees
  with IPC-7351 and therefore with the assembler's. Rotation error on those three
  is the most likely assembly defect on this board.
- **Assembly scope split** — state which parts the house places (U5, U6, U7 at
  minimum) and which are hand-soldered, so the centroid and BOM agree.
- **Stencil** if the LGAs are self-assembled, with paste apertures decided
  alongside the mask expansion below.
- **J1 is a 13mm-insulator 2×20 stacking header** — the part already proven to
  clear the Pi 5 active cooler with the Waveshare HAT. Re-check if the X1202 ends
  up between the Pi and this board.

### 12.4 U6 land pattern and the DRC exception

The imported LGA-14L footprint is **0.3048 × 0.5334 mm** — exact imperial values
(0.012" × 0.021") on a 0.5 mm metric-pitch part, the fingerprint of a generic
conversion. ST's TN0018 rule gives the correct land: for LGA pin spacing above
200 µm (this part is 0.5 mm pitch with 0.25 mm pins, so 250 µm), land = pin +
0.1 mm in each dimension:

| | TN0018 | As drawn | Delta |
|---|---|---|---|
| Land width | 0.350 mm | 0.3048 mm | 13% narrow |
| Land length | 0.575 mm | 0.5334 mm | 7% short |
| Land area | 0.201 mm² | 0.163 mm² | **19% less** |

This is a leadless package — the solder joint is the only mechanical attachment,
on a board bolted into a vehicle. **Resize recommended**, and note it makes DRC
*worse*: correct lands on 0.5 mm pitch leave a 0.15 mm pad gap rather than the
current 0.1952 mm. Both are inside the 0.127 mm fab capability §7 assumes. Pad
centres do not move, so no re-routing — but re-run DRC afterwards, since the
lands grow ~0.02 mm in each direction.

**Solder mask expansion: +0.025 mm per side** (openings 0.40 × 0.625, dams
0.10 mm). ST's C/D figures imply +0.05/side, which leaves 0.05 mm dams — below
any fab's minimum, so they would be dropped and the openings ganged. Zero
expansion gives safe 0.15 mm dams but lets ±0.05 mm mask registration encroach on
a 0.35 mm land and make the joint partly solder-mask-defined, a fatigue stress
concentrator that undoes the point of the resize. Fall back to zero expansion
only if the fab rejects 0.10 mm dams.

`canary-hat.kicad_dru` carries the scoped exception at 0.14 mm with the
derivation as a comment. It is scoped to U6 so board-wide clearance stays at
0.2 mm — in particular around the +12V nets, which want *more* clearance, not
less. **Verify the `insideCourtyard` predicate in Board Setup → Custom Rules**,
which has a syntax checker.

The reason to scope it rather than relax the global rule is not the solder joint:
it is that a board permanently reporting six DRC errors trains you to skim past
DRC output, and the next real violation gets lost in it.

### 12.5 Before sending

**Done**

- [x] Three fiducials, correctly attributed (§6)
- [x] SMA anti-pad and launch stitching — 2.9 dB → 25 dB return loss (§3.6.1)
- [x] Outer-layer GND pours on F.Cu / B.Cu, with J4 solid and J1 unconnected (§2)
- [x] C16 → 15pF C0G 0402, SRF-tuned (§3.6)
- [x] CAN bus protection and X1202 leg protection — decided, see §8.2
- [x] Crystal researched — CL is 18pF, so **C5/C6 → 27pF**; `F` option is
      load-bearing (§3.1.1)
- [x] DRC 0 violations, 0 unconnected, schematic parity clean
- [x] **C5/C6 changed to 27pF**
- [x] Fab notes written — `FAB-NOTES.md` in the project directory, covering the
      0.1mm outer-prepreg requirement, ENIG, mask expansion, fiducial positions,
      assembly scope, pin-1 warning and the do-not-substitute list
- [x] BOM regenerated with the §12.2 grouping, now including Descriptions
- [x] **Export hygiene for the PCBWay plugin:** `exclude_from_pos_files` on FID1–3
      and JP1 (they were appearing in the centroid as placeable parts); Descriptions
      added to U2–U7, the six imported symbols that had none; connector Values
      changed from library artifacts to something an assembler can read
      (`GPIO` → `2x20 stacking hdr 13mm`, `Conn_Coaxial` → `SMA edge mount`,
      `Battery_Cell` → `GNSS backup cell (JST-SH)`, `Conn_01x02` →
      `K-line PU select hdr`). CPL now exports 45 parts and nothing else
- [x] **TN0018 copper keepout under U6** — F.Cu rule area at X 104–107, Y 56–59.25,
      `copperpour not_allowed` only. Copper features under an LGA create
      solder-mask topology that stops the package seating flat, and the resulting
      die stress shows up as zero-g offset (datasheet p16). Tracks/vias/pads must
      stay *allowed* — the area necessarily covers U6's own pads, so forbidding
      them blocks the part's own connections
- [x] **U2 grounding** — nearest GND via to pin 5 is now 1.33mm with a second at
      2.54mm, satisfying TLIN1027 §11.1 via the pour stitching
- [x] **BAT54 pin 1 confirmed as the anode** — D2.1 → BT1+, D2.3 → V_BCKP, so
      current flows 1 → 3 as required. The last silent-failure item is closed
- [x] Crystal, load-cap and DC-block reasoning written into the schematic `Note`
      fields on Y1, C5, C6 and C16, so it travels with a BOM export rather than
      living only in this document

**Outstanding**

- [ ] Optional: resize U6 lands to 0.35 × 0.575 with +0.025mm/side mask expansion
      (§12.4) — a reliability improvement, not a defect
- [ ] Assembly drawing with pin 1 marked for U5, U6, U7
- [ ] Export gerbers, drill, centroid (PCBWay plugin)

## 13. Future revisions

**Rev A is locked** — submitted to PCBWay 2026-08-11. Nothing here changes that
board. The staging below separates *fixing what the first board teaches us*
(rev B) from *adding a subsystem* (rev C), because those carry very different
risk.

### 13.1 Measure on rev A — this feeds everything below

Several rev-B decisions cannot be made from datasheets and are only answerable
with a physical board. Capture these during bring-up, while the board is new and
the questions are still live.

| # | Measurement | What it decides |
|---|---|---|
| 1 | **K-line commander mode against the ECU** — does polling work at all? | The reason this board exists (§9). Everything else is secondary. |
| 2 | **Does the 1 kΩ pull-up disturb an NT510/UpRev session?** Sniff with J5 fitted, then open. | Whether §13.2's FET circuit is needed at all, or whether the jumper was a hedge against nothing. |
| 3 | **Crystal frequency and startup** with C5/C6 = 27pF. | 27pF was derived from a CL figure, never measured. Confirm 40 MHz ±0.5% and clean start; a wrong load cap shows as a frequency offset. |
| 4 | **GNSS TTFF and C/N₀**, and whether the WiFi hotspot desenses it. | Validates the RF launch (§3.6.1) and whether the MAX-M10**S** (integrated SAW+LNA) is needed. |
| 5 | **V_BCKP hot start** — TTFF with and without BT1 fitted. | Whether the backup cell earns its place, and whether the no-charge-path design is acceptable. |
| 6 | **I2C rise times** with R7/R9 plus the Pi's own 1.8 kΩ pull-ups. | Whether to depopulate R7/R9 (§8.3). |
| 7 | **Actual 3.3V draw**, idle and with GNSS acquiring. | Confirms the budget in §3.7.5 that justified deleting the buck. |
| 8 | **Thermals on D4 and R5** with the engine running. | D4 now passes only the HAT's load, and R5 is the 0.2W-rated part — both should be cool. Confirm rather than assume. |
| 9 | **BMP384 RMS noise** at the intended ODR against a grade budget. | Whether the footprint-compatible BMP390 swap is needed (§3.5). |
| 10 | **TLIN1027 at the real CONSULT baud.** | The 20 kbps *transmit* ceiling vs what the Xterra actually needs (§3.3.1). |
| 11 | **X1202 quiescent draw** once it has cut its own output. | Feeds the rev-C supervisor architecture (§13.3). |
| 12 | **Power-apply to first recorded frame** — wall-clock from 12V return to `canary.service` writing row 1. | Sizes the rev-C boot-gap buffer (§13.3.3) and quantifies what every cold start currently loses. The journal in `docs/time-sync.md` shows 12 s from *kernel handoff*, which is a floor, not the number. |
| 13 | **Does the K-line RXD break reach the host as SysRq?** Boot with the console left enabled on `uart0` and the HAT fitted. | Confirms the §11 step-1 hazard is real and worth designing out in rev C (§13.3.4), rather than a theoretical reading of Table 8-1. |

**Measurement 1 does not have to wait for rev A.** The Macchina M2 (open
hardware, `github.com/macchina/m2-hardware`) brings K-line out on OBD pin 7 and
can answer "does this ECM respond to CONSULT over K-line at all" independently —
de-risking §9, the entire justification for this board, before the PCBWay boards
land.

**But it must be re-terminated first, or a negative result means nothing.** Read
off the M2-B interface schematic (`SCH-01006 R2`) on 2026-08-11: both
ISO9141/LIN channels are a TJA1021 with a **10.0 kΩ pull-up to +12V and a 220 pF
bus cap, and no diode anywhere on the sheet**. That is neither LIN responder
termination (~30 kΩ) nor the commander termination a tester requires (§3.3.1:
1 kΩ plus a series blocking diode). Fit 1 kΩ and a 1N4148W in the harness
between +12V and pin 7 before concluding anything from a silent bus.

Worth knowing while you are in there: the M2 connects essentially all 16 OBD
pins, including **single-wire CAN on pin 1 and medium-speed CAN on pins 3/11**,
with the four remaining discretionary pins on test points. Manufacturer-
discretionary bus access is therefore *not* an unserved gap in the market —
useful context for any future form-factor work.

### 13.2 Rev B — fixes and basic GPIO control

Small, self-contained changes. No new subsystems, no firmware.

**The one that matters most: STB on a GPIO.** The TCAN1042V's STB pin is
hardwired to GND on rev A, which permanently forbids standby mode — see §13.3
for what that costs. It is a one-net change and every low-power option depends on
it. Note the pin has an internal pull-up forcing standby when floating, so a GPIO
that is high-Z at boot gives low-power-by-default and requires software to assert
normal mode; the daemon must drive it low at startup or CAN silently will not
work.

Also queued for rev B:

- **Rename the `KLINE_RAW` net to `KLINE`** — on this board `_RAW` means
  "upstream of protection", but this net *is* the protected, terminated bus.
- **U6 land pattern to ST TN0018** (0.350 × 0.575mm, mask +0.025mm/side). Rev A
  ships ~19% under-size — a solder-joint reliability position, not a defect
  (§12.4).
- **Depopulate R7/R9** if measurement 6 confirms the Pi's own pull-ups suffice.
- **12V sense** — a divider into an ADC or a comparator, so the daemon can
  distinguish engine-running (~14V) from key-off (~12.4V) from cranking. Cheap,
  and a prerequisite for any voltage-based wake in rev C. Put the divider where
  an MCU can also reach it: §13.3.3(b) turns the same net into crank-profile and
  alternator-ripple diagnostics once it is sampled at kHz rather than read as a
  status bit.
- **Status LEDs and test points** (§8.4) — power good, CAN activity, GPS fix, plus
  probe points on LIN, RXD and EN. In-vehicle debugging currently costs an SSH
  round trip.
- **Whatever rev A's bring-up turns up.**

#### GPIO-controlled K-line pull-up (replaces the J5 jumper)

*Gated on measurement 2 in §13.1 — if the fitted jumper turns out not to disturb
an NT510 session, none of this is needed.*

J5 selects commander mode (pull-up fitted) vs passive sniff (open). Changing it
means unstacking the HAT from the Pi under the dash, which is exactly the
friction that stops the mode being switched when it should be. A level-shifted
high-side switch makes it a GPIO:

```
+12V ──┬── Ra 100k ──┬── gate ── Q1 (P-FET, BSS84)
       │             │           source = +12V
       └─────────────┴───────────drain ──> R5 (1k) ──> D5 ──> LIN
                     │
                  Rb 10k
                     │
                  Q2 drain (N-FET, BSS138), source = GND
                  Q2 gate ── GPIOxx, 100k pulldown to GND
```

- GPIO low **or floating** → Q2 off → Q1 gate at +12V → Vgs = 0 → pull-up
  disconnected → **sniff mode is the safe default**, so a Pi that is off,
  booting or crashed cannot perturb an NT510 session.
- GPIO high → Q2 on → Q1 gate at ~1.1V → Vgs ≈ −11V → pull-up active.

Numbers: Vgs −11V is inside BSS84's ±20V limit; the divider wastes ~110 µA;
Q1's ~10Ω Rds(on) is a 1% error on a 1 kΩ pull-up; D5 still blocks bus-side
leakage when Q1 is off.

**Two approaches that do not work**, recorded so they are not re-attempted:
driving the P-FET gate directly from a GPIO never turns it *off* (3.3V against a
12V source is still Vgs = −8.7V), and a low-side N-FET cannot sit in the LIN leg
because its source would be at bus potential, needing gate drive above the rail.

**Deferred because the hedge may be unnecessary.** With 1 kΩ paralleling an
NT510's ~510Ω, a transmitting tool sinks roughly 40 mA to reach dominant — well
inside what a commercial scan tool drives. The first board will show whether the
extra pull-up disturbs anything; if it does not, the five parts are never needed.

### 13.3 Rev C — the supervisor MCU

The goal that opened this section: leave the rig plugged into OBD permanently
without draining the vehicle battery. Working it through settles on a
**supervisor MCU** rather than a comparator (§13.3.1) — and once an always-on MCU
exists, a set of capabilities the Pi structurally cannot provide come nearly
free, several of which close defects and procedural hazards recorded elsewhere in
this document.

**The board revision owes the MCU only *connections*.** Firmware grows for years
after fabrication; a missing net means a re-spin. So the design question is
*which nets must exist*, not *what the firmware should do* — which is what makes
the marginal items in §13.3.3–§13.3.4 cheap enough to include on spec, and why
the net list (§13.3.6) deserves care even where the firmware behind it is still
speculative.

| | |
|---|---|
| §13.3.1 | Sleep, wake, and why the Pi must be genuinely off |
| §13.3.2 | The power-domain question — settle this before drawing anything |
| §13.3.3 | Capabilities the Pi cannot provide |
| §13.3.4 | Hardware-side benefits, by subsystem |
| §13.3.5 | The bus-access rule: receive-only fan-outs |
| §13.3.6 | Nets the board must carry |
| §13.3.7 | Deliberately out of scope |
| §13.3.8 | Rejected: PCIe as the data transport |

#### 13.3.1 Sleep, wake, and why the Pi must be genuinely off

**One net change unlocks most of the HAT side.** The TCAN1042V's STB pin is
currently hardwired to GND, which permanently forbids standby. Per its §9.4.3
and EC table:

| TCAN1042V mode | VCC (5V) | VIO (3.3V) |
|---|---|---|
| Normal, recessive | 1.5–2.5 mA | 90–300 µA |
| **Standby** | **0.5 µA** — *VCC may be removed entirely* | **12–17 µA** |

In standby the low-power receiver detects the ISO 11898-2 **Wake-Up Pattern** and
signals it by driving RXD low — this is already a wake-on-CAN transceiver.
§9.3.5 also notes STB has an internal pull-up forcing standby when floating, so
**STB on a GPIO gives low-power-by-default** and requires software to assert
normal mode. Combined with the TLIN1027 (8–12 µA sleep, wake-on-LIN, already on
GPIO26), MCP2518FD (15 µA sleep), BMP384 (2 µA) and the LSM6DSOX's wake-on-motion,
the HAT reaches roughly **50 µA**.

**The Pi is the whole problem.** Against ~20 Ah of usable battery before a
no-start becomes a risk:

| State | Draw at 12V | Time to 20 Ah |
|---|---|---|
| Pi 5 idle | ~250 mA | **~3 days** |
| Pi 5 halted | ~80 mA | ~10 days |
| HAT asleep + µA supervisor | <1 mA | months |

So the Pi must be genuinely **off**, and something else must turn it back on.

**Proposed architecture — a load switch on the J3 pass-through leg**, reusing the
shutdown path that already exists rather than replacing the X1202:

```
+12V_RAW ──> [high-side load switch, ~3A] ──> J3 ──> X1202 ──> Pi
                        ▲
                 supervisor (µA)
```

- *Wake:* supervisor closes the switch → X1202 gets 12V → Pi boots.
- *Sleep:* supervisor opens the switch → X1202 sees power loss → PLD on GPIO6 →
  `ups_shutdown_watch.py` shuts down gracefully → Pi off.

This avoids the HAT having to generate 5V for the Pi, which would make it a
Power HAT+ (§3.8).

**Wake sources, cheapest first:**

1. **Alternator voltage sense.** Engine running ~13.8–14.4V vs key-off ~12.4V; a
   comparator with hysteresis at ~13.2V is unambiguous, needs no bus activity,
   ~6 parts, ~50 µA, no firmware. Misses key-on-engine-off, which the project
   does use (`ignition_only`).
2. **CAN wake via the TCAN1042.** Catches KOEO, but its low-power receiver runs
   from **VIO = Pi 3V3**. Pi off ⇒ no VIO ⇒ no wake detector. Requires a small
   always-on 3.3V rail from vehicle 12V — reintroducing the buck deleted in §3.7,
   though now at a few mA and with a concrete justification rather than the
   current-headroom argument that did not survive scrutiny.
3. **Supervisor MCU.** All wake sources, timers, and a proper handshake letting
   the Pi request its own shutdown. Costs firmware and a programming header.

**Decided: option 3.** Not because 1 and 2 fail on their own terms — a
comparator is genuinely cheaper and needs no firmware — but because the
handshake at the end of this section already forces an MCU (a comparator can cut
power; it cannot *agree with the host* about when), and because options 1 and 2
stop being separate line items once an MCU is present: alternator sense is one
ADC channel, CAN wake is one interrupt pin. The MCU absorbs both wake circuits
rather than adding to them. Everything in §13.3.3 onward is what that decision
then makes available.

**X1202 behaviour — confirmed 2026-08-11** from `scripts/ups_shutdown_watch.py`,
which documents the full cycle:

> "The X1202 then cuts battery output, so the next time switched 12 V returns
> (key-on) it's a fresh power-apply event and the board auto-boots."

- **Auto-boot on 12V return: yes.** No button press, so the load-switch approach
  is viable.
- **Power-loss signalling: yes**, via PLD on GPIO6, HIGH = input OK, LOW = lost.
- **It cuts its own battery output after shutdown**, so post-sleep draw should be
  small. Still worth measuring, but it is no longer a blocking unknown.

The historical unreliability is understood rather than mysterious: an earlier
version had the **PLD polarity inverted**, and PLD alone cannot distinguish
vehicle key-off from bench USB-C with the barrel jack unplugged. That second
ambiguity is now resolved by fusing PLD with the fuel gauge's discharge rate
(`power_truly_lost()`), so a flat SoC on bench power never trips a countdown.

**Do not route routine sleep through the power-loss path.** `UPS_SHUTDOWN_DELAY`
defaults to **300 s**, so simply cutting 12V leaves the Pi running five more
minutes *on the UPS battery* before it powers off. Every key-off would partially
cycle a cell whose actual job is emergency shutdown, and each sleep transition
would take ~6 minutes. Use a handshake instead:

1. Daemon sees sustained `engine_off` / `no_data` (the §3.7.6 software, still
   unwritten).
2. Daemon signals the supervisor "cutting in N seconds", then `systemctl poweroff`.
3. Supervisor waits, confirms the Pi is down, opens the switch.
4. The X1202 has nothing to hold up — the battery is never touched.

`UPS_SHUTDOWN_DELAY` then remains what it should be: the emergency path for
*unexpected* power loss, not the routine one. The handshake needs a bidirectional
link (at least one GPIO each way), which pushes the supervisor toward an MCU
rather than a bare comparator — a comparator alone can only cut power, not agree
with the host about when.

**Still to measure:** the X1202's quiescent draw once it has cut output, and
whether its auto-boot is reliable enough to be the only wake mechanism.

**Related software** (§3.7.6): escalating a sustained `engine_off` / `no_data`
`vehicle_state` to a shutdown still does not exist, and is a prerequisite for any
of this regardless of which wake source is chosen.

#### 13.3.2 The power-domain question — settle this before drawing anything

An always-on 3.3V rail is what every item below depends on, and it reintroduces
exactly what §3.7.5 designed out. That section's argument was never about current
headroom — it was that the HAT sits between a vehicle feed and a UPS-backed host,
so **each supply can outlive the other**, and with split rails a device is powered
while the domain driving its I/O is not. The single-rail board satisfies the HAT+
STANDBY requirement *by construction*. Rev C breaks that construction
deliberately, so it has to satisfy the requirement by design instead.

The §3.7.5 state table with the MCU rail added:

| State | Vehicle +12V | Pi 5V | Pi 3V3 | MCU rail | Cross-domain? |
|---|---|---|---|---|---|
| Normal run | up | up | up | up | no |
| UPS battery run (key off, X1202 carrying the Pi) | **down** | up | up | **down** | yes — MCU side dead |
| STANDBY (`sudo halt`, vehicle connected) | up | up | **down** | up | yes — Pi side dead |
| **Parked, Pi powered off, vehicle connected** — the rev-C steady state | up | **down** | **down** | up | **yes, and it is now the common case** |

The fourth row is new, and it is where the rig will spend most of its life. Every
MCU↔Pi net is then a powered driver facing an unpowered input: current through
the Pi's ESD clamps into a dead rail, and on a shared bus, a bus hung for whoever
is left alive.

Three mitigations. Rev C should state which one it uses **per crossing net**:

- **Series resistance on every crossing net.** R6's precedent (1 kΩ in the TXD
  line, ~10 ns RC against a 96 µs bit) — cheapest, and sufficient for slow
  handshake lines.
- **MCU I/O specified for an unpowered far side.** Check the datasheet rather
  than assuming; "5V tolerant" is a different claim.
- **The MCU must not drive a Pi-domain net high while 3V3 is down.** This is the
  rule that actually closes it, and it requires the MCU to *know*: one ADC
  channel or comparator on the Pi's 3V3 rail, tri-stating everything that crosses
  when it reads low. Note this is a firmware invariant guarding a hardware
  hazard — the shape AGENTS.md warns about — so back it with the series resistors
  regardless, and prefer a hardware default (crossing nets idle low, MCU drives
  only when it must) over a firmware promise.

**HAT+ conformance becomes something to argue rather than inherit.** §3.7.5
quotes the spec: HAT+ boards "must be electrically compatible with the STANDBY
power state, where the 5V power rail is powered, but the 3.3V rail is not
powered," and "must not assume any particular sequencing timing between the 5V
and 3.3V rails." Rev A satisfies both by having nothing alive when 3V3 is down.
Rev C must show it satisfies them with a rail that *is* alive — a paragraph of
analysis owed to this document, not a checkbox.

**The regulator's quiescent current drives the sleep budget, not the MCU's sleep
mode.** Against ~20 Ah of usable battery, 1 mA is ~2.3 years and 200 µA is over a
decade — well past the point where the vehicle's own parasitic load and the
battery's self-discharge dominate. Specify a nA-class-Iq buck (TPS62840 class,
~60 nA) and the MCU's sleep figure stops being a selection constraint. That
matters because it reopens part choice: an RP2350's 520 KB of RAM (§13.3.3a) or
an STM32G4's three FDCAN peripherals become affordable, where a µA-first
selection would have forced a 32 KB part.

**One rail serves two jobs.** §13.3.1's wake source 2 needs VIO powered for the
TCAN1042's low-power receiver; the MCU needs the same voltage. That is one
regulator, and it is the concrete justification §13.3.1 asked for — as against
the current-headroom argument that "did not survive scrutiny" in §3.7.5.

#### 13.3.3 Capabilities the Pi cannot provide

Each is unavailable to the Pi for a structural reason: it is asleep, it has no
sensor, or Linux cannot make the timing guarantee.

**(a) The boot-gap black box.** The strongest case, and rev C's own sleep
architecture is what creates it.

Once `idle_shutdown.py` (§11) plus the load switch put the Pi genuinely off when
parked, key-on → first recorded frame is the full boot time (§13.1 measurement
12). That window holds the data `docs/roadmap/data-collection-plan.md` ranks
first and calls unfakeable — capture 1, the cold-start session: "warmup curve,
cold-enrichment fueling, open→closed-loop transition… **Can't be faked; needs an
actually-cold engine.**" There is one per day, and rev C is otherwise about to
lose the first 20–40 s of every one. Crank itself has never been observed at all.

The MCU is awake at that instant — deciding to wake *is* its job — so buffering
broadcast CAN from the moment of wake costs RAM and nothing else. 500 kbit/s at
realistic load for 40 s fits in a few hundred KB uncompressed. Frames carry
MCU-timebase timestamps (c), so the recovered window is not second-class data.

Note the scope: **broadcast only** — no requests, no ISO-TP, no poll plan in
firmware (§13.3.7). That suffices, because broadcast is already the primary live
path; `docs/data-architecture.md` on it: "zero polling cost, no contention,
deterministic cadence, high refresh rate."

**(b) Electrical-system diagnostics.** §13.2 already queues a 12V divider into an
ADC as a *status* input. Behind an MCU sampling at 1–10 kHz it becomes an
instrument, measuring quantities the ECU does not publish at all:

- crank voltage minimum and recovery slope → battery health
- crank duration → starter and cold-fuelling health
- alternator ripple → rectifier-diode failure, an ordinary fault at this vehicle
  age and invisible to OBD
- parasitic draw trend while parked, over weeks

These need no reverse engineering and carry no binding risk — rare in this
project — and they degrade slowly and monotonically, the exact shape
`docs/roadmap/eval-longitudinal-and-dtc.md` Phase 4 exists to detect.

**(c) One hardware time base.** Today CAN frames, OBD responses, IMU samples and
GPS fixes are each stamped in a different place with different latency, all
through Linux scheduling. An MCU counter disciplined by the M10's TIMEPULSE can
stamp CAN RX, K-line bytes, IMU FIFO entries and its own ADC in **one monotonic
domain**, with the PPS edge tying that domain to GPS absolute time.

This is worth more here than in most projects. AGENTS.md's "never compare two
signals by their aggregates" rule, and `compare_channels`' insistence on
same-instant pairing, exist because cross-signal comparison has produced six
retracted findings in three months. A single hardware time base attacks that at
the source rather than in review.

It also largely dissolves `docs/time-sync.md`'s cold-boot problem: the MCU keeps
monotonic time across Pi power cycles, so a session's *internal* timeline is
never fractured by a mid-session step. That doc's outstanding "clock-jump session
roll" item drops from a correctness issue to bookkeeping.

**(d) Parked monitoring.** Battery voltage every few minutes, CAN wake events,
and motion via the LSM6DSOX — **INT2 (pin 9) is unconnected on rev A**, so it is
free. Tiny data volume, ingested on the next boot. Catches a tow, a developing
parasitic drain, or someone in the truck.

**(e) A watchdog with power-cycle authority.** The MCU already holds the load
switch; a heartbeat line plus a timeout means a hung Pi recovers without someone
under the dash — currently the failure mode with the worst manual cost.

#### 13.3.4 Hardware-side benefits, by subsystem

**K-line (U2, TLIN1027) — the largest hardware win on the board.** §11 lists a
seven-step startup order the collector must execute correctly. Most of those
steps exist because a Linux tty cannot do what the part needs, and an MCU owning
the transceiver's host side removes them as a class rather than making them
easier:

| §11 / §3.3 requirement | Why Linux struggles | With the MCU |
|---|---|---|
| Blank RXD ~50 µs after raising EN (`tMODE_CHANGE` + `tNOMINT`); 1.5 ms after power-up | userspace cannot gate a UART RX at µs scale — the garbage lands in the tty buffer and is filtered after the fact | receiver gated for exactly that window; the garbage never exists |
| Power-up lands in **standby**, RXD driven low = a continuous serial **break**; with a console on `uart0`, magic SysRq can kill or reboot the host (§11 step 1) | mitigated only by *remembering* to disable the console | the break physically cannot reach the Pi |
| Open the tty before raising EN; drop EN before releasing it, including on crash paths | depends on a Python process not dying at the wrong moment — R8 exists to cover part of it | EN raised only while the MCU is itself driving TXD: guarantees the *sequence*, not just the static level |
| Strip the local echo (single-wire) | heuristic matching on content and timing through a buffered tty | byte-exact — the MCU knows what it sent and when |
| RXD low means three different things: dominant bit, bus wake, or VSUP undervoltage (§3.3.2) | "the line alone cannot distinguish them" | the 12V ADC resolves the third case directly |
| 5-baud slow init — 200 ms per bit, up to ~600 ms dominant | termios cannot do 5 baud: drop the UART, re-mux TXD as GPIO mid-protocol, bit-bang, hand the pin back | ordinary firmware |

The last row is an *optimization, not an enabler* — the TLIN1027 was chosen
precisely so slow init is possible without an MCU (§3.3.1, no dominant state
timeout). It is listed because the Linux mechanics are worse than they look, not
because rev A cannot do it.

One addition rather than a fix: with the MCU driving §13.2's commander-pull-up
FET, the 1 kΩ can be enabled **only for the duration of a transaction** instead
of for a whole session. That shrinks the open NT510 question (§13.1 measurement
2) from "does the pull-up disturb a tool session" to "does it disturb one during
our own ~20 ms of talking."

**CAN (U3/U4) — modest, plus one option to record and defer.** The MCU should not
touch the working path: MCP2518FD → mainline `mcp251xfd` → SocketCAN `can0` →
`can_collector.py` is clean and has no problem an MCU solves. The hardware story
is the receive-only RXD fan-out (§13.3.5) plus the standby-wake fix — the
TCAN1042's low-power receiver runs from VIO, so the always-on rail is what makes
wake-on-CAN work at all.

The option worth recording and *not* doing yet: the CANable2 already in the rig
is an **STM32G431 running candleLight/gs_usb** (`docs/canable-firmware.md`), the
build and flash tooling is committed (`scripts/build_canable_fw.sh`,
`firmware/canable2_fw-ba6b1dd.bin`), and the G431 carries three FDCAN
peripherals. An STM32G4-class supervisor could therefore present extra CAN
channels to the Pi as **native SocketCAN over USB on a mainline driver** — a
route back to the multi-bus capability rev A gave up (§10: 2 channels → 1), which
matters for the "generalized to any vehicle" intent in AGENTS.md, since plenty of
vehicles put the interesting traffic on a second bus.

**Deferred for two concrete reasons.** candleLight is a CAN adapter and nothing
else, so combining roles means implementing gs_usb *alongside* the supervisor
firmware rather than reusing the committed binary; and it needs a USB connector
on the HAT plus a cable to a Pi port, where every other interface on this board
arrives through the header. Pick one role for rev C — **supervisor, timebase and
sensors** — and leave CAN alone.

**IMU and barometer (U6/U5) — real gains, and one trap not to walk into.**

> **Do not put the MCU on I2C1.** §5 records the X1202 fuel gauge at **0x36 on
> bus 1**, and `scripts/ups_shutdown_watch.py` / `src/dashboard/ups.py` depend on
> it. Multi-master I2C between Linux and an MCU is a reliability hazard even
> where it is legal, and what it would put at risk here is the UPS shutdown
> path — the one subsystem on the rig that must not become flaky.

The clean split is **a private MCU I2C bus carrying the LSM6DSOX and BMP384
entirely**, leaving Pi I2C1 with the fuel gauge and nothing else. That buys:

- **FIFO offload.** The LSM6DSOX's on-chip FIFO with batching and timestamps,
  drained by the MCU, gives clean 416 Hz–1.6 kHz IMU data without Linux interrupt
  jitter and without spending Pi interrupts — which is what `imu_frame.py`'s
  gravity low-pass and yaw-offset resolution actually want.
- **A possible rescue for the BMP384.** §13.1 measurement 9 asks whether its RMS
  noise supports `derived:GRADE`, holding the footprint-compatible BMP390 swap in
  reserve (§3.5). Oversampling at high ODR on a hardware clock cuts noise as √N,
  which may close the gap and save the swap.
- **Sensors that work while the Pi is off** — motion wake, parked altitude and
  temperature, orientation continuity across a power cycle.
- **R7/R9 stop being a question.** §5's doubled pull-ups (~1.3 kΩ effective,
  ~2.5 mA sink per line) that §8.3 wants depopulated do not exist on a new bus.

**The timing for this is unusually favourable and will not recur.**
`imu_collector.py` does not exist, and §11 records that the barometer has "no
config, collector, or channel." Both collectors would be written against the MCU
link *from the start* rather than migrated onto it later.

**GNSS (U7) — the MCU's rail closes a recorded defect for free.** §3.6:
"**Backup supply has no fallback.** V_BCKP is fed *only* from BT1 through D2. If
the JST connector is empty the pin floats… the receiver cold-starts every boot —
the exact cost PPS exists to avoid," and u-blox expects V_BCKP diode-OR'd with
the main rail. The always-on 3.3V rail **is** the missing OR leg: one diode
closes it, and whether BT1 is fitted stops mattering.

Beyond that:

- The M10's 10 Hz nav rate is volatile configuration (§3.6). The MCU re-applies
  it deterministically at every power-up, before the Pi is alive.
- Keeping the receiver tracking while the Pi is off gives a **hot fix at key-on**
  and — more usefully — gives the MCU valid *absolute* time at the instant of
  wake, which is what makes §13.3.3(a)'s buffer timestamps real rather than
  merely relative.
- §3.6 notes the antenna bias feed has "no bias fuse or short-circuit
  protection." A sense resistor plus one ADC channel gives open/short antenna
  detection, currently neither protected nor observable.

**Debug.** §13.2 wants status LEDs because "in-vehicle debugging currently costs
an SSH round trip." Driven by the MCU they mean something in the case that
matters most — when the Pi is the broken part: no 12V, CAN silent, Pi not
answering the heartbeat, GNSS no fix.

#### 13.3.5 The bus-access rule: receive-only fan-outs

**Every MCU bus connection is a receive-only fan-out. Transmit stays with the
Pi's existing path.**

The TCAN1042's RXD is a push-pull CMOS output, so it drives the MCP2518FD's RXCAN
and an MCU input with no contention. The same holds for K-line RXD (already
pulled up by R3) and the GNSS TX line. The MCU gains full receive-side
visibility — boot-gap buffer, timestamps, wake detection — and is *physically
incapable* of disturbing a vehicle bus or contending with SocketCAN.

This is what preserves the software stack: `mcp251xfd` stays mainline, `can0`
stays SocketCAN, `can_collector.py` is untouched. **The rejected alternative** is
an MCU with an integrated CAN FD controller replacing the MCP2518FD —
electrically tidier (one controller per transceiver, direct frame access) but it
discards the mainline driver and everything downstream of it. Recorded so it is
not re-proposed on the tidiness argument alone.

K-line is the one deliberate exception: §13.3.4 has the MCU owning that UART in
both directions, because the value there *is* the transmit-side sequencing. If
that is judged too large a step, the fallback is an RX-only K-line tap with EN
and TXD left on the Pi — which keeps the echo-stripping and break hazards and
earns none of the §13.3.4 table's guarantees.

#### 13.3.6 Nets the board must carry

Fix this list before layout: a missing net is a re-spin, unused firmware is free.

| # | Net | For |
|---|---|---|
| 1 | **Always-on 3.3V** from `+12V_RAW`, nA-class-Iq regulator | MCU + TCAN1042 VIO (wake) + GNSS V_BCKP OR leg (§13.3.2) |
| 2 | 12V divider → MCU ADC | crank/alternator/parasitic (§13.3.3b); K-line undervoltage disambiguation |
| 3 | MCU GPIO → high-side load switch on the J3 leg | §13.3.1 sleep/wake |
| 4 | MCU ↔ Pi UART pair | handshake; works whenever the MCU is up |
| 5 | MCU ↔ Pi on **SPI0 CE1 (GPIO7, pin 26 — free, §4.1)** | bulk buffer dump; a few hundred KB over UART is painful |
| 6 | Pi 3V3 rail sense → MCU | the tri-state invariant in §13.3.2 |
| 7 | TCAN1042 RXD → MCU (fan-out) | boot-gap capture, wake, timestamps |
| 8 | K-line RXD / TXD / EN → MCU | §13.3.4; supersedes the Pi's `uart0` connection |
| 9 | GNSS TX + TIMEPULSE → MCU (fan-out) | time base, hot fix, absolute time at wake |
| 10 | **LSM6DSOX INT2 (pin 9 — unconnected on rev A)** → MCU | motion wake |
| 11 | Private MCU I2C → LSM6DSOX + BMP384 | §13.3.4 — **must not be I2C1** |
| 12 | GNSS antenna bias sense → MCU ADC | open/short antenna detection |
| 13 | STB (U4) and the §13.2 pull-up FET gate → MCU | safe-state ownership |
| 14 | **A flash path from the Pi** — SWD bit-banged from Pi GPIO, or a UART bootloader with BOOT0 on a Pi-drivable pin | see below |
| 15 | Status LEDs → MCU | §13.2, §13.3.4 |

**Item 14 is a hard requirement, not a convenience.** If updating firmware means
pulling the board out from under the dash, firmware stops being updated, and
every capability above freezes at whatever shipped.

**Items 4, 5 and 6 are the crossing nets** that §13.3.2 governs — size their
series resistance there, not here.

Header budget: §4.1 lists GPIO27, 22, 23, 24, 7, 12, 13, 19, 20 and 21
unconnected, so items 3–6 fit without disturbing anything, and `uart4`
(GPIO12/13) is the natural home for item 4. Note that item 8 *frees* `uart0`
(GPIO14/15) on the Pi side — a net simplification of §11's K-line software work
rather than an addition to it.

Physical: 65 × 56mm already carries 48 footprints. An MCU, an always-on buck,
crossing-net components and a programming header are not free there. Swapping
Y1's HC49/US can (**11.4 × 4.7 × 3.3mm**, already flagged in §3.1.1 as
"physically large" against a 3.2 × 2.5mm SMD equivalent) is worth doing on its
own merits and helps here.

#### 13.3.7 Deliberately out of scope for the MCU

Recorded with reasons, because each is individually tempting:

- **The OBD scheduler and ISO-TP.** Hard-real-time sweep scheduling would deliver
  `docs/roadmap/capture-profiles.md` Phase 5.c cleanly, and 5.c is deferred there
  precisely because it means "a second scheduling mode inside `run_can_obd_loop`,
  which already carries backoff, DID windows, budget pacing, freeze frames and
  DTC polling." **But the measurements say Linux is not the constraint:**
  `docs/data-architecture.md` records 85 single-PID requests/s and 33.9 Hz per
  channel batched, with the ECM answering in ~1.5 ms, against a live rate of ~8
  req/s — and names the cause outright, `BUS_BUDGET_MULTIPLIER` × `poll_rate_hz`,
  "a conservative heuristic, not a measured limit." Fix the constant in Python
  before building hardware to route around it. Moving ISO-TP, the poll plan, DID
  session gating and per-vehicle profile logic into C would duplicate tested code
  and put vehicle-specific behaviour where `pytest` cannot reach it.
- **Decode and bindings.** Byte → quantity stays in Python against
  `data/bindings.yaml`, gated by `compare_channels` and
  `tests/test_analysis_ground_truth.py`. A second decode implementation in
  firmware would be a second source of truth for exactly the class of claim this
  project has retracted six times.
- **Any inference or detection.** Obvious, but stated so it stays stated.
- **The gs_usb CAN-adapter role** — §13.3.4, deferred with reasons.
- **Anything that writes ECU non-volatile memory.** AGENTS.md's one hard line
  applies to the MCU identically. An MCU with its own transmit path onto the
  vehicle bus is precisely the component that could quietly acquire that
  capability, and §13.3.5's receive-only rule is also what prevents it.

#### 13.3.8 Rejected: PCIe as the data transport

Considered and rejected 2026-08-11. Recorded because the Pi 5's exposed PCIe is a
visible unused resource and the idea will otherwise be re-derived.

**The bandwidth case fails by three orders of magnitude.** The bus is 500 kbit/s
classic CAN (`docs/can-setup.md`). At 100% load that is ~4000 frames/s, each
costing ~30 bytes of SPI traffic out of the MCP2518FD's RX FIFO — ~120 kB/s ≈
1 Mbit/s against a 20 Mbit/s SPI link, about 5% utilisation. GNSS at 10 Hz is a
few kB/s and the IMU single-digit kB/s. Total ingest is under ~200 kB/s against
PCIe Gen2 x1's ~500 MB/s.

**The header data already crosses a PCIe link.** Every GPIO, SPI, UART and I2C
line on a Pi 5 hangs off the RP1 southbridge, which reaches the BCM2712 over an
internal PCIe link. The only non-PCIe segment is the 20 MHz SPI hop — the
5%-utilised one. There is no congested link to relieve.

**Latency would not improve either.** A frame read over SPI is ~15–20 µs;
IRQ-to-handler latency on a stock Pi 5 kernel is tens to hundreds of µs, so the
transport is not the dominant term. Timestamp quality — the thing actually worth
wanting — comes from the MCP2518FD's own time base counter (`mcp251xfd` drives
hardware RX timestamps from it; confirm at bring-up) and from §13.3.3(c), not
from PCIe.

**Costs, all real:** no drop-in PCIe CAN part exists, so it means an FPGA or a
PCIe-endpoint MCU plus a custom kernel driver replacing a mainline one; 5 GT/s
differential routing on a board whose single 1.575 GHz launch already cost two
rounds and a custom DRU rule (§3.6.1); the Pi 5's PCIe is a 16-pin FFC rather
than header pins, so the board stops being a HAT; and it consumes the only x1
lane, which is worth far more as **NVMe** — storage for the Parquet ring buffer,
training corpus and session directories, against the microSD/USB-SSD plan in
`docs/CANary-overview.md`.

**What would change the answer:** a front end of a different character — raw
analogue sampling of CANH/CANL at MS/s for transceiver fingerprinting or
physical-layer intrusion detection, or sub-bit timing analysis. Two channels at
10 MSa/s × 12-bit is ~30 MB/s, where a fast link starts to matter. That is
outside anything rev A/B/C contemplates, and even then USB 3.0 carries it — the
FPGA front end would be the hard part, not the link.
