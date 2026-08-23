# CANary-HAT — fabrication and assembly notes

Send this with the gerbers. It states the things the design depends on that the
gerbers alone do not communicate.

> **Revision 2.0.** Rev 1.0 was quoted (PCBWay T-B4W341056A, 2026-08-14) but never
> fabricated; the quote stalled and the submission was withdrawn 2026-08-22. This
> is therefore the **first board built**, not a second spin, and everything here
> is unverified against hardware. Changes from the quoted rev 1.0 are listed in
> `docs/CANary-HAT-Revisions.md` §13.2 in the CANary repo.

## Board

| | |
|---|---|
| Size | 65.0 × 56.0 mm |
| Layers | 4 |
| Thickness | 1.6 mm |
| Copper | 1 oz all layers |
| Layer order | F.Cu / **In1.Cu = GND plane** / **In2.Cu = +3V3 plane** / B.Cu |
| Vias | Through-hole only. No blind, buried, or microvias |
| Min track | 0.15 mm |
| Min via | 0.40 mm pad / 0.30 mm drill, 0.15 mm annular ring |
| Min hole-to-hole | 0.25 mm |
| Copper to edge | 0.05 mm |

## Stackup — this one matters

**The outer dielectric (F.Cu → In1.Cu) must be 0.1 mm nominal.**

A 0.20 mm trace on F.Cu referenced to In1.Cu is a 50Ω GNSS antenna feed at
1.575 GHz. That impedance depends on the outer dielectric thickness, and a
substituted stackup is not visible in the gerbers:

| F.Cu → In1.Cu dielectric | Resulting Z₀ |
|---|---|
| **0.1 mm (required)** | **~45 Ω** — correct |
| 0.2 mm | ~66 Ω — antenna feed detuned |

Controlled-impedance fabrication is **not** required and should not be quoted —
the tolerance band across normal FR4 variation (εr 4.0–4.6) and ±10% prepreg
thickness is 42–48 Ω, which is fine. Only the *nominal thickness* needs holding.

Please confirm the stackup you intend to build, including the prepreg dielectric
constant, before starting.

## Surface finish

**ENIG preferred.** U5 (LGA-10, 0.5 mm pitch) and U6 (LGA-14, 0.5 mm pitch) are
leadless parts whose solder joints are their only mechanical attachment. HASL
leaves domed, uneven pads that hurt coplanarity and placement on fine-pitch LGAs.

## Solder mask

Board default expansion, except:

- **U6 (LSM6DSOX LGA-14):** the land pattern **is** the ST TN0018 size
  (0.350 × 0.575 mm) as of rev 2.0, so use **+0.025 mm per side** expansion —
  openings 0.40 × 0.625 mm, leaving 0.10 mm mask dams. This is carried in the
  footprint itself (`solder_mask_margin 0.025`), not just in this note. If
  0.10 mm dams are below your minimum, **tell us and we will drop to zero
  expansion** rather than have the dams silently ganged.
- **Fiducials:** 1 mm copper, 2 mm mask opening, no silkscreen within the opening.

## Fiducials

Three global fiducials, top side, `Fiducial_1mm_Mask2mm`:

| Ref | Position (mm) |
|---|---|
| FID1 | 103.50, 52.50 |
| FID2 | 161.50, 54.70 |
| FID3 | 103.50, 91.25 |

They are board-only footprints (no schematic symbol) and are excluded from the
BOM. Top-side only is intended — every placed component is on F.Cu.

## Harness pinout — changed in rev 2.0

**J2 pins 4–6 were re-assigned.** A harness built to the rev 1.0 pinout will land
CAN High on the K-line pin.

| J2 pin | Signal | OBD-II pin | rev 1.0 was |
|---|---|---|---|
| 1 | +12V | 16 | *unchanged* |
| 2 | GND (signal) | 5 | *unchanged* |
| 3 | GND (chassis) | 4 | *unchanged* |
| **4** | **CAN High** | **6** | K-line |
| **5** | **CAN Low** | **14** | CAN High |
| **6** | **K-line** | **7** | CAN Low |

Nothing for the assembler to do — J2 is a 6-way terminal block and the wiring is
ours. Recorded here because this file travels with the board.

## Bench use — CAN termination

The board carries **no CAN termination**, deliberately: a vehicle bus is already
terminated at both ends and a third terminator is a fault.

For bench work against an unterminated bus, **clamp a 120 Ω resistor across J2
pins 4 and 5** (CAN High / CAN Low). J2 is a screw terminal block, so this needs
no tooling and no harness accessory. Remove it before the board goes back in a
vehicle.

## Assembly

**Machine placement requested for:** U5 (BMP384, LGA-10), U6 (LSM6DSOX, LGA-14),
U7 (MAX-M10M-00B, LCC module). Everything else is hand-soldered.

**Pin 1 orientation — please check against the assembly drawing.** The footprints
for U5, U6 and U7 are third-party library imports (SnapEDA / UltraLibrarian)
whose zero-rotation convention may not match IPC-7351, and therefore may not
match your machine's expectation. **Rotation error on those three parts is the
most likely defect on this board.** Query anything that looks inconsistent rather
than assuming the centroid file is right.

**J1** is a 2×20 stacking header with a **13 mm insulator**, mounted on the
**bottom** side (B.Cu). It is the only bottom-side part.

## Parts that must not be substituted

These have silent failure modes — the board will assemble and look correct while
not working.

| Ref | Part | Why |
|---|---|---|
| **D2** | **1N4148W** (silicon, SOD-123) | Must not be a Schottky. It blocks reverse current into BT1, a **primary cell with no charge path**; Schottky leakage is orders of magnitude higher and roughly doubles per 10 °C, so a hot parked cabin would trickle-charge a non-rechargeable cell. Numbers, from the datasheets in `datasheets/`: the 1N4148W leaks **< 25 nA at 25 °C / 20 V** and **< 30 µA at Tj = 150 °C / 20 V**, and the reverse bias across D2 here is under a volt, so actual leakage sits far below even the 25 nA figure. The backup load is **32 µA** (MAX-M10M hardware backup mode, V_BCKP = 3.3 V), not the ~20 µA previously stated here; the ~0.4–0.5 V drop at that current is accepted, not an oversight. **Identical part to D5 — one line item.** |
| **Y1** | **ABLS2-40.000MHZ-D4YF-T** | The **`F`** selects fundamental mode. At 40 MHz the ABLS2 *standard* product is 3rd overtone, which will not oscillate in the MCP2518FD's Pierce circuit. The `D` selects −40/+85 °C; standard is 0/+70 °C. Order by full part number only — **the footprint is named `XTAL_ABLS2-12.000MHZ-D4Y-T`, which is a misnomer from the imported library.** |
| **U1** | **CAT24C256WI-GT3** (onsemi) | WP and A0/A1/A2 are left floating and rely on onsemi's internal pull-downs. Microchip's 24LC256 requires WP tied to a rail. |
| **D5** | **1N4148W** (silicon) | Must not be a Schottky. It blocks reverse current from a live K-line bus into an unpowered VSUP, and Schottky leakage defeats that. |

## Resolved since the last quote — C2

**C2 was quoted as 100 µF/50 V into a Ø6.3 mm / 2.5 mm-pitch land, which is not a
purchasable part.** The smallest standard can for 100 µF at 50 V is ~10 mm on
5 mm pitch. The 50 V rating cannot be reduced — D1 (SMBJ24A) clamps at ~38.9 V,
so a 25 V part fits the footprint and fails on the first load dump.

**Fixed in rev 2.0 by changing the value, not the footprint: C2 is now 47 µF/50 V**
(Panasonic `EEU-FR1H470`, or Nichicon `UPW1H470MED`), which is the largest
standard 50 V value in this can. The footprint was kept because 6.3 mm is the
right size for a rail carrying tens of mA, and the capacitance is not
load-bearing here — C3 is the part the TLIN1027 datasheet actually requires, and
D1 does the transient clamping either way.

**C2 remains operator-sourced — do not populate at the assembler.**


## Sourcing status

Procurement state for every line lives in `QUOTE-PCBWay-T-B4W341056A.md` and in
the `MPN` / `Manufacturer` / `Cost_5u` / `Quote` fields on each schematic symbol.
`grep -l OPEN *.kicad_sch` lists what is still unresolved with the vendor.

**C2, J1 and J5 are operator-sourced — do not populate at the assembler.**

**R7 and R9 are DNP** — the footprints are on the board deliberately, but they
must be left unpopulated. They are duplicate I2C pull-ups; the host already
provides them. See the BOM note on that line.

## Also required, not on the board BOM

See `BOM-offboard.md` — most importantly a **2.54 mm shunt for J5** (a bare
header does nothing on its own) and a **primary cell** for BT1 (there is no
charge path on the board).
