# Hardware tools

## Placement review

Scores a KiCad board's footprint placement and draws it, so a placement can be
judged (and iterated) without opening KiCad:

```bash
hardware/tools/review_placement.sh <board.kicad_pcb> <config.json> <out.png> [table.json]
# e.g.
hardware/tools/review_placement.sh hardware/canary-pwr-rev1.0/canary-pwr.kicad_pcb \
    hardware/canary-pwr-rev1.0/gen/placement_review.json /tmp/pwr.png
```

It prints, and exits 1 if there is any problem:

- **problems**: courtyard overlaps on one side, a part over another part's
  through-hole pins on the far side, a courtyard outside the outline (edge
  parts get a tolerance), a part inside a keep-out;
- **ratsnest**: the minimum-spanning-tree length of every net, totalled as
  signal, power and GND, with the longest nets named. It is what the router
  has to connect, so a better placement makes it shorter;
- **pin distances**: for each configured `[part, ref, pin]`, how far the part's
  pad is from that pin, worst first — decoupling capacitors, gate drives,
  Kelvin sense.

The PNG shows both sides seen from the top (the bottom is not mirrored):
courtyards, pads coloured by net class, and the ratsnest (GND omitted, power
red, signals blue).

**Trying a placement** before writing it: pass a table as the fourth argument.
It is JSON, `{"REF": [x_mm, y_mm, rotation_deg, "F" | "B"], ...}`; unlisted
parts keep their position, and the board file is not modified. A bottom-side
rotation is the value KiCad shows for the flipped footprint. Iterate on the
table, then write it into the board with
`kicad_placement.py <board> <report> --place table.json --save <board>` and
refill the zones with `kicad-cli pcb drc --refill-zones --save-board` (on a
scratch copy of the project if the `.kicad_pro` has edits you want left
alone). The fills `--save` writes are stale, and pcbnew's own filler missed
pads that had just moved. Run DRC with `--schematic-parity` afterwards.

**Config** (per board): `outline` `[x0, y0, x1, y1]`, `keepouts`
`[{"name", "side", "rect"}]`, `edge_refs`, `edge_tolerance_mm`, `power_nets`
and `checks`. CANary-PWR's is `hardware/canary-pwr-rev1.0/gen/placement_review.json`.

## Silkscreen references

`silk_refs.py` puts every reference designator where it collides with
nothing, so DRC's silkscreen checks pass on a densely placed board:

```bash
"/mnt/c/Program Files/KiCad/10.0/bin/python.exe" "$(wslpath -w hardware/tools/silk_refs.py)" \
    "$(wslpath -w path/to/board.kicad_pcb)" [--out other.kicad_pcb] [--size 0.8]
```

- **Sizing.** Each label is set to 0.8 mm text with a 0.15 mm stroke, which
  is the board's minimum.
- **Where it tries.** Spots around the part at two distances, sliding along
  each side, at 0° and 90°. Big parts can also carry the label on their body.
- **What a spot must clear.** Every pad on that side (through-hole pads count
  on both sides), all silkscreen already placed, fiducial mask openings and
  the board edge.
- **Order.** Semiconductors, magnetics, connectors and switches go first, then
  passives, then fiducials and mounting holes.
- **No room.** The label is hidden, and the fab-layer copy stays.

The result depends only on the board, so CANary-PWR's `gen_pcb.py` calls
`place_refs()` and a regenerated board gets the same labels. Footprint text
drawn twice in one place is reported, not removed: fix it in the library.
Removing footprint items from pcbnew's python left KiCad 10.0.5's wrappers
returning raw pointers.

**Pieces.** `kicad_placement.py` runs under KiCad's own python (it needs
`pcbnew`; here that is the Windows KiCad 10 install, so paths go through
`wslpath -w`). `placement_review.py` runs under the repo venv (Pillow).
`KICAD_PYTHON` overrides the KiCad python the wrapper uses.
