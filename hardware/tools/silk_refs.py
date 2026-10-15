"""Place silkscreen reference designators where they collide with nothing; hide the rest.

Run with KiCad's own python (it needs `pcbnew`), or import `place_refs` from a board generator:

    silk_refs.py <board.kicad_pcb> [--out other.kicad_pcb] [--size 0.8] [--thickness 0.15]

Each visible reference is resized, then tried at spots around its part's courtyard (above, below,
left, right, centred and corner-aligned, at 0 and 90 degrees, and finally on the part's own body
for large parts). A spot is taken only if the text clears every pad on that side (through-hole pads
count on both sides), all silkscreen already there, fiducial mask openings and the board edge. A
reference with no free spot is hidden; the fab-layer copy stays. Footprint text drawn twice in
the same place (Ultra Librarian footprints do it with their pin-1 "*") is reported, not removed:
fix it in the library. Removing footprint items from pcbnew's python corrupted the SWIG wrappers
in KiCad 10.0.5 (GraphicalItems() came back as a raw std::deque pointer).

The result depends only on the board, so a generator that calls it reproduces the same silk.
"""

import argparse
from collections.abc import Iterator
from typing import Any

import pcbnew

Rect = tuple[float, float, float, float]

EDGE_MARGIN_MM = 0.5  # silk-to-board-edge
PAD_MARGIN_MM = 0.1  # solder-mask expansion around a pad, plus a little
FIDUCIAL_MARGIN_MM = 0.6  # Fiducial_1mm_Mask2mm opens 2 mm of mask around a 1 mm pad
TEXT_GAP_MM = 0.1  # between two silk items
COURTYARD_GAP_MM = 0.15  # between a reference and its own part
BODY_TEXT_MIN_AREA_MM2 = 30.0  # parts big enough to carry their reference on their body
SLIDE_STEP_MM = 0.4  # label positions tried along each side of a part
OUTER_RING_MM = 0.6  # a second, farther ring of positions when the first is full
# Labelled first, so passives cannot take every spot around a chip: semiconductors, magnetics,
# connectors and switches, then passives, then fixtures (fiducials, mounting holes).
PRIORITY = ("U", "Q", "D", "L", "J", "F", "SW", "Y")
LAST = ("FID", "MH")


def _rect(bb: "pcbnew.BOX2I", grow: float = 0.0) -> Rect:
    mm = pcbnew.ToMM
    return (
        mm(bb.GetX()) - grow,
        mm(bb.GetY()) - grow,
        mm(bb.GetRight()) + grow,
        mm(bb.GetBottom()) + grow,
    )


def _hit(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def duplicate_texts(fp: "pcbnew.FOOTPRINT") -> list[str]:
    """Footprint texts that repeat another one exactly (text, layer, position)."""
    seen, dups = set(), []
    for item in fp.GraphicalItems():
        if not isinstance(item, pcbnew.PCB_TEXT):
            continue
        pos = item.GetPosition()
        key = (item.GetText(), item.GetLayer(), pos.x, pos.y)
        if key in seen:
            dups.append(f"{fp.GetReference()} {item.GetText()!r}")
        seen.add(key)
    return dups


def _obstacles(board: "pcbnew.BOARD", back: bool) -> list[Rect]:
    """Everything a silk reference on this side must clear, except other references."""
    copper, silk = (pcbnew.B_Cu, pcbnew.B_SilkS) if back else (pcbnew.F_Cu, pcbnew.F_SilkS)
    out = []
    for fp in board.GetFootprints():
        fiducial = fp.GetReference().startswith("FID")
        for pad in fp.Pads():
            if pad.IsOnLayer(copper):
                margin = FIDUCIAL_MARGIN_MM if fiducial else PAD_MARGIN_MM
                out.append(_rect(pad.GetBoundingBox(), margin))
        for item in fp.GraphicalItems():
            if item.GetLayer() == silk:
                out.append(_rect(item.GetBoundingBox(), TEXT_GAP_MM))
        value = fp.Value()
        if value.IsVisible() and value.GetLayer() == silk:
            out.append(_rect(value.GetBoundingBox(), TEXT_GAP_MM))
    for item in board.GetDrawings():
        if item.GetLayer() == silk:
            out.append(_rect(item.GetBoundingBox(), TEXT_GAP_MM))
    return out


def _slide(lo: float, hi: float) -> list[float]:
    """Positions from lo to hi in SLIDE_STEP_MM steps, nearest the middle first."""
    n = max(int((hi - lo) / SLIDE_STEP_MM), 0)
    mid = (lo + hi) / 2
    steps = [lo + i * (hi - lo) / max(n, 1) for i in range(n + 1)]
    return sorted(steps, key=lambda v: abs(v - mid))


def _candidates(crt: Rect, w: float, h: float) -> Iterator[tuple[float, float]]:
    """Text centres around a courtyard for a text box of w x h, nearest-looking first."""
    x0, y0, x1, y1 = crt
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    for g in (COURTYARD_GAP_MM, COURTYARD_GAP_MM + OUTER_RING_MM):
        above, below = y0 - g - h / 2, y1 + g + h / 2
        left, right = x0 - g - w / 2, x1 + g + w / 2
        yield from [(mx, above), (mx, below), (left, my), (right, my)]
        for x in _slide(x0 - w / 2, x1 + w / 2):
            yield from [(x, above), (x, below)]
        for y in _slide(y0 - h / 2, y1 + h / 2):
            yield from [(left, y), (right, y)]


def _place_one(
    fp: "pcbnew.FOOTPRINT", taken: list[Rect], outline: Rect, size: float, thickness: float
) -> bool:
    ref = fp.Reference()
    ref.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(size), pcbnew.FromMM(size)))
    ref.SetTextThickness(pcbnew.FromMM(thickness))
    crt = _rect(fp.GetCourtyard(_courtyard_layer(fp)).BBox())
    area = (crt[2] - crt[0]) * (crt[3] - crt[1])
    reach = 4 * size + COURTYARD_GAP_MM + OUTER_RING_MM  # no label lands farther out than this
    zone = (crt[0] - reach, crt[1] - reach, crt[2] + reach, crt[3] + reach)
    near = [t for t in taken if _hit(t, zone)]
    spots = []
    for angle in (0.0, 90.0):
        ref.SetTextAngleDegrees(angle)
        ref.SetPosition(pcbnew.VECTOR2I(0, 0))
        bx0, by0, bx1, by1 = _rect(ref.GetBoundingBox())
        spots += [(angle, c) for c in _candidates(crt, bx1 - bx0, by1 - by0)]
    if area >= BODY_TEXT_MIN_AREA_MM2:
        spots += [(a, ((crt[0] + crt[2]) / 2, (crt[1] + crt[3]) / 2)) for a in (0.0, 90.0)]
    for angle, (cx, cy) in spots:
        ref.SetTextAngleDegrees(angle)
        ref.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy)))
        box = _rect(ref.GetBoundingBox())
        inside = (
            box[0] >= outline[0]
            and box[1] >= outline[1]
            and box[2] <= outline[2]
            and box[3] <= outline[3]
        )
        if inside and not any(_hit(box, t) for t in near):
            g = TEXT_GAP_MM
            taken.append((box[0] - g, box[1] - g, box[2] + g, box[3] + g))
            return True
    ref.SetVisible(False)
    return False


def _courtyard_layer(fp: "pcbnew.FOOTPRINT") -> int:
    return int(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)


def place_refs(board: "pcbnew.BOARD", size: float = 0.8, thickness: float = 0.15) -> dict[str, Any]:
    """Place every visible reference designator; return counts and any duplicated footprint text."""
    edge = _rect(board.GetBoardEdgesBoundingBox())
    outline = (
        edge[0] + EDGE_MARGIN_MM,
        edge[1] + EDGE_MARGIN_MM,
        edge[2] - EDGE_MARGIN_MM,
        edge[3] - EDGE_MARGIN_MM,
    )
    fps = list(board.GetFootprints())
    dups = [d for fp in fps for d in duplicate_texts(fp)]
    taken = {False: _obstacles(board, False), True: _obstacles(board, True)}

    def order(fp: "pcbnew.FOOTPRINT") -> tuple[int, float, str]:
        ref = fp.GetReference()
        prefix = ref.rstrip("0123456789")
        rank = 2 if prefix in LAST else 0 if prefix in PRIORITY else 1
        c = _rect(fp.GetCourtyard(_courtyard_layer(fp)).BBox())
        return (rank, (c[2] - c[0]) * (c[3] - c[1]), ref)

    placed = hidden = 0
    for fp in sorted((f for f in fps if f.Reference().IsVisible()), key=order):
        if _place_one(fp, taken[fp.IsFlipped()], outline, size, thickness):
            placed += 1
        else:
            hidden += 1
    return {"placed": placed, "hidden": hidden, "duplicate_footprint_texts": dups}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("--out", help="write here instead of over the input")
    ap.add_argument("--size", type=float, default=0.8, help="text height, mm")
    ap.add_argument("--thickness", type=float, default=0.15, help="stroke, mm")
    args = ap.parse_args()
    board = pcbnew.LoadBoard(args.board)
    print(place_refs(board, args.size, args.thickness))
    pcbnew.SaveBoard(args.out or args.board, board, True)  # True: leave the .kicad_pro alone


if __name__ == "__main__":
    main()
