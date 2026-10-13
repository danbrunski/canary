# CANary-HAT rev 2.1 (KiCad project)

Design record: [`docs/CANary-HAT-Rev2.1.md`](../../docs/CANary-HAT-Rev2.1.md),
§13.5.10 for what schematic entry changed against
[`../canary-hat-rev2.0/`](../canary-hat-rev2.0/).

**Status (2026-10-12): layout routed, not yet fabricated.** DRC is clean:
0 violations, 0 unconnected items and 0 schematic parity issues.

[`FAB-NOTES.md`](FAB-NOTES.md) is rev 2.1's own, written from the routed
board. It carries over rev 2.0's stackup, impedance and assembly constraints.
Rev 2.0's other fabrication outputs were deliberately not carried over, so
nothing here can be sent to a fab by mistake: `production/`, `BOM.csv`, the
PCBWay assembly notes and quote, and the DRC/ERC reports. Regenerate them
from this project at fab time; rev 2.0's versions in `../canary-hat-rev2.0/`
are the templates.

J1 is DNP in rev 2.1: the operator fits a 13.5 mm stacking header (open
item 14).

The overlay and bring-up scripts are shared across revisions in
[`../canary-hat-firmware/`](../canary-hat-firmware/).
