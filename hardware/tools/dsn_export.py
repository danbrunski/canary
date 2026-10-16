"""Export a KiCad board to Specctra DSN for Freerouting, with KiCad's export fixed for routing.

Run with KiCad's own python (it needs `pcbnew`) on a board whose zones are filled:

    dsn_export.py <board.kicad_pcb> <out.dsn> [--fixed-classes Power_High,Switch_Node]
                  [--drop-nets GND] [--width CLASS=mm ...]

KiCad's export, and how Freerouting reads it, needs five corrections here:

- A rule area that bans only footprints or pads comes out as a full `(keepout ...)`, so the
  router loses copper the board allows (CANary-PWR's fan inlet on B.Cu). Rule areas that
  allow both tracks and vias are dropped.
- Freerouting reads every `(plane ...)` as a conduction area that does not block other nets,
  so signals get routed through power pours and cut them when KiCad refills. Each outer-layer
  pour of a net in `--fixed-classes` is replaced by fixed wires covering its fill: other nets
  keep clear, and same-net taps connect to its edge.
- Outer-layer pours of `--drop-nets` (GND) are dropped, so GND pads fan out to the inner
  planes rather than count as joined through a pour that the routing then breaks up.
- `--width CLASS=mm` sets a class's routed width. The hand-laid pours carry the current, so
  the router only adds low-current taps to those nets and can do it narrow; a 1.5 mm class
  width cannot reach a 0.25 mm QFN pin.
- The board-edge clearance and pads' own larger clearances (fiducial mask openings) are not
  exported. Both go in as keepouts, after the layer definitions: a shape that names a layer
  before the structure defines it crashed Freerouting's reader (fixed in 07c970db9).

Coordinates in the DSN are micrometres with y negated.
"""

import argparse
import re
from collections.abc import Iterator

import pcbnew

OUTER = {pcbnew.F_Cu: "F.Cu", pcbnew.B_Cu: "B.Cu"}
SLAB_MM = 0.25  # width of the vertical strips a pour fill is cut into
MIN_MM = 0.05  # strips thinner than this are dropped
Interval = tuple[float, float]


def um(v: float) -> int:
    return round(v * 1000)


def _crossings(chains: list[list[tuple[float, float]]], x: float) -> list[float]:
    ys = []
    for pts in chains:
        for (xa, ya), (xb, yb) in zip(pts, pts[1:] + pts[:1], strict=True):
            if (xa <= x < xb) or (xb <= x < xa):
                ys.append(ya + (x - xa) * (yb - ya) / (xb - xa))
    ys.sort()
    return ys


def _intervals(chains: list[list[tuple[float, float]]], x: float) -> list[Interval]:
    ys = _crossings(chains, x)
    return [(ys[i], ys[i + 1]) for i in range(0, len(ys) - 1, 2)]


def _intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo >= MIN_MM:
                out.append((lo, hi))
    return out


def fill_rects(
    chains: list[list[tuple[float, float]]],
) -> Iterator[tuple[float, float, float, float]]:
    """Rectangles inside a polygon set (outlines and holes, even-odd), from vertical strips."""
    xs = [x for pts in chains for x, _ in pts]
    x, x_end = min(xs), max(xs)
    strips = []
    while x < x_end - MIN_MM:
        x1 = min(x + SLAB_MM, x_end)
        inside = _intersect(_intervals(chains, x + 1e-4), _intervals(chains, x1 - 1e-4))
        strips.append((x, x1, inside))
        x = x1
    # merge neighbouring strips with the same intervals into wider rectangles
    i = 0
    while i < len(strips):
        x0, x1, ivs = strips[i]
        j = i + 1
        while (
            j < len(strips)
            and len(strips[j][2]) == len(ivs)
            and all(
                abs(p[0] - q[0]) < 1e-3 and abs(p[1] - q[1]) < 1e-3
                for p, q in zip(strips[j][2], ivs, strict=True)
            )
        ):
            x1 = strips[j][1]
            j += 1
        for y0, y1 in ivs:
            yield (x0, y0, x1, y1)
        i = j


def wire(layer: str, net: str, rect: tuple[float, float, float, float]) -> str:
    """A fixed path covering a rectangle: along its long side, as wide as its short side."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    if w >= h:
        ya = (y0 + y1) / 2
        pts = (x0 + h / 2, ya, max(x1 - h / 2, x0 + h / 2 + 0.001), ya)
        width = h
    else:
        xa = (x0 + x1) / 2
        pts = (xa, y0 + w / 2, xa, max(y1 - w / 2, y0 + w / 2 + 0.001))
        width = w
    coords = f"{um(pts[0])} {-um(pts[1])} {um(pts[2])} {-um(pts[3])}"
    return f"    (wire (path {layer} {um(width)} {coords}) (net {net}) (type fix))\n"


def _chains(sps: "pcbnew.SHAPE_POLY_SET") -> list[list[tuple[float, float]]]:
    mm = pcbnew.ToMM
    out = []
    for i in range(sps.OutlineCount()):
        rings = [sps.Outline(i)] + [sps.Hole(i, h) for h in range(sps.HoleCount(i))]
        for ch in rings:
            out.append([(mm(ch.CPoint(k).x), mm(ch.CPoint(k).y)) for k in range(ch.PointCount())])
    return out


def _net_token(dsn: str, name: str) -> str:
    """The net's name as the DSN's network section spells it (quoted when it has to be)."""
    quoted = f'"{name}"'
    return quoted if f"(net {quoted}" in dsn else name


def _block_end(text: str, start: int) -> int:
    """Index just past the s-expression that opens at `start`, skipping quoted strings."""
    depth, i, in_str = 0, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            in_str = c != '"'
        elif c == '"' and text[i - 1] != "(" and text[i - 2 : i] != "e ":
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced DSN")


def _remove_blocks(text: str, pattern: str, keep) -> tuple[str, int]:
    removed, out, pos = 0, [], 0
    for m in re.finditer(pattern, text):
        if m.start() < pos:
            continue
        end = _block_end(text, m.start())
        block = text[m.start() : end]
        if keep(block):
            continue
        out.append(text[pos : m.start()])
        pos = end
        removed += 1
    out.append(text[pos:])
    return "".join(out), removed


DEFAULT_CLEARANCE_MM = (
    0.2  # what the router keeps from a keepout; the strips below make up the rest
)
SIGNAL_LAYERS = ("F.Cu", "B.Cu")


def _edge_keepouts(board: "pcbnew.BOARD") -> list[str]:
    """Strips along the inside of the outline's bounding box, so the board-edge clearance holds."""
    edge = pcbnew.ToMM(board.GetDesignSettings().m_CopperEdgeClearance)
    w = edge - DEFAULT_CLEARANCE_MM
    if w <= 0:
        return []
    outline = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(outline, False)  # centreline, which edge clearance is from
    bb = outline.BBox()
    x0, y0 = pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY())
    x1, y1 = pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom())
    o = 1.0  # the strips reach past the edge, where nothing routes anyway
    strips = [
        (x0 - o, y0 - o, x1 + o, y0 + w),
        (x0 - o, y1 - w, x1 + o, y1 + o),
        (x0 - o, y0 - o, x0 + w, y1 + o),
        (x1 - w, y0 - o, x1 + o, y1 + o),
    ]
    return [
        f'    (keepout "" (rect {layer} {um(a)} {-um(d)} {um(c)} {-um(b)}))\n'
        for layer in SIGNAL_LAYERS
        for a, b, c, d in strips
    ]


def _pad_keepouts(board: "pcbnew.BOARD") -> list[str]:
    """A circle round each pad whose own clearance exceeds the default (fiducial mask openings)."""
    out = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            local = pad.GetLocalClearance()
            local = pcbnew.ToMM(local) if local else 0.0
            if local <= DEFAULT_CLEARANCE_MM:
                continue
            size = pad.GetSize(pcbnew.F_Cu)
            dia = max(pcbnew.ToMM(size.x), pcbnew.ToMM(size.y)) + 2 * (local - DEFAULT_CLEARANCE_MM)
            x, y = pcbnew.ToMM(pad.GetPosition().x), pcbnew.ToMM(pad.GetPosition().y)
            for layer, lid in zip(SIGNAL_LAYERS, (pcbnew.F_Cu, pcbnew.B_Cu), strict=True):
                if pad.IsOnLayer(lid):
                    out.append(f'    (keepout "" (circle {layer} {um(dia)} {um(x)} {-um(y)}))\n')
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("out")
    ap.add_argument("--fixed-classes", default="Power_High,Switch_Node")
    ap.add_argument("--drop-nets", default="GND")
    ap.add_argument("--width", action="append", default=[], help="CLASS=mm, repeatable")
    args = ap.parse_args()
    fixed_classes = set(filter(None, args.fixed_classes.split(",")))
    drop_nets = set(filter(None, args.drop_nets.split(",")))

    board = pcbnew.LoadBoard(args.board)
    if not pcbnew.ExportSpecctraDSN(board, args.out):
        raise SystemExit("KiCad's DSN export failed")
    dsn = open(args.out).read()

    # 1. keepouts from rule areas that allow tracks and vias
    allowed = []
    for z in board.Zones():
        if z.GetIsRuleArea() and not z.GetDoNotAllowTracks() and not z.GetDoNotAllowVias():
            bb = z.GetBoundingBox()
            allowed.append((um(pcbnew.ToMM(bb.GetX())), um(pcbnew.ToMM(bb.GetRight()))))

    def keep_keepout(block: str) -> bool:
        m = re.search(r"\(polygon\s+\S+\s+\S+((?:\s+-?\d+(?:\.\d+)?)+)", block)
        if not m:
            return True  # circles inside footprint images: mounting holes, pin keepouts
        nums = [int(float(n)) for n in m.group(1).split()]
        xs = nums[0::2]
        return not any(abs(min(xs) - a) < 20 and abs(max(xs) - b) < 20 for a, b in allowed)

    dsn, n_keepouts = _remove_blocks(dsn, r"\(keepout ", keep_keepout)

    # 2./3. outer-layer planes: power pours become fixed wires, dropped nets go
    net_class = {}  # from the DSN's own class lists, so it matches what the router sees
    for m in re.finditer(r"\(class\s+(\S+)((?:\s+(?:\"[^\"]*\"|[^\s()]+))*)", dsn):
        for tok in re.findall(r"\"[^\"]*\"|[^\s()]+", m.group(2)):
            net_class[tok.strip('"')] = m.group(1)
    fixed_wires, fixed_nets = [], set()
    for z in board.Zones():
        layer = OUTER.get(z.GetLayer())
        if z.GetIsRuleArea() or layer is None or not z.IsFilled():
            continue
        net = z.GetNetname()
        if net in drop_nets:
            continue
        if net_class.get(net) not in fixed_classes:
            continue
        fixed_nets.add((net, layer))
        token = _net_token(dsn, net)
        for rect in fill_rects(_chains(z.GetFilledPolysList(z.GetLayer()))):
            fixed_wires.append(wire(layer, token, rect))

    def keep_plane(block: str) -> bool:
        m = re.match(r'\(plane\s+("[^"]*"|\S+)\s+\(polygon\s+(\S+)', block)
        net, layer = m.group(1).strip('"'), m.group(2)
        if layer not in OUTER.values():
            return True
        return net not in drop_nets and (net, layer) not in fixed_nets

    dsn, n_planes = _remove_blocks(dsn, r"\(plane ", keep_plane)

    # 4. class widths
    for spec in args.width:
        cls, mm = spec.split("=")
        m = re.search(r"\(class " + re.escape(cls) + r"\s", dsn)
        if not m:
            raise SystemExit(f"no class {cls} in the DSN")
        end = _block_end(dsn, m.start())
        block = re.sub(
            r"\(width \d+(?:\.\d+)?\)", f"(width {um(float(mm))})", dsn[m.start() : end], count=1
        )
        dsn = dsn[: m.start()] + block + dsn[end:]

    # 5. clearances the export leaves out: the board-edge clearance, and pads' own larger clearances
    #    (fiducials). Both go in as keepouts, which the router already keeps its clearance from.
    extra = _edge_keepouts(board) + _pad_keepouts(board)
    if extra:  # after the layer definitions, which the shapes name
        m = re.search(r"\n\s*\(via\s", dsn)
        dsn = dsn[: m.start()] + "\n" + "".join(extra).rstrip("\n") + dsn[m.start() :]

    if fixed_wires:
        m = re.search(r"\(wiring\b", dsn)
        if m:
            insert = m.end()
            dsn = dsn[:insert] + "\n" + "".join(fixed_wires) + dsn[insert:]
        else:
            close = dsn.rstrip().rfind(")")
            dsn = dsn[:close] + "  (wiring\n" + "".join(fixed_wires) + "  )\n" + dsn[close:]
    open(args.out, "w").write(dsn)
    print(
        f"{args.out}: dropped {n_keepouts} keepout(s) the board allows routing in, "
        f"{n_planes} outer plane(s); {len(fixed_wires)} fixed wires for {len(fixed_nets)} "
        f"pour(s); widths {args.width or 'unchanged'}"
    )


if __name__ == "__main__":
    main()
