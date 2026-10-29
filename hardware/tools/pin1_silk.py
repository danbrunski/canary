"""List every part whose silkscreen does not show the assembler where pin 1 is.

Run with KiCad's own python (it needs `pcbnew`):

    pin1_silk.py <board.kicad_pcb | library.pretty>

An assembler orients a part from the position file and the silkscreen, and the silkscreen is the
only drawing the Gerbers carry: the fab layer, where many library footprints draw their only pin-1
mark, is not exported. On HAT rev 2.1 (2026-10-28) PCBWay had to ask where pin 1 was on U3, U4 and
U6. U4's silkscreen (TI's D0008A_L) had no mark. The only mark on U3 and U6 was a filled square
beside pin 10: Ultra Librarian footprints draw one beside every tenth pin, as a counting aid. Every
part with three or more numbered pads is checked twice:

- **a mark beside the wrong pad**: a filled silk shape, or a "*" or "1", that is nearer another pad
  than pin 1;
- **no mark**: silk that looks the same after a turn that maps the land pattern onto itself (180
  degrees for an SOIC or an LGA, 90 too for a square QFN), so nothing on it tells pin 1 from the
  pad it turns into.

Parts left out of the position files (fiducials, solder jumpers) are not placed, so not checked.
Given a `.pretty` directory it checks every footprint in that library, so a project library can be
cleaned before a board uses it. Exits 1 when it finds any, so a script can refuse the board.
`pcbway_bom.py` runs the board check whenever it writes an order's files.
"""

import math
import sys
from collections.abc import Callable

import pcbnew

PIN_ONE = ("1", "A1")
MARK_TEXTS = ("*", "1")
PAD_MATCH_MM = 0.01  # two pad centres this close are the same place
TIE_MM = 0.05  # a corner mark sits as near pin 1 as its neighbour, and still names the corner
MAX_ERROR_MM = 0.005  # arc approximation when silk is turned into polygons
# A turned copy of the silk that differs from it by less than a 0.15 mm square (the board's
# narrowest silk line) differs by nothing anyone can see.
MIN_MARK_AREA_MM2 = 0.15 * 0.15

Pads = list["pcbnew.PAD"]
Turn = Callable[[float, float], tuple[float, float]]


def _mm2(area: float) -> float:
    return area / 1e12


def _silk_layer(fp: "pcbnew.FOOTPRINT") -> int:
    return int(pcbnew.B_SilkS if fp.GetLayer() == pcbnew.B_Cu else pcbnew.F_SilkS)


def _silk_items(fp: "pcbnew.FOOTPRINT") -> list["pcbnew.BOARD_ITEM"]:
    """The footprint's silk drawing: its shapes and any "*" or "1".

    Other text is a label, not a mark. Glyphs are never symmetric, so counting them would pass any
    land pattern that carries one: TI's D0008A_L passed on a stray "Designator9" that its board copy
    no longer has.
    """
    layer = _silk_layer(fp)
    items = []
    for item in fp.GraphicalItems():
        if item.GetLayer() != layer:
            continue
        if isinstance(item, pcbnew.PCB_TEXT) and (
            not item.IsVisible() or item.GetText().strip() not in MARK_TEXTS
        ):
            continue
        items.append(item)
    return items


def _is_mark(item: "pcbnew.BOARD_ITEM") -> bool:
    if isinstance(item, pcbnew.PCB_TEXT):
        return item.GetText().strip() in MARK_TEXTS
    return isinstance(item, pcbnew.PCB_SHAPE) and item.IsSolidFill()


def _pad_distance(pad: "pcbnew.PAD", point: "pcbnew.VECTOR2I") -> float:
    layer = pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
    poly = pad.GetEffectivePolygon(layer, pcbnew.ERROR_INSIDE)
    return float(pcbnew.ToMM(int(poly.SquaredDistance(point, False) ** 0.5)))


def wrong_marks(fp: "pcbnew.FOOTPRINT", pads: Pads, pin_one: "pcbnew.PAD") -> list[str]:
    """Pin-1 marks that sit beside some other pad."""
    found = []
    for item in _silk_items(fp):
        if not _is_mark(item):
            continue
        centre = item.GetBoundingBox().GetCenter()
        nearest = min(pads, key=lambda p: _pad_distance(p, centre))
        if _pad_distance(pin_one, centre) > _pad_distance(nearest, centre) + TIE_MM:
            found.append(
                f"silk mark at ({pcbnew.ToMM(centre.x):.2f}, {pcbnew.ToMM(centre.y):.2f}) is "
                f"beside pad {nearest.GetNumber()}, not pad {pin_one.GetNumber()}"
            )
    return found


def _symmetries(pads: Pads, centre: "pcbnew.VECTOR2I") -> list[int]:
    """The turns (degrees) that put every pad centre onto a pad centre."""
    cx, cy = pcbnew.ToMM(centre.x), pcbnew.ToMM(centre.y)
    points = [
        (pcbnew.ToMM(p.GetPosition().x) - cx, pcbnew.ToMM(p.GetPosition().y) - cy) for p in pads
    ]
    # a quarter turn on screen, y down: (x, y) -> (y, -x)
    quarter: dict[int, Turn] = {
        90: lambda x, y: (y, -x),
        180: lambda x, y: (-x, -y),
        270: lambda x, y: (-y, x),
    }
    return [
        degrees
        for degrees, turn in quarter.items()
        if all(any(math.dist(turn(*p), q) < PAD_MATCH_MM for q in points) for p in points)
    ]


def unmarked_turn(fp: "pcbnew.FOOTPRINT", pads: Pads) -> int | None:
    """A turn of the land pattern that the silk does not show, or None when every turn shows."""
    xs = [p.GetPosition().x for p in pads]
    ys = [p.GetPosition().y for p in pads]
    centre = pcbnew.VECTOR2I(sum(xs) // len(xs), sum(ys) // len(ys))
    silk = pcbnew.SHAPE_POLY_SET()
    for item in _silk_items(fp):
        item.TransformShapeToPolygon(
            silk, item.GetLayer(), 0, pcbnew.FromMM(MAX_ERROR_MM), pcbnew.ERROR_INSIDE
        )
    for degrees in _symmetries(pads, centre):
        turned = pcbnew.SHAPE_POLY_SET(silk)
        turned.Rotate(pcbnew.EDA_ANGLE(degrees, pcbnew.DEGREES_T), centre)
        diff = pcbnew.SHAPE_POLY_SET(silk)
        diff.BooleanXor(turned)
        if _mm2(diff.Area()) < MIN_MARK_AREA_MM2:
            return degrees
    return None


def footprint_problems(fp: "pcbnew.FOOTPRINT") -> list[str]:
    if fp.GetAttributes() & pcbnew.FP_EXCLUDE_FROM_POS_FILES:
        return []
    pads = [p for p in fp.Pads() if p.GetNumber() and p.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH]
    ones = [p for p in pads if p.GetNumber() in PIN_ONE]
    if len({p.GetNumber() for p in pads}) < 3 or not ones:
        return []
    found = wrong_marks(fp, pads, ones[0])
    turn = unmarked_turn(fp, pads)
    if turn is not None:
        found.append(f"no pin-1 mark: the silkscreen looks the same turned {turn} degrees")
    return found


def board_problems(board: "pcbnew.BOARD") -> list[str]:
    problems = []
    for fp in sorted(board.GetFootprints(), key=lambda f: f.GetReference()):
        label = f"{fp.GetReference()} ({fp.GetValue()}, {fp.GetFPID().GetLibItemName()})"
        problems += [f"{label}: {f}" for f in footprint_problems(fp)]
    return problems


def library_problems(path: str) -> list[str]:
    problems = []
    for name in sorted(pcbnew.FootprintEnumerate(path)):
        problems += [f"{name}: {f}" for f in footprint_problems(pcbnew.FootprintLoad(path, name))]
    return problems


def main() -> None:
    path = sys.argv[1]
    if path.rstrip("/").endswith(".pretty"):
        problems = library_problems(path)
    else:
        problems = board_problems(pcbnew.LoadBoard(path))
    print(f"pin-1 silk: {len(problems)}")
    for p in problems:
        print("  " + p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
