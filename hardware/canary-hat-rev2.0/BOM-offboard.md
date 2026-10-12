# CANary-HAT — off-board and assembly BOM

Items the schematic cannot carry because they have no footprint, plus the
mating hardware the board is useless without. Keep this alongside the
KiCad-generated BOM; neither is complete on its own.

## Mates with a board connector

| For | Item | Notes |
|---|---|---|
| **BT1** | JST SH 2-pin cable, 1.0mm pitch | Mates with SM02B-SRSS-TB. |
| **BT1** | GNSS backup cell — **primary cell only** | There is **no charge path** on the board (series 1N4148W, D2, from the cell to V_BCKP), so a supercap will never charge. Without a cell fitted, V_BCKP floats and the receiver cold-starts on every boot. |
| **J4** | Active GNSS antenna, SMA male | Must have an **integrated SAW filter ahead of its LNA** — the MAX-M10M-00B has no on-module front end. Board supplies bias via L1 from VCC_RF. Confirm the LNA's bias current against VCC_RF's rating. |
| **J2 / J3** | Ferrules or tinned leads, 5.08mm block | 6-way vehicle side, 2-way 12V pass-through. |
| **J3** | DC barrel-jack pigtail | Feeds the X1202 UPS. **Rev 2.0: this leg is now tapped downstream of Q1 and D1**, so it sits behind both the reverse-polarity protection and the transient clamp — the exposure §3.7.4 previously had to accept is closed. |
| **J2** | OBD-II Y-cable / breakout | Needed for K-line eavesdropping alongside the NT510 or UpRev cable — both tools must share pins 7, 16 and ground. **Rev 2.0 re-assigned J2 pins 4–6** — pin 4 CAN High, 5 CAN Low, 6 K-line. A harness built to the rev 1.0 pinout puts CAN High on the K-line pin; see §3.9. |
| **J2** | 120 Ω resistor — bench use only | The board carries no CAN termination by design; a third terminator on a vehicle bus is a fault. For bench work against an unterminated bus, clamp 120 Ω across **J2 pins 4 and 5**, and remove it before the board goes back in a vehicle. This is why the split-termination footprints were considered and dropped in rev 2.0 — J2 is a screw block, so the workaround needs no tooling. |

## Mechanical

| Item | Notes |
|---|---|
| M2.5 standoffs + screws, 4 off | Height must match the J1 stacking header (13mm insulator) plus the Pi 5 active cooler and, if it sits between them, the X1202. |
| Pi 5 active cooler | Already fitted; sets the stack height below this board. |

## Assembly-house inputs (not parts)

| Item | Status |
|---|---|
| **Fiducials** | ❌ **Not on the board.** 49 footprints, zero fiducials. Machine placement of the 0.5mm-pitch LGA-14 (U6) and LGA-10 (U5) normally wants 3 global fiducials. Add before sending. |
| Position / centroid file | Generate with `kicad-cli pcb export pos`. |
| Assembly drawing with pin 1 marked | **Do this for U5, U6, U7.** Their footprints are SnapEDA/UltraLibrarian imports whose zero-rotation convention often disagrees with IPC-7351 and therefore with the assembler's. Rotation error on those three is the most likely assembly defect. |
| Assembly scope split | State which parts they place (U5, U6, U7 at minimum) vs which are hand-soldered, so the centroid and BOM agree. |
| Stencil | Needed if the LGAs are self-assembled. Paste apertures to be decided alongside the solder-mask expansion (see below). |

## Decisions recorded

- **U6 land pattern** — the imported footprint is 0.3048 × 0.5334 mm, about 19%
  under ST TN0018's 0.35 × 0.575 mm. Resize recommended; that widens the
  pad-to-pad gap problem from 0.1952 mm to 0.15 mm, which the scoped rule in
  `canary-hat.kicad_dru` covers.
- **Solder mask expansion on U6: +0.025 mm per side** (openings 0.40 × 0.625,
  dams 0.10 mm). ST's C/D figures would give 0.05 mm dams, below any fab
  minimum. Zero expansion gives safe 0.15 mm dams but lets ±0.05 mm mask
  registration encroach on the land and make the joint partly mask-defined —
  a fatigue stress concentrator. Fall back to zero expansion only if the fab
  rejects 0.10 mm dams.
- **R5 stays 1 kΩ.** It must be a **0.2W-rated 0603** — it dissipates 151 mW
  during a stuck-dominant bus fault, over a standard 0603's 0.1W.
  *(J5, the 2-pin mode-select header this note used to describe, was removed in
  rev 2.0: the commander pull-up is now switched by Q2/Q3 from GPIO24, so the
  mode changes in software rather than by unstacking the HAT from under the
  dash. There is no shunt to order.)*
- **D2 is a 1N4148W in SOD-123 — the same part as D5** (changed 2026-08-15 from
  a BAT54 in SOT-23). Silicon, *not* Schottky: it blocks reverse current into
  BT1, and BT1 is a primary cell with no charge path, so Schottky leakage would
  trickle-charge a non-rechargeable cell. The 2-pin package also makes the
  BAT54A/C/S common-node dual — which PCBWay proposed on the 2026-08-14 quote —
  impossible to fit rather than merely forbidden.
