# CANary-PWR — fabrication and assembly notes

Send this with the gerbers. It states what the design depends on that the gerbers, BOM and
position file do not show.

> **Revision 1.0, the first board built.** The design record is `docs/CANary-PWR-Spec.md` in the
> CANary repo. Nothing on this board has been verified on hardware yet.

## Board

| | |
|---|---|
| Size | 65.0 × 56.0 mm, rectangular, 3 mm corner radii (the Raspberry Pi HAT outline) |
| Layers | 4 |
| Thickness | 1.6 mm |
| Copper | 1 oz on all layers |
| Layer order | F.Cu / **In1.Cu = GND plane** / In2.Cu = GND with +12 V and +5 V islands, and some signals / B.Cu |
| Vias | Through-hole only: 0.60 mm pad / 0.30 mm drill, and 0.80 / 0.40 mm on the 12 V power nets. No blind, buried or micro vias. **No via sits in a pad, so no plugged or filled vias are needed.** All vias tented |
| Min track | 0.15 mm |
| Min spacing | 0.15 mm. **0.13 mm is allowed inside the land patterns of Q2–Q5, Q7, U1–U4 and U8.** The narrowest gap between two pads is 0.14 mm, gate to source on the VSON-8 FETs |
| Min hole-to-hole | 0.25 mm |
| Copper to edge | 0.30 mm |
| Copper to hole | 0.25 mm |
| Drills | Vias 0.30 and 0.40 mm. PTH 0.65 mm (J5), 1.0 mm (J1), 1.02 mm (J3, J4), 1.2 mm (J2). NPTH 2.7 mm × 4 (MH1–MH4) and 3.0 mm × 3 (the J3 and J4 locating pegs) |

**Stackup.** No impedance control is needed, so any standard 4-layer 1.6 mm build is fine. The
project file carries HAT rev 2.1's: 0.035 / 0.1 prepreg / 0.035 / 1.24 core / 0.035 / 0.1
prepreg / 0.035 mm.

## Surface finish

**ENIG preferred.** Several parts need flat pads, which HASL does not give:

- U1, U2 and U3 have 0.5 mm pitch.
- U1, U2 and the five VSON-8 FETs (Q2–Q5, Q7) are leadless.
- U6 has an exposed pad.

## Solder mask

- **Openings are 1:1 with the pads,** with no expansion.
- **The narrowest dam is 0.14 mm,** between gate and source on Q2–Q5 and Q7. If that is below
  your minimum, tell us before the dams are ganged.
- **Fiducials:** 1 mm copper, 2 mm mask opening.

## Fiducials

Three per side, `Fiducial_1mm_Mask2mm`. Positions are in the position file's frame: origin at
the board's bottom-left corner, y up.

| Ref | Side | Position (mm) |
|---|---|---|
| FID1 | top | 61.5, 46.5 |
| FID2 | top | 61.5, 9.0 |
| FID3 | top | 2.0, 39.2 |
| FID4 | bottom | 35.0, 47.7 |
| FID5 | bottom | 61.5, 9.0 |
| FID6 | bottom | 2.0, 39.2 |

They are board-only footprints, excluded from the BOM. MH1–MH4 are mounting holes, not parts.

## Assembly

**Double-sided SMD: 72 parts on the top, 45 on the bottom.**

- **Bottom side:** U5 (LQFP-32), U6 (HVSSOP-8 with exposed pad), U7 (SOIC-14), U8 (VSSOP-8),
  F1 (1812), the 2512 resistors R16–R20, and small diodes, a SOT-23 and 0603/0402 passives.
- **Top side:** the tall and heavy parts: L1 and L2 (6.6 × 6.4 mm inductors), the C1, C18 and C19
  electrolytics (6.3 × 7.7 mm) and Q1 (DPAK).
- **The electrolytics are EEE-FK1H470XP,** the 6.3 × 7.7 mm can. EEE-FK1H470P, without the X, is
  the same value in an 8 × 6.2 mm can and does not fit the pads.

**No through-hole assembly.** J1–J5 are the board's only through-hole parts, and none is fitted
at the assembler (below). None of them is in the BOM or the position file. No wave or selective
soldering is needed.

**Not fitted at the assembler.** The operator sources and fits all five after assembly:

| Ref | Part |
|---|---|
| **J1** | 2 × 20 Raspberry Pi stacking header with a 13.5 mm body, on the bottom side |
| **J2** | Phoenix Contact MC 1,5/4-G-3,81 header |
| **J3** | Molex Micro-Fit 3.0 2 × 3 right-angle header (43045-0600) |
| **J4** | Molex Micro-Fit 3.0 2 × 5 right-angle header (43045-1002) |
| **J5** | 10-pin 1.27 mm SWD header, part not yet chosen |

**U5 (STM32G031K8T6) is supplied blank.** No programming or testing at assembly.

**Pin 1 and rotation.**

- **Pin 1 at 0°.** Every IC footprint has pin 1 at the top left at 0° rotation, KiCad's library
  convention. The four vendor-imported footprints follow it too (checked 2026-10-20): U1 (Ultra
  Librarian), U2 (SnapMagic), U6 and U8 (Ultra Librarian).
- **Rotations** in the position file are KiCad's. Please check each part against its silkscreen
  pin-1 mark, and query anything that looks inconsistent rather than trusting the position file.
- **Polarised parts:** D1–D8 and the electrolytics C1, C18 and C19.
- **D2 is a unidirectional TVS.** Fitted backwards, it shorts the 12 V input.

**Footprint names that do not match the MPN.**

- **U2:** the footprint is named after TI's LM61460**AFS**. The BOM's LM61460**AAS** is the
  intended part, and the two share a land pattern.
- **U6:** the footprint is named after TI's TPS7A1633**A**QDGNRQ1. The BOM's TPS7A1633QDGNRQ1,
  without the A, is the intended part, and the two share a land pattern.
- **U8:** the footprint `SOIC8_DGK_TEX` is TI's DGK (VSSOP-8) land pattern.

Please do not change any of these MPNs to match its footprint name.

**Cleaning.** Please use no-clean paste and do not wash the board. SW1 is a tactile switch, and
nothing else here needs washing.

## Parts that must not be substituted

These parts fail silently: the board assembles, looks correct and works on the bench.

| Ref | Part | Why |
|---|---|---|
| **D4** | Diodes Inc 1N4148W-7-F, PN switching diode | The LTC3350's bootstrap diode. **Must not be Schottky:** the datasheet warns that with a Schottky, BST–SW pumps past 5.5 V when the step-up runs non-synchronously |
| **D5, D6** | Diotec BAT46WS-AQ, 100 V Schottky | Diode-OR into the always-on regulator. One leg can sit at 0 V while the other sees the input clamp's 38.9 V, so a 30 or 40 V Schottky fails at the first load dump |
| **F1** | Bourns MF-MSMF050/40X-2, 40 V | Resettable fuse on the 12 V bus, which can reach 38.9 V. Lower-voltage 0.5 A 1812 fuses fit the same pads |
| **R13** | Yageo RT0603BRD0710KL, 0.1 % thin film | Bias resistor for the cap bank's NTC. A 1 % part shifts the temperature reading |
| **U2** | TI LM61460AASQRJRRQ1 | AAS is auto mode with spread spectrum. The AFS (forced PWM) and AAN (no spread spectrum) variants fit the same footprint |
| **U6** | TI TPS7A1633QDGNRQ1 | The always-on 3.3 V regulator. Its pin 2 is tied to GND, which this part allows. The A variant, TPS7A1633AQDGNRQ1, fits the same footprint, and its datasheet forbids it |

If any of these is unavailable, please contact us before substituting.

## Ratings are deliberate

Please do not down-rate any line to a cheaper equivalent. Each passive's BOM Value states its
rating, for example `100nF 50V X7R`, `10k 0.1%`, `8mOhm 1% 1W` or `4.7uH Isat 14.4A`.

- **The 12 V side is rated for the input clamp.** D2 (SMBJ24A) clamps at up to 38.9 V, so every
  capacitor on a 12 V net is 50 V, and the CSD18543Q3A FETs are 60 V. The 25 V and 10 V
  capacitors are on rails of 10 V or less.
- **R14, R15 (8 mΩ) and R28 (5 mΩ) are Vishay WSL2512 metal-strip current-sense resistors, 1 W.**
  The LTC3350's 4 A limits and the INA226's reading scale with them.
- **R16–R20 are 2.7 Ω, 1 W, 2512.** Each carries 0.5 W while the cell shunts run.
- **L1 and L2 are Würth 744393465047** (WE-XHMI Performance 6060), 4.7 µH with a 14.4 A
  saturation current. L2's must be at least 11.5 A, the buck's high-side current limit, and
  L1's must exceed 7.2 A.
- **C12 (120 pF) and C25 (22 pF) are C0G.**

## Order-form note

For the assembly-notes box on the order form (under 600 characters):

```text
Double-sided SMD: 72 parts top, 45 bottom. DO NOT FIT J1-J5 (through-hole; we fit them), so no
through-hole soldering. U5 (STM32) is supplied blank: no programming. Do not substitute D4 (PN
diode, not Schottky), D5/D6 (100 V Schottky), F1 (40 V PTC), R13 (0.1% thin film), U2 (LM61460
AAS, footprint named AFS) or U6 (TPS7A1633Q, not the A variant its footprint is named after). Do
not down-rate capacitor voltages. Every IC footprint has pin 1 top-left at 0 deg; please query
anything inconsistent. ENIG preferred. Full notes in FAB-NOTES.md.
```

## Files

- **BOM and position file.** The BOM is `BOM.csv`: 54 lines and 117 parts, every one of them
  fitted at the assembler. J1–J5 are excluded from it and from the position file, which lists the
  117 parts and the six fiducials. Both files are exactly what PCBWay's KiCad plugin uploads, and
  `hardware/tools/pcbway_bom.py` writes both from the board. The Value and MPN columns carry
  everything a line needs. The `KiLib_Generator` column is KiCad library metadata, not a part
  attribute.
- **Not on the board BOM.** The operator supplies J1–J5 and their mating parts. Spec §P10 items 10
  and 12 have the open ones. The cap-bank lead is in `hardware/tools/capbank_lead.py`.
