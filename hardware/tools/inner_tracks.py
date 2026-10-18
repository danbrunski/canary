"""List every track and arc on an inner plane layer (type power): copper that cuts a plane.

Run with KiCad's own python (it needs `pcbnew`):

    inner_tracks.py <board.kicad_pcb>

KiCad's DRC says nothing about a track on a plane layer. Both Freerouting and freerouting-rs routed
signals on In1 although the DSN declares it `(type power)` (2026-10-16: CAN_WAKE on the
36-unconnected board). A mixed or signal inner layer may carry tracks: CANary-PWR's In2 is mixed
since 2026-10-18, and rule areas on it keep tracks out of its power islands, which DRC enforces.
Exits 1 when it finds any, so a script can refuse the result.
"""

import sys

import pcbnew


def inner_tracks(board: "pcbnew.BOARD") -> list[str]:
    mm = pcbnew.ToMM
    hits = []
    for track in board.GetTracks():
        layer = track.GetLayer()
        if track.GetClass() == "PCB_VIA" or not pcbnew.IsInnerCopperLayer(layer):
            continue
        if board.GetLayerType(layer) != pcbnew.LT_POWER:
            continue
        a, b = track.GetStart(), track.GetEnd()
        hits.append(
            f"{track.GetNetname()} on {board.GetLayerName(track.GetLayer())}: "
            f"({mm(a.x):.2f}, {mm(a.y):.2f}) -> ({mm(b.x):.2f}, {mm(b.y):.2f}), "
            f"{mm(track.GetLength()):.2f} mm{' (locked)' if track.IsLocked() else ''}"
        )
    return hits


def main() -> None:
    hits = inner_tracks(pcbnew.LoadBoard(sys.argv[1]))
    print(f"inner-layer tracks: {len(hits)}")
    for h in hits:
        print("  " + h)
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
