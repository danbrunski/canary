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

## Autorouting with hand-laid power copper

`autoroute.sh` routes a board with Freerouting while keeping the copper laid by hand:

```bash
hardware/tools/autoroute.sh <scratch_project_dir> <freerouting.jar> [passes] [-- dsn_export.py options]
```

It edits the board in that directory, so give it a scratch copy of the project. The steps:

1. Refill the zones.
2. Export the DSN with `dsn_export.py`.
3. Route headless.
4. Add the router's tracks and vias with `ses_import.py`.
5. Refill again, then run DRC with schematic parity.
6. Refuse the result if any via sits in a pad (`via_in_pad.py`). KiCad's DRC does not flag a
   via in a pad of its own net, and plugged vias raise the board's cost a lot.
7. Refuse the result if any track lies on an inner plane, a layer of type power (`inner_tracks.py`).
   DRC does not flag a track cutting one. A mixed inner layer may carry tracks: CANary-PWR's In2
   is mixed, and rule areas keep tracks out of its power islands, which DRC does check. Freerouting routed signals across
   In1 although the DSN declares it `(type power)`: its maze search skips an inactive layer
   only when that layer is a signal layer (found 2026-10-16).

Use the router builds frozen under `~/code/freerouting/logs/jars/` and quote the jar's sha
with any result. For CANary-PWR the best settings so far (2026-10-15, a773a70ec) are one pass
with a higher rip-up cost: `FREEROUTING_ARGS=--router.scoring.start_ripup_costs=1500` and
passes `1`. Later passes never beat the first on this board.

**`dsn_export.py`** (KiCad python) fixes four things in KiCad's Specctra export:

- **Keep-outs.** A rule area that bans only parts comes out as a full keep-out. CANary-PWR's
  fan inlet would cost the router 26 x 26 mm of B.Cu, so those keep-outs are dropped.
- **Pours.** Freerouting reads a `plane` as copper that does not block other nets, so it
  would route signals through the power pours. Each outer-layer pour of a `--fixed-classes`
  net goes in as fixed wires covering its fill. Same-net taps can still connect at the edge.
- **Outer GND pours.** These are dropped, so GND pads fan out to the inner planes.
- **Widths.** `--width CLASS=mm` overrides a class's routed width. CANary-PWR no longer needs
  it: its classes carry the taps' widths (0.3 mm, GND 0.4) because the pours carry the current.
- **Clearances.** The board-edge clearance and the fiducials' 0.6 mm clearance go in as
  keepouts, since the export leaves both out.

**`grade.sh`** grades a routing result made elsewhere (another router, another session):
`grade.sh <project_dir> <routes.ses | routed.kicad_pcb> [workdir]`. It takes the project from git
HEAD (or `GRADE_REV=<commit>`) into a scratch directory, imports the SES (or takes the routed board as it is), and runs DRC
with schematic parity, `via_in_pad.py` and `inner_tracks.py`. A result counts when the only errors
are unconnected items and starved thermals, and both checkers report 0.

**`ses_import.py`** (KiCad python) adds only the router's work. KiCad's own SES import
deletes every track first. This script skips the wires and vias Freerouting marks `fix` or
`protect`: the hand-laid copper and the pour stand-ins.

A pad the router cannot reach is often boxed in by fixed copper rather than by geometry.
`~/code/KiCadRoutingTools/py_tools/check_reachability.py <board> --pad REF.N` measures
reachability on the board itself, where a pour gives way to a trace. So "PASSABLE" there,
with the pad still unrouted here, points at the fixed pours. That is how CANary-PWR's TG gate
was found: it is now laid by hand.

**Pieces.** `kicad_placement.py` runs under KiCad's own python (it needs
`pcbnew`; here that is the Windows KiCad 10 install, so paths go through
`wslpath -w`). `placement_review.py` runs under the repo venv (Pillow).
`KICAD_PYTHON` overrides the KiCad python the wrapper uses.
