# PCBWay quote T-B4W341056A — 5 units, 2026-08-14

Source: `Quote for T-B4W341056A-5units-BOM_canary-hat.kicad_pcb(2026-08-14).xls`.

Component cost **$211.99 / 5 units** ($42.40 per board). Assembly and PCB cost
were left blank on the quote — this is **parts only**, and it is not the price
of a finished board.

The quote has been folded back into the schematics. Every symbol now carries:

| Field | Meaning |
|---|---|
| `MPN` / `Manufacturer` | the part to buy. Manufacturer is written **only where the quote stated one** — see "What was deliberately left blank" below |
| `Cost_5u` | unit price at qty 5 from this quote. Blank = not priced |
| `Quote` | status and provenance, prefixed `PCBWay T-B4W341056A (5 units, 2026-08-14)` |

`grep -l 'OPEN' *.kicad_sch` finds everything still unresolved.

> **One manual step before you re-export the BOM.** The quote was generated from
> `canary-hat.kicad_pcb`, and the PCB's footprints carry their own copy of these
> fields — currently the 17 MPNs that existed before this update. Run
> **Tools → Update PCB from Schematic** with *Update footprint fields* ticked, so
> the new MPN / Manufacturer / Cost_5u / Quote values reach the board file.
> Without that step the next BOM you send PCBWay will still be missing every
> part number added here. This was left for KiCad to do rather than hand-edited
> into the routed board file.

## Still open — PCBWay is waiting on these

| Ref | Question | Answer given | Status |
|---|---|---|---|
| C1, C3, C7–C14 | "provide exact part number" | Samsung **CL10B104KB8NNNC** or equivalent | Named, **not yet priced** |
| R5, R6 | "provide exact part number" | Panasonic **ERJ-PA3F1001V** or equivalent (0603, **0.2 W**) | Named, **not yet priced** |
| Y1 | "2 part numbers, which one?" | **ABLS2-40.000MHZ-D4YF-T** | Answered, **not yet priced** |
| D2 | offered `BAT54A` | — | **Closed by design change — see below** |
| C2, J1, J5 | "provide exact part number" | self-source, do not populate | Closed — excluded from PCBWay assembly |

Because C1-bank, R5/R6 and Y1 are unpriced, **$211.99 is not the final number.**

### D2 was resolved by removing the part, not by picking one (2026-08-15)

PCBWay offered **`BAT54A`** — precisely the common-anode dual that D2's own Note
and `FAB-NOTES.md` forbid, differing from the intended `BAT54-A` by one hyphen.
That the vendor proposed it while holding the document that prohibits it is the
whole argument: a prohibition written in prose does not survive contact with a
supply chain.

So D2 is now a **1N4148W in `Diode_SMD:D_SOD-123`** — the identical part to D5.
This closes the line three ways at once:

- **Silicon beats Schottky here on the parameter that matters.** D2 blocks
  reverse current into BT1, a primary cell with no charge path. Schottky leakage
  is orders of magnitude higher and roughly doubles per 10 °C; a hot parked
  cabin would trickle-charge a non-rechargeable cell. The ~0.4–0.5 V forward
  drop at the ~20 µA backup load is the accepted cost.
- **The failure mode stops existing.** SOD-123 is a 2-pin package, so no
  BAT54A/C/S common-node dual can physically be fitted. Impossible, not
  documented.
- **It deletes a line item.** D2 and D5 are now the same part in the same
  package.

**Action for PCBWay:** the D2 line on this quote (BAT54, SOT-23, $0.037 ea) is
void. Merge D2 into D5's line and re-quote 2 × 1N4148W / SOD-123.

**One open verification, not vendor-facing:** the MAX-M10M **V_BCKP minimum**
has not been confirmed. `datasheets/` holds only the 2-page product summary,
which does not state it. Silicon leaves roughly 1.55 V at cell end-of-life and
cold pushes Vf higher, so pull u-blox's integration manual before trusting
backup hold time at the tail of the cell. If the floor turns out well above
1.5 V, the off-board cell is the cheap lever — a 3.6 V LiSOCl₂ primary holds
~3.6 V nearly flat and makes the question disappear.

## Errors PCBWay's review caught

**D1 description was wrong.** SMBJ24A is a **TVS**, not a Zener. Corrected in
the schematic to `TVS diode, 24V standoff, SMB (DO-214AA)`. Nothing electrical
changes — the part number was always right — but the wrong word in a BOM
Description is how a Zener gets substituted.

**C2's footprint is too small — not yet fixed.** `CP_Radial_D6.3mm_P2.50mm` is a
6.3 mm can on 2.5 mm lead pitch. A 100 µF **50 V** aluminium electrolytic is a
~10 mm can on 5 mm pitch and will not fit that land pattern. The 50 V rating is
not negotiable (D1 clamps at ~38.9 V), so the footprint has to grow. C2 is
self-sourced and hand-fitted, so this does not block the current order, but it
**must be fixed before the next fab run** — the pads are already etched wrong on
this revision.

**Y1's duplicate part number is fixed at the root.** PCBWay saw *two* part
numbers on the Y1 line and had to ask which to quote. The second one leaked out
of the SnapEDA library symbol, which still carried the 12 MHz donor part's
`MP`, `MF`, `MANUFACTURER`, `Package`, `Price`, `Check_prices`, `Purchase-URL`,
`Availability` and `SnapEDA_Link` fields. Those are now stripped from both
`lib/symbols/ABLS2-12.000MHZ-D4Y-T.kicad_sym` and the cached copy in
`CANFD_Interface.kicad_sch`, so the next BOM export cannot reproduce the
ambiguity. The symbol keeps `MPN` / `Manufacturer` on the instance, which is the
single place the part number should live.

## What was deliberately left blank

`Manufacturer` is **not** filled in for the passives PCBWay chose
(C4, C5, C6, C15, C16, R1–R4, R7–R9). The quote gave a part number and no brand
for those lines, and the series prefixes only *imply* a manufacturer. Inferring
one and writing it into the same field as the quote-stated manufacturers would
make guessed data indistinguishable from confirmed data. Ask PCBWay for the
brands, or accept the part numbers as-is.

Likewise `SS54` (D4) and `1N4148W` (D5) keep an empty `Manufacturer`. The quote
notes brands `HXY MOSFET` and `R+O`, which are supplier shorthands rather than
manufacturer names, and both are generic part numbers where pinning one brand
would over-constrain a re-order. The brands are recorded in each part's `Quote`
field.

## Cost reconciliation

Summing `Cost_5u` across the schematic gives **$42.361/unit → $211.81**, against
the quote's **$211.99**. The $0.185 gap is D2's line: PCBWay priced *their*
`BAT54A` at $0.037 ea. No `Cost_5u` is recorded for D2 because that price is for
a part that will not be used. Every other line reconciles exactly.

## Lead time and cost concentration

Four lines quote **7–10 workdays**: U7, J2, J3 and J4. Everything else has no
stated lead time.
U7 (MAX-M10M-00B, $10.741) is both a longest-lead part and **25% of the
component cost**; U5 + U6 + U7 together are **$19.82 of $42.36, 47%**.

The three Phoenix/Molex connectors are the surprise: **J2 + J3 + J4 = $14.52,
34% of the per-board component cost** — more than the GNSS module. The operator
already told PCBWay that a cheaper 5.08 mm alternative (KF128 / DG128 family) is
acceptable for J2 and J3, which is worth $6–7 per board. J4 (Molex SMA edge
mount, $6.80) has no stated alternative.

## Stale BOM caveat

The quote was priced against a BOM export in which **C7, C8 and C9 were 25 V**
100 nF parts. The schematic now specifies **50 V** for the entire 100 nF bank
(the change landed after the export). No price impact — that line was never
quoted, because PCBWay asked for a part number instead — but if you re-send the
BOM, re-send it from the current schematic.
