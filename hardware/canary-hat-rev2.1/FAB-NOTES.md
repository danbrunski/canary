# CANary-HAT — fabrication and assembly notes

Send this with the gerbers. It states the things the design depends on that the
gerbers alone do not communicate.

> **Revision 2.1.** The second board built. Rev 2.0 was fabricated and
> assembled by PCBWay; changes since then are recorded in
> `docs/CANary-HAT-Rev2.1.md` in the CANary repo. Rev 2.1 replaces the ADC
> with an 8-channel ADS8688A (U8), adds a 5 V sensor supply (U9) and a 13-way
> terminal block (J2), removes the barometer, and moves the GNSS receiver and
> antenna connector onto a tab. Nothing new in it is verified against hardware
> yet.

## Board

| | |
|---|---|
| Size | **87.0 × 56.0 mm overall, not a rectangle** — see *Outline* |
| Layers | 4 |
| Thickness | 1.6 mm |
| Copper | 1 oz all layers |
| Layer order | F.Cu / **In1.Cu = GND plane** / **In2.Cu = +3V3 plane** / B.Cu |
| Vias | Through-hole only, **all 0.60 mm pad / 0.30 mm drill**. No blind, buried, or microvias |
| Min track | 0.15 mm (rule minimum; the narrowest routed track is 0.20 mm) |
| Min spacing | 0.15 mm; **0.14 mm inside U6's LGA-14 land pattern only**, as in rev 2.0 |
| Min hole-to-hole | 0.25 mm |
| Copper to edge | **0.30 mm** (rev 2.0 was 0.05 mm) |
| Copper to hole | **0.25 mm**, including both inner planes at the four NPTH mounting holes |
| Drills | 0.30 mm vias; 0.8 mm C2; 1.0 mm J1; 1.2 mm J2; **2.7 mm NPTH × 4** (MH1–MH4) |

## Outline

Route to Edge.Cuts. The board is the 65 × 56 mm Raspberry Pi HAT outline with
two changes:

- **A 22 × 20 mm tab** extends the bottom-right of the board to 87 mm overall.
  It carries the GNSS receiver (U7), its antenna connector (J4) and the backup
  cell connector (BT1). The tab's inside corner has a 3 mm radius.
- **A 5 × 17 mm notch** in the left edge for the Pi's display cable. **Its two
  inside corners have a 0.5 mm radius.** If your router needs a larger
  radius there, a larger radius is acceptable; tell us what you will cut.

The outer corners have a 3 mm radius.

## Stackup — this one matters

**The outer dielectric (F.Cu → In1.Cu) must be 0.1 mm nominal.**

A 0.20 mm trace on F.Cu referenced to In1.Cu is the 50 Ω GNSS antenna feed at
1.575 GHz, from U7 pin 11 to the J4 U.FL receptacle (about 4.4 mm). Its
impedance depends on the outer dielectric thickness, and a substituted stackup
is not visible in the gerbers:

| F.Cu → In1.Cu dielectric | Resulting Z₀ |
|---|---|
| **0.1 mm (required)** | **~45 Ω** — correct |
| 0.2 mm | ~66 Ω — antenna feed detuned |

Controlled-impedance fabrication is **not** required and should not be
quoted: across normal FR4 variation (εr 4.0–4.6) and ±10 % prepreg thickness
the line stays at 42–48 Ω, which is fine. Only the *nominal thickness* needs
holding. The project's stackup is 0.035 / **0.1 prepreg** / 0.035 / 1.24 core
/ 0.035 / **0.1 prepreg** / 0.035 mm.

Please confirm the stackup you intend to build, including the prepreg
dielectric constant, before starting.

## Surface finish

**ENIG preferred.** U6 (LSM6DSOX, LGA-14, 0.5 mm pitch), U8 (ADS8688A,
TSSOP-38, 0.5 mm pitch), U7 (MAX-M10M, LCC module) and Y1 (3.2 × 2.5 mm
no-lead crystal) need flat pads. HASL leaves domed, uneven pads that hurt
coplanarity and placement on fine-pitch and leadless parts.

## Solder mask

Board default expansion, except:

- **U6 (LSM6DSOX LGA-14):** the land pattern is the ST TN0018 size
  (0.350 × 0.575 mm), so use **+0.025 mm per side** expansion: openings
  0.40 × 0.625 mm, leaving 0.10 mm mask dams. This is carried in the footprint
  itself (`solder_mask_margin 0.025`), not just in this note. If 0.10 mm dams
  are below your minimum, **tell us and we will drop to zero expansion**
  rather than have the dams silently ganged.
- **Fiducials:** 1 mm copper, 2 mm mask opening, no silkscreen within the
  opening.

## Fiducials

Three global fiducials, top side, `Fiducial_1mm_Mask2mm`. FID2 has moved
from its rev 2.0 position onto the GNSS tab, so the three span the whole
board:

| Ref | Position (mm) |
|---|---|
| FID1 | 103.50, 52.50 |
| FID2 | 183.20, 82.10 |
| FID3 | 103.50, 91.25 |

They are board-only footprints (no schematic symbol) and are excluded from the
BOM. Top-side only is intended: every assembled part is on F.Cu.

## Harness pinout — changed again in rev 2.1

**J2 is now one 13-way terminal block**, Phoenix `PT 1,5/13-3,5-H` (order
number 1984727). Rev 2.0's
separate J3 (the UPS feed) is folded in and OBD pin 5 is dropped. **A rev 2.0
harness does not fit this board.**

| J2 pin | Signal | Wire |
|---|---|---|
| 1 | +12V in (OBD 16) | 18 AWG |
| 2 | GND, chassis (OBD 4) | 18 AWG |
| 3 | +12V out to the UPS | 18 AWG |
| 4 | GND out to the UPS | 18 AWG |
| 5 | CAN High (OBD 6) | 20 AWG |
| 6 | CAN Low (OBD 14) | 20 AWG |
| 7 | K-line (OBD 7) | 20 AWG |
| 8 | Sensor GND | 20 AWG |
| 9 | Sensor voltage input | 20 AWG |
| 10 | Sensor resistive input | 20 AWG |
| 11 | 5 V sensor supply | 20 AWG |
| 12 | Fuel-pressure gauge − | 20 AWG |
| 13 | Fuel-pressure gauge + | 20 AWG |

Nothing for the assembler to do: the wiring is ours. It is recorded here
because this file travels with the board.

## Bench use — CAN termination

The board carries **no CAN termination**, deliberately: a vehicle bus is
already terminated at both ends and a third terminator is a fault.

For bench work against an unterminated bus, **clamp a 120 Ω resistor across J2
pins 5 and 6** (CAN High / CAN Low). On rev 2.0 these were pins 4 and 5; on
rev 2.1 pin 4 is the UPS ground. Remove the resistor before the board goes back
in a vehicle.

## Assembly

**Single-sided.** Every assembled part is on the top side; the only bottom-side
footprint is J1, which is not fitted (below). One reflow pass is all the board
needs. Please keep it to one: U8's internal voltage reference shifts a little
with each reflow (TI SBAS680).

**Machine placement requested for U6, U7, U8, U9 and Y1** at least. Their pads
are hidden or at 0.5 mm pitch.

**Pin 1 orientation — please check against the assembly drawing for U6, U7,
U8, U9 and Y1.**

- **U6 and U7** are third-party library imports (SnapEDA / UltraLibrarian)
  whose zero-rotation convention may not match IPC-7351, and therefore may not
  match your machine's expectation. **Rotation error on those parts is the most
  likely defect on this board.** Query anything that looks inconsistent rather
  than assuming the centroid file is right.
- **Y1 is a 4-pad crystal.** A 90° rotation puts the crystal on its ground
  pads and shorts its two signal pins through the metal lid.

**Not fitted at the assembler:**

| Ref | Why |
|---|---|
| **J1** | 2×20 stacking header with a **13.5 mm body**, bottom side. **We supply and fit it ourselves.** Rev 2.0 was fitted with a standard-height header, which does not clear the Pi 5 cooler; leave the footprint empty. |
| **R21, R24** | Optional resistor positions, deliberately left empty. |

**Through-hole: J2 (13 joints) and C2 (2 joints).** Please quote them
separately. **Please source and fit C2** (Panasonic `EEU-FR1H470`, 47 µF
50 V). Rev 2.0 asked you to leave it for us; rev 2.1 does not. J4 is now a
surface-mount U.FL receptacle with no through-hole ground posts.

**Do not wash the board, and no ultrasonic cleaning.** u-blox's integration
manual for U7 (MAX-M10M) forbids water, solvent and ultrasonic cleaning: water
wicks under the module, and ultrasound permanently damages its quartz
oscillators. Please use no-clean paste.

**Moisture sensitivity:** U7 is MSL 4 and U8 is MSL 2. Bake per J-STD-033 if
their floor life has run out.

**Not components:** JP1 is a solder jumper, a PCB feature; leave it open.
FID1–FID3 are fiducials and MH1–MH4 are mounting holes. BT1's BOM line is the
JST connector only; no cell is fitted.

## Parts that must not be substituted

These have silent failure modes: the board will assemble and look correct while
not working.

| Ref | Part | Why |
|---|---|---|
| **Y1** | **Kyocera `CX3225SA40000D0PTWCC`** | 40 MHz **fundamental**, 8 pF load, ESR ≤ 50 Ω. The load capacitors (C33/C34, 8.2 pF) and the oscillator's start-up margin are sized for exactly this part; a different load capacitance, a higher ESR or an overtone crystal may not start. **The only accepted alternate is ECS `ECS-400-8-33Q-JES-TR`**, and please tell us if you use it. |
| **U1** | **CAT24C256WI-GT3** (onsemi) | WP and A0/A1/A2 are left floating and rely on onsemi's internal pull-downs. Microchip's 24LC256 requires WP tied to a rail. |
| **D2, D5** | **1N4148W** (silicon, SOD-123) | **Must not be Schottky.** D5 blocks reverse current from a live K-line into an unpowered supply. D2 blocks reverse current into BT1, a **primary cell with no charge path**: Schottky leakage is orders of magnitude higher and roughly doubles per 10 °C, so a hot parked cabin would trickle-charge a non-rechargeable cell. One line item, quantity 2. |
| **FL1** | **Coilcraft `1812CAN-513NRC`** | CAN common-mode choke. The board relies on this part's pin numbering: both windings start at pins 1 and 4, at the same end. A substitute numbered differently puts the two windings in opposition, so it becomes a 51 µH series choke on the CAN signal, which shows up only as a bus that misbehaves. |
| **L1, C16** | **Murata `LQG18HN27NJ00D`, Yageo `CC0402JRNPO9BN150`** | The antenna's bias feed and DC block. Both values are chosen for their self-resonance at 1.575 GHz, and L1 must carry 300 mA or more. A same-value part of a different construction can detune the antenna feed. |

If any of the above is unavailable, please contact us before substituting.

## Ratings are deliberate

Voltage, power and dielectric ratings on the BOM are not defaults. Please do
not down-rate any line to a cheaper equivalent. In particular:

- **Every 100 nF 0603 is 50 V.** C3 sits on the 12 V rail, whose transient
  clamp (D1) allows up to 38.9 V.
- **C2 47 µF, C19 1 µF and C20 4.7 µF are 50 V** for the same reason. C2 is
  Panasonic `EEU-FR1H470` or Nichicon `UPW1H470MED`. A 25 V part fits the
  footprint and fails on the first load dump.
- **C4 220 pF is 100 V C0G.** It sits on the K-line bus.
- **R5 and R6 are 0.2 W 0603** (Panasonic `ERJ-PA3F1001V`). R5 dissipates
  ~150 mW in a bus fault, over a standard 0603's 0.1 W.
- **C21/C22 are 22 µF 25 V X7R 1210, and C24/C27 are 10 µF 16 V X7R 0805.**
  They are the ADC's reference and supply capacitors; X5R loses too much
  capacitance under bias and is rated only to 85 °C.
- **C16, C32, C33, C34 and C36 are C0G.**

## Also required, not on the board BOM

See `BOM-offboard.md`, most importantly:

- a **primary cell for BT1**, with its + lead on connector pin 2. That is
  **the reverse of rev 2.0**, so meter every lead before fitting;
- the U.FL-to-SMA pigtail, and an active antenna with a SAW filter ahead of its
  LNA;
- the 13.5 mm J1 stacking header;
- ferrules for every J2 wire.
