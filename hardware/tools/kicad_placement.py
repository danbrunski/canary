"""Apply a placement table to a KiCad board and report footprint geometry.

Run with KiCad's own python (it needs `pcbnew`); on this machine that is the Windows install, driven
from WSL with Windows paths (see README.md):

    kicad_placement.py <board.kicad_pcb> <report.json> [--place table.json] [--save out.kicad_pcb]

The report lists every footprint's side, position, rotation, courtyard box and pads (number, net,
centre, bounding box, through-hole or not) in board millimetres. `placement_review.py` scores and
draws it. A placement table is JSON: {"REF": [x_mm, y_mm, rotation_deg, "F" | "B"], ...}; parts it
does not list keep their position. A bottom-side rotation is the value KiCad shows for the flipped
footprint.

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


def footprint_record(fp: "pcbnew.FOOTPRINT") -> dict:
    back = fp.IsFlipped()
    courtyard = fp.GetCourtyard(pcbnew.B_CrtYd if back else pcbnew.F_CrtYd)
    crt = courtyard.BBox() if courtyard.OutlineCount() else fp.GetBoundingBox(False, False)
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
        "crt": _box(crt),
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
    report = {"fps": [footprint_record(fp) for fp in board.GetFootprints()], "missing": missing}
    with open(args.report, "w") as f:
        json.dump(report, f)
    if args.save:
        pcbnew.SaveBoard(args.save, board, True)  # True: leave the .kicad_pro alone
    absent = ", ".join(missing) or "none"
    print(f"{len(report['fps'])} footprints reported; table refs not on the board: {absent}")


if __name__ == "__main__":
    main()
