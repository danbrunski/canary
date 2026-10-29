# Hardware tools

## Which KiCad

The scripts here (`grade.sh`, `autoroute.sh`, `review_placement.sh`, the cap bank's
`gen/build.sh`) take their KiCad from `kicad_env.sh`. That is the native install when there is
one: `kicad-cli` on PATH and a `python3` that imports `pcbnew`, which Ubuntu gets from
`ppa:kicad/kicad-10.0-releases`. Otherwise, or when `KICAD_BIN` names a Windows install's bin
directory, it is that Windows install, driven from WSL and handed Windows paths. A "KiCad
python" tool below runs under that python: `python3` natively.

- **Windows KiCad may not overwrite a board in the WSL filesystem.** On this laptop
  (2026-10-20) `pcb drc --save-board` fails there with "Cannot copy permissions" and pcbnew's
  `SaveBoard` hangs, even with 755 directories; a new file saves. Another setup's cap-bank
  builds did overwrite boards there, so the scripts detect it rather than refuse: `kicad_drc`
  stops on the error, a Windows `kicad_py` stops after `KICAD_PY_TIMEOUT` (600 s) with the
  reason, and `kicad_tmp` puts scratch copies under Windows `%TEMP%`, where both work. Until
  2026-10-20 the scripts hid the failure: the refilled boards `grade.sh` reported were never
  saved (its DRC verdicts were still right), and `autoroute.sh` exported its DSN from fills that
  were never refilled.
- **A native install needs the global library tables** that the GUI copies on its first run:
  `cp /usr/share/kicad/template/{fp,sym}-lib-table ~/.config/kicad/10.0/`. Without them DRC
  reports a `lib_footprint_issues` warning per footprint instead of the real mismatches.
  `kicad_env.sh` says so.
- **Native KiCad saves boards with LF line endings.** The committed boards were saved by Windows
  KiCad with CRLF, so the first native save of one changes every line.

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
  parts get a tolerance), a part inside a keep-out. Overlaps are the courtyard
  pairs KiCad finds colliding, touching included, the same test as DRC's
  `courtyards_overlap`. Boxes round the courtyards reported overlaps that
  weren't there (2026-10-20): KiCad's box adds the courtyard line's width on
  every side, and a QFN's courtyard steps in at its corners;
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
python3 hardware/tools/silk_refs.py path/to/board.kicad_pcb [--out other.kicad_pcb] [--size 0.8]
```

(KiCad python: with the Windows install it is `"$KICAD_BIN/python.exe"`, given `wslpath -w` paths.)

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

## Pin 1 on the silkscreen

`pin1_silk.py` (KiCad python) lists every part whose silkscreen does not show where pin 1 is:

```bash
python3 hardware/tools/pin1_silk.py <board.kicad_pcb | library.pretty>
```

The assembler orients each part from the position file and the silkscreen. Those are all the
Gerbers carry: a pin-1 mark drawn only on the fab layer never reaches the fab. On 2026-10-28 PCBWay
had to ask where pin 1 was on HAT rev 2.1's U3, U4 and U6, because:

- **Ultra Librarian footprints draw a filled square beside every tenth pin**, as a counting aid. On a
  14-pin or 10-pin part, that square beside pin 10 is the only filled mark, and it reads as a pin-1
  dot. U3 (SOIC-14) and U6 (LGA-14) had nothing else: their board copies had lost the library's `*`
  beside pin 1. CANary-PWR's U1 (QFN-38) has squares beside pins 10, 20 and 30 as well as its `*`.
- **TI's `D0008A_L` (SOIC-8) marks pin 1 on the fab layer only.** U4's silkscreen was two
  identical lines.

Every part with three or more numbered pads gets two checks:

- **a mark beside the wrong pad**: a filled silk shape, or a `*` or `1`, nearer another pad than pin
  1;
- **no mark**: silk that looks the same after any turn that maps the land pattern onto itself. Only
  shapes and `*`/`1` count. A text label is not an orientation mark, and TI's library copy passed
  on a stray `Designator9` text.

Given a `.pretty` directory, it checks every footprint in that library. Run it on the project
library before laying out a new revision. `pcbway_bom.py` runs the board check whenever it writes
an order's files, and exits 1 on any problem.

## PCBWay BOM

`pcbway_bom.py` (KiCad python) writes the BOM and position file that PCBWay's KiCad plugin would
upload, byte for byte (checked against the plugin's own zips for the cap bank and HAT rev 2.1,
2026-10-20):

```bash
python3 hardware/tools/pcbway_bom.py <board.kicad_pcb> <out_dir>
```

Read it before an order, or upload its two files by hand. It then runs `pin1_silk.py`'s board check
and exits 1 if any part's pin 1 is unmarked or mismarked (see Pin 1 on the silkscreen). The plugin
builds the BOM from the **footprints'** fields, so a field changed only in the schematic does not reach it until the board
is updated from the schematic. Schematic parity in DRC checks that the two agree.

- **Columns.** MPN comes from a field named `MPN`. Package comes from `Package`, and is left empty
  when no footprint has that field. Every other non-empty field is a column, except Reference,
  Value, Datasheet and Footprint.
- **Lines.** There is one line per value, footprint, package and MPN, and one per do-not-populate
  part. A footprint excluded from both the BOM and the position files is left out of both. One
  excluded from the BOM only still has a placement.
- **PCBWay works from Value and MPN** (operator, 2026-10-20), not from the DNP column or the notes.
  A part they must not buy or fit is therefore excluded from both files, as CANary-PWR's J1–J5 are,
  rather than only marked DNP.
- **A field that differs inside a line** reads `[C1]text; [C2,C3]other`.
- **A field set on only one part of a line** reads as if it applied to the whole line.

## 3D models

KiCad's 3D library has no Micro-Fit 3.0 header models, although its Micro-Fit
footprints reference them. They are missing from 10.0 and from upstream
kicad-packages3D. `gen_3dmodels.py` writes those headers (2 × 3 and 2 × 5,
right-angle), and the cap bank's flat cell, as STEP files into each project's
`lib/3dmodels/`. Boards reference them as `${KIPRJMOD}/lib/3dmodels/<name>.step`.
The script needs cadquery, which the repo venv does not carry; its docstring
has the commands.

- Plan geometry comes from the footprints. The header's **height is an
  estimate** (8.4 mm body, 9.4 mm at the latch ramp), because Molex's drawing
  could not be fetched. Check it against Molex's own 43045-1002 STEP before
  relying on it for an enclosure clearance.
- PWR's custom IC footprints (U1 QFN-38, U6 DGN, U8 DGK) use KiCad's stock
  package models. Their pads match the stock footprints' origin and pin-1
  corner, so the models need no offset. F1 renders as a stock 1812 chip
  resistor until its fuse MPN is chosen.
- KiCad's *Update footprints from library* puts a Micro-Fit header's stock
  model path back on the board, and that file does not exist. The cap bank's
  `gen_pcb.py` re-points it on every build. On CANary-PWR the fix is to edit
  J3 and J4 by hand again.

## Autorouting with hand-laid power copper

`autoroute.sh` routes a board with freerouting-rs while keeping the copper laid by hand:

```bash
hardware/tools/autoroute.sh <scratch_project_dir> [passes] [-- dsn_export.py options]
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

**Freerouting (Java) is retired** (operator, 2026-10-20): the router is freerouting-rs's
`freerouting-rs` binary, found as its KiCad plugin finds it: `FREEROUTING_RS_BIN` (a directory),
then PATH, then `~/.cargo/bin`. It is never a bare `freerouting`, which can be Java's launcher.
`autoroute.sh` refuses a `.jar` argument and any binary whose `--version` does not start with
`freerouting-rs`, and prints the build it ran (`freerouting-rs <version> (git <commit>)`); quote
that with any result. The binary takes
Java's `-de`/`-do` and `--router.*` option names, so `FREEROUTING_ARGS` passes them on, e.g.
`--router.scoring.start_ripup_costs=1500 --router.autorouter.sweep=free
--router.autorouter.sweep_passes=2`. Java Freerouting's results above and in the CANary-PWR spec
are history.

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
are unconnected items and starved thermals, and both checkers report 0. The graded board is
saved refilled in the scratch directory; a DRC that fails, or cannot save, stops the grade.

**`cut_off.py`** (KiCad python) says where each unconnected item is: `cut_off.py <board> [NET ...]`.
kicad-cli's DRC names a pour piece by its zone's anchor (the outline's first corner), so a GND pad cut
off in a scrap of pour reads as "Zone [GND] <-> Zone [GND]" at the board's corner. It refills the zones,
joins each net's copper wherever two pieces overlap on a layer, and prints every group but the largest:
fill pieces with their extents, pads, vias, and the nearest point of the rest of the net. A net in k
groups is k - 1 unconnected items; it matched DRC net for net on CANary-PWR's unrouted board (149 in 77
nets) and its routed one (2026-10-19). Joining by anchors alone does not: a track ending 0.05 mm short
of a pour still overlaps its fill, and DRC counts it joined. `grade.sh` runs it last.

**`ses_import.py`** (KiCad python) adds only the router's work. KiCad's own SES import
deletes every track first. This script skips the wires and vias Freerouting marks `fix` or
`protect`: the hand-laid copper and the pour stand-ins.

A pad the router cannot reach is often boxed in by fixed copper rather than by geometry.
`~/code/KiCadRoutingTools/py_tools/check_reachability.py <board> --pad REF.N` measures
reachability on the board itself, where a pour gives way to a trace. So "PASSABLE" there,
with the pad still unrouted here, points at the fixed pours. That is how CANary-PWR's TG gate
was found: it is now laid by hand.

**Pieces.** `kicad_placement.py` runs under KiCad's own python (it needs
`pcbnew`; `kicad_env.sh` picks it, see Which KiCad). `placement_review.py` runs
under the repo venv (Pillow).
