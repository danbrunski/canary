"""List every via whose copper touches an SMD pad: via-in-pad, which needs plugged vias at the fab.

Run with KiCad's own python (it needs `pcbnew`):

    via_in_pad.py <board.kicad_pcb>

KiCad's DRC does not flag a via inside a pad of its own net, and plugged, capped vias raise the
board's cost a lot (operator, 2026-10-15), so nothing on CANary boards may put a via in a pad:
not hand-laid copper, not an autorouter. Exits 1 when it finds any, so a script can refuse the
result.
"""

import sys

import pcbnew


def vias_in_pads(board: "pcbnew.BOARD") -> list[str]:
    mm = pcbnew.ToMM
    hits = []
    for via in board.GetTracks():
        if via.GetClass() != "PCB_VIA":
            continue
        centre = via.GetPosition()
        radius = via.GetWidth(pcbnew.F_Cu) // 2
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                    continue
                for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                    if pad.IsOnLayer(layer) and pad.GetEffectivePolygon(layer).Collide(
                        centre, radius
                    ):
                        hits.append(
                            f"{via.GetNetname()} via at ({mm(centre.x):.2f}, {mm(centre.y):.2f}) "
                            f"in {fp.GetReference()} pad {pad.GetNumber()} [{pad.GetNetname()}]"
                            f"{' (locked)' if via.IsLocked() else ''}"
                        )
    return hits


def main() -> None:
    hits = vias_in_pads(pcbnew.LoadBoard(sys.argv[1]))
    print(f"via-in-pad: {len(hits)}")
    for h in hits:
        print("  " + h)
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
