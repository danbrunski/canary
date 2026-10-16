"""Add a Freerouting session's routes to a KiCad board, keeping the copper already on it.

Run with KiCad's own python (it needs `pcbnew`):

    ses_import.py <board.kicad_pcb> <routes.ses> [--out other.kicad_pcb]

KiCad's own SES import deletes every track first. This one adds only what the router made:
wires and vias marked `fix` or `protect` came from the board (hand-laid copper, and the fixed
wires dsn_export.py drew over the power pours), so they are skipped. Zones are left unfilled;
refill them afterwards (kicad-cli pcb drc --refill-zones --save-board).
"""

import argparse
import re

import pcbnew

Node = list  # a parsed s-expression: [head, child, ...]; atoms are str


def parse(text: str) -> Node:
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+', text)
    stack: list[Node] = [[]]
    for tok in tokens:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(tok[1:-1] if tok.startswith('"') else tok)
    return stack[0][0]


def children(node: Node, head: str) -> list[Node]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def child(node: Node, head: str) -> Node | None:
    found = children(node, head)
    return found[0] if found else None


def fixed(node: Node) -> bool:
    kind = child(node, "type")
    return kind is not None and kind[1] in ("fix", "protect")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board")
    ap.add_argument("ses")
    ap.add_argument("--out")
    args = ap.parse_args()

    ses = parse(open(args.ses).read())
    routes = child(ses, "routes")
    res = child(routes, "resolution")
    per_mm = {"um": 1000.0, "mm": 1.0, "mil": 1000 / 25.4, "inch": 1 / 25.4}[res[1]] * float(res[2])

    def xy(x: str, y: str) -> "pcbnew.VECTOR2I":
        return pcbnew.VECTOR2I(pcbnew.FromMM(float(x) / per_mm), pcbnew.FromMM(-float(y) / per_mm))

    stacks = {}
    for ps in children(child(routes, "library_out") or ["library_out"], "padstack"):
        circle = child(child(ps, "shape"), "circle")
        stacks[ps[1]] = float(circle[2]) / per_mm
    board = pcbnew.LoadBoard(args.board)
    layers = {board.GetLayerName(i): i for i in range(pcbnew.PCB_LAYER_ID_COUNT)}
    nets = board.GetNetsByName()
    tracks = vias = skipped = 0
    for net in children(child(routes, "network_out"), "net"):
        ni = nets[net[1]]
        for w in children(net, "wire"):
            if fixed(w):
                skipped += 1
                continue
            path = child(w, "path")
            layer, width, coords = layers[path[1]], float(path[2]) / per_mm, path[3:]
            pts = [xy(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]
            for a, b in zip(pts, pts[1:], strict=False):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(a)
                t.SetEnd(b)
                t.SetWidth(pcbnew.FromMM(width))
                t.SetLayer(layer)
                t.SetNet(ni)
                board.Add(t)
                tracks += 1
        for v in children(net, "via"):
            if fixed(v):
                skipped += 1
                continue
            m = re.search(r"_(\d+):(\d+)_um", v[1])
            dia, drill = (int(m[1]) / 1000, int(m[2]) / 1000) if m else (stacks[v[1]], None)
            via = pcbnew.PCB_VIA(board)
            board.Add(via)
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetPosition(xy(v[2], v[3]))
            via.SetWidth(pcbnew.FromMM(dia))
            via.SetDrill(pcbnew.FromMM(drill if drill else dia / 2))
            via.SetNet(ni)
            vias += 1
    pcbnew.SaveBoard(args.out or args.board, board, True)  # True: leave the .kicad_pro alone
    print(f"added {tracks} track segments and {vias} vias; skipped {skipped} fixed items")


if __name__ == "__main__":
    main()
