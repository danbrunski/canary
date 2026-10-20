# CANary cap bank rev 1.0

Four 25 F cells in series for CANary-PWR's LTC3350 hold-up, on a lead
(design record: [`docs/CANary-PWR-Spec.md`](../../docs/CANary-PWR-Spec.md) §P4).
The board has no active parts: four cells, an NTC, two Kelvin net ties and a
Micro-Fit 2 × 5 header. It is 87 × 55.5 mm, 2 layers, 1.6 mm, 1 oz, with four
M3 holes.

State 2026-10-20: ERC clean at every severity. DRC is clean with schematic
parity and 0 unconnected, and `via_in_pad.py` reports 0. Both ends of the lead
match its pin map (`hardware/tools/check_capbank_lead.py`).

## Files

| File | What |
|---|---|
| `canary-capbank.kicad_{pro,sch,pcb}` | The project, generated. No hand edits yet, so `gen/build.sh` can regenerate them |
| `BOM.csv` | The board's parts, exported from the schematic |
| `BOM-offboard.csv` | The lead (from `hardware/tools/capbank_lead.py`, shared with CANary-PWR J4) and the fixings |
| `lib/footprints.pretty/` | The cell laid flat, written by `gen/footprints.py` |
| `gen/` | `bank.py` parts and nets, `layout.py` placement and copper, `build.sh` runs it all and the checks |

`gen/build.sh` overwrites the KiCad files. Once the board is edited in KiCad,
the board file becomes the source of truth and the generators stop being run
on it, as with CANary-PWR.

## Parts

- **Cells: Eaton TVA1625-3R0256-R**, 25 F, 3.0 V rated, run at 2.5 V. It is
  the only candidate that meets all three requirements:
  - AEC-Q200;
  - holds 2.5 V at 85 °C (every 2.7 V cell is derated to 2.3 V there);
  - rated past the 4 A charge current (4.2 A at a 15 °C rise).

  It is long: 16.5 × 28.9 mm max. The footprint's envelope is Ø17 × 29 mm,
  so the alternate fits the same board: UCAP BCAP0025 P300 X11, 3.0 V,
  4.1 A, not AEC-Q200. The operator confirmed every part here in stock at
  DigiKey, lead parts included (2026-10-20).
- **NTC: Vishay NTCS0603E3103FLT**, 10 k 1 %, B25/85 3435 K, AEC-Q200. Murata
  NCU18XH103F6SRB and TDK NTCG163JF103FT1S follow the same curve within 0.3 %
  above 0 °C, so one GPI table in the firmware covers all three. TH1 sits on
  cell 1's − lead copper; Maxwell notes that a sensor on a terminal reads the
  cell accurately.
- **Connector: Molex 43045-1002**, the gold version of 43045-1000 on the same
  footprint, on both boards (2026-10-20, when the tin -1000 went out of stock).
  Gold also resists fretting under vibration better than tin.
- **Lead:** 172952-1001 housings with TPA (two 172953-1001 retainers each)
  and 43030-0009 gold crimps, 20 AWG, ≤ 300 mm, wired pin n to pin n.
  The plain 43025-1000 housing also fits (in stock at DigiKey, WM1787-ND), at
  the cost of the TPA's second lock. **Every contact must be gold**, because
  gold never mates tin. `check_capbank_lead.py` therefore checks the part
  number at both ends as well as the pins.
- **Ready-made leads are not suitable for the cab.** Molex's 214755
  female-to-female assemblies use 18 AWG UL 1061 wire, a style usually rated
  80 °C, and their pin mapping is unverified.

## Layout

Two rows of two cells lie flat, leads facing a centre strip, with J1 at the
strip's foot and its mating face off the bottom edge. The string runs C1
(bottom left) up to C2, across to C3 and down to C4 (bottom right), so both
of its ends land beside J1. The vents (the cells' closed ends) face the
board's left and right edges.

```
    C2 ====]  TAP2  [==== C3
            TAP1 | TAP3
    C1 ====]   J1   [==== C4
```

- Every inter-cell join is a pour on both layers.
- VCAP and GND reach J1's front-row power pins round the row's ends and
  under the housing.
- The taps and the NTC drop onto J1's back row from the strip.
- The sense lines cross in the 1.5 mm channel between J1's rows: − sense on
  F.Cu, + sense on B.Cu. Each net tie sits in a pour keep-out and joins its
  cell pad through a trace of its own, so no stack current flows past a
  sense point. `gen/layout.py`'s docstring has the details.

## Assembly

1. **Bend the leads** 90° at least 2 mm from the seal, over a ~2 mm radius,
   so the body lies flat with its end face ~4.5 mm from the holes. Don't
   pull or twist the leads at the seal; it can leak. Factory bent-lead
   versions exist (BCAP0025 P300 X12), but their bend geometry has not been
   checked against this footprint.
2. **Polarity:** the longer lead is + on the Eaton and UCAP cells. Pad 1 is
   the square pad with the `+` beside it. A silk bar marks each body's − side.
3. **Bed each cell in neutral-cure silicone RTV.** Acetoxy RTV gives off
   acetic acid, which corrodes the leads.
4. **Tie each column down.** One miniature tie per slot pair, round both
   cells, two per column.
5. Hand-solder only (~360 °C, under 5 s per lead), and don't wash the board.
   Keep the iron off the sleeves.
6. Keep the vent ends clear of the enclosure wall.

**Discharge the bank before unplugging it.** Lower `vshunt` on CANary-PWR
(bench button held, or I²C) until the red LED goes out. 6 F at 10 V across a
screwdriver is a welding event.

## Decisions and open items

- **The lead's pin map stays as it is (operator, 2026-10-20).** Swapping
  pins 5 and 6 would uncross the sense lines here but create a crossing on
  CANary-PWR. Its + sense ballast (R20) sits beside J4 pin 5 and its − sense
  ballast (R16) beside pin 6, so PWR's sense routing is uncrossed today. The
  question would reopen only if PWR's J4 area had to be redone; the gold
  header is a footprint drop-in, so it doesn't.
- **Firmware:** the GPI table needs the NTC's R–T curve (Vishay's online
  calculator, or Murata R44E for XH103).
