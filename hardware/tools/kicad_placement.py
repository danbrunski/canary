"""Apply a placement table to a KiCad board and report footprint geometry.

Run with KiCad's own python (it needs `pcbnew`); on this machine that is the Windows install, driven
from WSL with Windows paths (see README.md):

    kicad_placement.py <board.kicad_pcb> <report.json> [--place table.json] [--save out.kicad_pcb]

The report lists every footprint's side, position, rotation, courtyard box and pads (number, net,
centre, bounding box, through-hole or not) in board millimetres, and every same-side pair whose
courtyards touch or overlap. `placement_review.py` scores and draws it. A placement table is JSON:
{"REF": [x_mm, y_mm, rotation_deg, "F" | "B"], ...}; parts it does not list keep their position. A
bottom-side rotation is the value KiCad shows for the flipped footprint.

--save writes the moved board, but the zone fills it carries are stale: refill and save with
`kicad-cli pcb drc --refill-zones --save-board` before committing (pcbnew's ZONE_FILLER, run from
here, missed pads that had just moved).
"""

import argparse
import json

import pcbnew


def _mm(v: int) -> float:
    return round(pcbnew.ToMM(v), 4)


def _box(bb: "pcbnew.BOX2I") -> list[float]:
    return [_mm(bb.GetX()), _mm(bb.GetY()), _mm(bb.GetRight()), _mm(bb.GetBottom())]


def apply_placement(board: "pcbnew.BOARD", table: dict[str, list]) -> list[str]:
    """Move each listed footprint; return the refs the table names but the board lacks."""
    by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
    missing = []
    for ref, (x, y, rot, side) in table.items():
        fp = by_ref.get(ref)
        if fp is None:
            missing.append(ref)
            continue
        if (side == "B") != fp.IsFlipped():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)  # toggles the side
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    return missing


def courtyard(fp: "pcbnew.FOOTPRINT") -> "pcbnew.SHAPE_POLY_SET":
    """The footprint's courtyard polygon, or its bounding box as one when it has no courtyard."""
    poly = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    if poly.OutlineCount():
        return poly
    bb = fp.GetBoundingBox(False, False)
    poly = pcbnew.SHAPE_POLY_SET()
    poly.NewOutline()
    for x, y in (
        (bb.GetLeft(), bb.GetTop()),
        (bb.GetRight(), bb.GetTop()),
        (bb.GetRight(), bb.GetBottom()),
        (bb.GetLeft(), bb.GetBottom()),
    ):
        poly.Append(x, y)
    return poly


def corner_box(poly: "pcbnew.SHAPE_POLY_SET") -> list[float]:
    """The box round a polygon's corners. Not BBox(): that adds the courtyard line's width (0.05 mm,
    0.15 mm on some vendor parts) on every side, so courtyards a fraction of a millimetre apart read
    as overlapping (C24/C29, 2026-10-20)."""
    pts = [
        o.CPoint(i)
        for o in map(poly.Outline, range(poly.OutlineCount()))
        for i in range(o.PointCount())
    ]
    xs, ys = [p.x for p in pts], [p.y for p in pts]
    return [_mm(min(xs)), _mm(min(ys)), _mm(max(xs)), _mm(max(ys))]


def courtyard_overlaps(fps: list["pcbnew.FOOTPRINT"]) -> list[dict]:
    """Same-side pairs whose courtyards collide, touching included: the test DRC's
    courtyards_overlap makes, so the two agree. The polygons are compared, not boxes round them: a
    QFN's courtyard steps in at its corners, and a box round it covers a part sitting clear in a
    step (U1/C7 on CANary-PWR)."""
    polys = [(fp, courtyard(fp)) for fp in fps]
    found = []
    for i, (a, pa) in enumerate(polys):
        for b, pb in polys[i + 1 :]:
            if a.IsFlipped() != b.IsFlipped() or not pa.BBox().Intersects(pb.BBox()):
                continue
            if pa.Collide(pb, 0):
                side = "B" if a.IsFlipped() else "F"
                found.append({"refs": [a.GetReference(), b.GetReference()], "side": side})
    return found


def footprint_record(fp: "pcbnew.FOOTPRINT") -> dict:
    back = fp.IsFlipped()
    through = (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
    pads = [
        {
            "n": p.GetNumber(),
            "net": p.GetNetname(),
            "x": _mm(p.GetPosition().x),
            "y": _mm(p.GetPosition().y),
            "bb": _box(p.GetBoundingBox()),
            "pth": p.GetAttribute() in through,
        }
        for p in fp.Pads()
    ]
    pos = fp.GetPosition()
    return {
        "ref": fp.GetReference(),
        "value": fp.GetValue(),
        "fp": str(fp.GetFPID().GetLibItemName()),
        "side": "B" if back else "F",
        "x": _mm(pos.x),
        "y": _mm(pos.y),
        "rot": fp.GetOrientationDegrees(),
        "crt": corner_box(courtyard(fp)),
        "pads": pads,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("report")
    ap.add_argument("--place", help="placement table (JSON) to apply before reporting")
    ap.add_argument("--save", help="write the moved board here (zone fills stay stale)")
    args = ap.parse_args()
    board = pcbnew.LoadBoard(args.board)
    missing = []
    if args.place:
        with open(args.place) as f:
            missing = apply_placement(board, json.load(f))
    fps = list(board.GetFootprints())
    report = {
        "fps": [footprint_record(fp) for fp in fps],
        "overlaps": courtyard_overlaps(fps),
        "missing": missing,
    }
    with open(args.report, "w") as f:
        json.dump(report, f)
    if args.save:
        pcbnew.SaveBoard(args.save, board, True)  # True: leave the .kicad_pro alone
    absent = ", ".join(missing) or "none"
    print(f"{len(report['fps'])} footprints reported; table refs not on the board: {absent}")


if __name__ == "__main__":
    main()
