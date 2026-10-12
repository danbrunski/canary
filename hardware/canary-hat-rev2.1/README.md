# CANary-HAT rev 2.1 (KiCad project)

Design record: [`docs/CANary-HAT-Rev2.1.md`](../../docs/CANary-HAT-Rev2.1.md),
§13.5.10 for what schematic entry changed against
[`../canary-hat-rev2.0/`](../canary-hat-rev2.0/).

**Status: schematic entered, layout not started.** `canary-hat.kicad_pcb` is
still rev 2.0's board, the starting point for "Update PCB from Schematic". It is
not a rev 2.1 layout.

Rev 2.0's fabrication outputs and notes were deliberately not carried over,
so nothing here can be sent to a fab by mistake: `production/`, `BOM.csv`,
`FAB-NOTES.md`, the PCBWay assembly notes and quote, and the DRC/ERC reports.
Regenerate them from this project at fab time. Rev 2.0's versions in
`../canary-hat-rev2.0/` are the templates. The stackup, impedance and
assembly constraints in its `FAB-NOTES.md` still apply. J1 is DNP in rev 2.1
(operator-fitted 13.5 mm stacking header, open item 14) and the parts open items 34–36 settle.

The overlay and bring-up scripts are shared across revisions in
[`../canary-hat-firmware/`](../canary-hat-firmware/).
