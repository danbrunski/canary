# CANary-HAT — Fabrication and Manufacturing

How the board in [`CANary-HAT-Spec.md`](CANary-HAT-Spec.md) gets made: the
KiCad project as built, manufacturing notes, and the assembly-readiness work
done before rev A was submitted.

> **Part of the CANary-HAT board documentation**, split across four files.
> Section numbers are **stable across the split** — a bare `§3.7.5` means the
> same thing in every file; this table says which file holds it.
>
> | Sections | File |
> |---|---|
> | §1, §2, §4, §5, §8–§11 | [`CANary-HAT-Spec.md`](CANary-HAT-Spec.md) — what the board is: overview, stackup, pin map, open items, implied software work |
> | §3 | [`CANary-HAT-Circuits.md`](CANary-HAT-Circuits.md) — why each part is wired this way, per subsystem |
> | §6, §7, §12 | [`CANary-HAT-Fabrication.md`](CANary-HAT-Fabrication.md) — KiCad implementation, manufacturing notes, assembly readiness |
> | §13 | [`CANary-HAT-Revisions.md`](CANary-HAT-Revisions.md) — bring-up measurements, rev B, rev C, lessons from prior art |

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

