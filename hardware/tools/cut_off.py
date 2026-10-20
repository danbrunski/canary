"""List the copper each net still has cut off from the rest of it, and where that copper is.

Run with KiCad's own python (it needs `pcbnew`):

    cut_off.py <board.kicad_pcb> [NET ...]

kicad-cli's DRC reports a missing connection between two pieces of one pour at the zone's anchor
(its first outline corner), not at the pieces, and KiCad's ratsnest is not reachable from pcbnew's
python. This refills the zones and joins each net's copper wherever two pieces of it overlap on a
layer (fill pieces, pads, vias, tracks), which is how KiCad joins it: anchors alone are not enough,
since a track ending 0.05 mm short of a pour's edge still overlaps the fill and DRC counts it joined
(LTC_VOUTSP at C5, 2026-10-19). Every group but the largest is printed with its fill pieces'
extents, pads and vias, and the nearest point of the rest of the net. A net in k groups is k - 1 of
DRC's unconnected items. Exits 1 when any net has copper cut off.
"""

import sys

import pcbnew

mm = pcbnew.ToMM
ERR = pcbnew.FromMM(0.005)


def layer_poly(item, layer):
    s = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(s, layer, 0, ERR, pcbnew.ERROR_INSIDE)
    return s


def net_items(board, net, layers):
    """(kind, label, {layer: SHAPE_POLY_SET}, anchors) for every copper item on `net`."""
    items = []
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for layer in z.GetLayerSet().CuStack():
            fill = z.GetFilledPolysList(layer)
            for i in range(fill.OutlineCount()):
                piece = pcbnew.SHAPE_POLY_SET()
                piece.AddOutline(fill.Outline(i))
                for h in range(fill.HoleCount(i)):
                    piece.AddHole(fill.Hole(i, h))
                bb = piece.BBox()
                x0, x1, y0, y1 = (
                    mm(v) for v in (bb.GetX(), bb.GetRight(), bb.GetY(), bb.GetBottom())
                )
                area = piece.Area() / 1e12
                extent = f"x {x0:.2f}-{x1:.2f} y {y0:.2f}-{y1:.2f}"
                label = f"{board.GetLayerName(layer)} pour {extent} ({area:.2f} mm2)"
                items.append(("fill", label, {layer: piece}, []))
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetname() == net:
                pos = p.GetPosition()
                label = f"{fp.GetReference()}.{p.GetNumber()} ({mm(pos.x):.2f}, {mm(pos.y):.2f})"
                shapes = {ly: layer_poly(p, ly) for ly in layers if p.IsOnLayer(ly)}
                items.append(("pad", label, shapes, [pos]))
    for t in board.GetTracks():
        if t.GetNetname() != net:
            continue
        if t.GetClass() == "PCB_VIA":
            pos = t.GetPosition()
            shapes = {ly: layer_poly(t, ly) for ly in layers if t.IsOnLayer(ly)}
            items.append(("via", f"via ({mm(pos.x):.2f}, {mm(pos.y):.2f})", shapes, [pos]))
        else:
            layer = t.GetLayer()
            items.append(
                (
                    "track",
                    board.GetLayerName(layer),
                    {layer: layer_poly(t, layer)},
                    [t.GetStart(), t.GetEnd()],
                )
            )
    return items


def joined(a, b, layer):
    """Whether two items of one net overlap on `layer`."""
    x = pcbnew.SHAPE_POLY_SET(a[2][layer])
    x.BooleanIntersection(b[2][layer])
    return x.OutlineCount() > 0


def groups(items):
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_layer = {}
    for i, it in enumerate(items):
        for layer, s in it[2].items():
            bb = s.BBox()
            by_layer.setdefault(layer, []).append(
                (bb.GetX(), bb.GetRight(), bb.GetY(), bb.GetBottom(), i)
            )
    for layer, boxes in by_layer.items():
        boxes.sort()
        active = []
        for x0, x1, y0, y1, i in boxes:
            active = [b for b in active if b[1] >= x0]
            for _, _, by0, by1, j in active:
                if (
                    by0 <= y1
                    and y0 <= by1
                    and find(i) != find(j)
                    and joined(items[i], items[j], layer)
                ):
                    parent[find(i)] = find(j)
            active.append((x0, x1, y0, y1, i))
    out = {}
    for i in range(len(items)):
        out.setdefault(find(i), []).append(i)
    return sorted(out.values(), key=len, reverse=True)


def main():
    board = pcbnew.LoadBoard(sys.argv[1])
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    layers = list(board.GetEnabledLayers().CuStack())
    nets = sys.argv[2:] or sorted(str(n) for n in board.GetNetsByName().keys() if str(n))
    total = 0
    for net in nets:
        items = net_items(board, net, layers)
        gs = groups(items)
        if len(gs) < 2:
            continue
        total += len(gs) - 1
        print(
            f"{net}: {len(gs)} groups, so {len(gs) - 1} unconnected (the rest: {len(gs[0])} items)"
        )
        rest = [p for i in gs[0] for p in items[i][3]]
        for g in gs[1:]:
            fills = [items[i][1] for i in g if items[i][0] == "fill"]
            pads = [items[i][1] for i in g if items[i][0] == "pad"]
            vias = [items[i][1] for i in g if items[i][0] == "via"]
            tracks = [items[i][1] for i in g if items[i][0] == "track"]
            print("  cut off:")
            for line in fills + pads + vias:
                print(f"    {line}")
            if tracks:
                print(f"    {len(tracks)} track segment(s) on {', '.join(sorted(set(tracks)))}")
            mine = [p for i in g for p in items[i][3]]
            if mine and rest:
                d, p, q = min(
                    [((q - p).EuclideanNorm(), p, q) for p in mine for q in rest],
                    key=lambda t: t[0],
                )
                print(
                    f"    nearest of the rest: ({mm(q.x):.2f}, {mm(q.y):.2f}), {mm(d):.2f} mm from"
                    f" ({mm(p.x):.2f}, {mm(p.y):.2f})"
                )
    print(f"cut-off copper: {total} unconnected")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
