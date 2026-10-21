"""Score and draw a footprint placement from a kicad_placement.py report.

    placement_review.py <report.json> <config.json> [--png out.png]

Prints, then exits non-zero if any problem was found:
- problems: courtyard overlaps on one side (the pairs kicad_placement.py found colliding, touching
  included, as DRC does), a part sitting over another part's through-hole pins on the far side,
  courtyards outside the outline (edge parts get a tolerance), parts inside a keep-out;
- ratsnest: the minimum-spanning-tree length of every net (what the router has to connect), as
  signal, power and GND totals, with the longest nets named;
- pin distances: for each configured [part, target ref, target pin], how far the part's pad on the
  target pin's net is from that pin, worst first. Use it for decoupling capacitors, gate drives,
  Kelvin sense and anything else that has to sit close.

--png draws both sides seen from the top (the bottom is not mirrored): courtyards, pads coloured
by net class, and the ratsnest (GND left out; power nets red, signals blue).

The config is JSON; see hardware/canary-pwr-rev1.0/gen/placement_review.json:
    outline [x0, y0, x1, y1]; keepouts [{"name", "side": "F"|"B", "rect"}]; edge_refs [...];
    edge_tolerance_mm; power_nets [...]; checks [[part, target_ref, target_pin], ...]
"""

import argparse
import json
import math
import sys
from typing import Any

from PIL import Image, ImageDraw, ImageFont

Rect = tuple[float, float, float, float]
Point = tuple[float, float]
Footprint = dict[str, Any]
Nets = dict[str, tuple[float, list[tuple[Point, Point]]]]  # net -> (tree length, tree edges)

MIN_OVERLAP_MM = 0.02  # a courtyard may touch a keep-out or pin margin; more is a problem
PIN_MARGIN_MM = 0.25  # clearance kept around a through-hole pin on the far side
PX_PER_MM = 13
MARGIN_MM = 2.0


def overlaps(a: Rect, b: Rect, margin: float = MIN_OVERLAP_MM) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > margin and min(a[3], b[3]) - max(a[1], b[1]) > margin


def placement_problems(
    fps: list[Footprint], collisions: list[dict[str, Any]], cfg: dict[str, Any]
) -> list[str]:
    found = {f"overlap {'/'.join(c['refs'])} ({c['side']})" for c in collisions}
    for a in fps:
        for pad in (p for p in a["pads"] if p["pth"]):
            x0, y0, x1, y1 = pad["bb"]
            zone = (x0 - PIN_MARGIN_MM, y0 - PIN_MARGIN_MM, x1 + PIN_MARGIN_MM, y1 + PIN_MARGIN_MM)
            for b in fps:
                fixture = b["ref"].startswith(("MH", "FID"))
                if (
                    b is not a
                    and b["side"] != a["side"]
                    and not fixture
                    and overlaps(zone, b["crt"])
                ):
                    found.add(f"{b['ref']} ({b['side']}) over {a['ref']} pin {pad['n']}")
    x0, y0, x1, y1 = cfg["outline"]
    for f in fps:
        c = f["crt"]
        tol = cfg.get("edge_tolerance_mm", 0.6) if f["ref"] in cfg.get("edge_refs", []) else 0.0
        if c[0] < x0 - tol or c[1] < y0 - tol or c[2] > x1 + tol or c[3] > y1 + tol:
            found.add(f"{f['ref']} outside the outline")
        for k in cfg.get("keepouts", []):
            if f["side"] == k["side"] and not f["ref"].startswith(("MH", "FID")):
                if overlaps(c, tuple(k["rect"])):
                    found.add(f"{f['ref']} in keep-out '{k['name']}'")
    return sorted(found)


def spanning_tree(points: list[Point]) -> tuple[float, list[tuple[Point, Point]]]:
    """Prim's minimum spanning tree: total length and edges."""
    pts = list(dict.fromkeys((round(x, 3), round(y, 3)) for x, y in points))
    if len(pts) < 2:
        return 0.0, []
    best = {j: (math.dist(pts[0], pts[j]), 0) for j in range(1, len(pts))}
    total, edges = 0.0, []
    while best:
        j, (d, i) = min(best.items(), key=lambda kv: kv[1][0])
        del best[j]
        total += d
        edges.append((pts[i], pts[j]))
        for k in best:
            dk = math.dist(pts[j], pts[k])
            if dk < best[k][0]:
                best[k] = (dk, j)
    return total, edges


def ratsnest(fps: list[Footprint]) -> Nets:
    nets: dict[str, list[Point]] = {}
    for f in fps:
        for p in f["pads"]:
            if p["net"] and not p["net"].startswith("unconnected-"):
                nets.setdefault(p["net"], []).append((p["x"], p["y"]))
    return {name: spanning_tree(pts) for name, pts in nets.items()}


def pin_distances(fps: list[Footprint], checks: list[list[str]]) -> list[tuple[float, str]]:
    by_ref = {f["ref"]: f for f in fps}
    out = []
    for part, ref, pin in checks:
        target = next(p for p in by_ref[ref]["pads"] if p["n"] == pin)
        pads = [p for p in by_ref[part]["pads"] if p["net"] == target["net"]]
        if not pads:
            out.append((math.inf, f"{part} has no pad on {ref}.{pin}'s net"))
            continue
        d = min(math.dist((p["x"], p["y"]), (target["x"], target["y"])) for p in pads)
        other = " (other side)" if by_ref[part]["side"] != by_ref[ref]["side"] else ""
        out.append((d, f"{part}->{ref}.{pin} {d:.1f} mm{other}"))
    return sorted(out, reverse=True)


def short(net: str) -> str:
    return net.split("/")[-1]


def summary(nets: Nets, power: set[str]) -> list[str]:
    sig = {k: v[0] for k, v in nets.items() if k not in power}
    pwr = {k: v[0] for k, v in nets.items() if k in power and k != "GND"}
    longest = sorted(sig.items(), key=lambda kv: -kv[1])[:12]
    return [
        f"ratsnest: signal {sum(sig.values()):.0f} mm, power {sum(pwr.values()):.0f} mm, "
        f"GND {nets.get('GND', (0.0, []))[0]:.0f} mm",
        "longest signal nets: " + ", ".join(f"{short(k)} {v:.0f}" for k, v in longest),
        "power nets: "
        + ", ".join(f"{short(k)} {v:.0f}" for k, v in sorted(pwr.items(), key=lambda kv: -kv[1])),
    ]


def pad_colour(net: str, power: set[str]) -> tuple[int, int, int]:
    if net == "GND":
        return (0, 150, 0)
    if net in power:
        return (220, 40, 40)
    return (90, 90, 90)


def draw_side(
    fps: list[Footprint], nets: Nets, cfg: dict[str, Any], side: str, power: set[str]
) -> Image.Image:
    x0, y0, x1, y1 = cfg["outline"]

    def at(x: float, y: float) -> Point:
        return ((x - x0 + MARGIN_MM) * PX_PER_MM, (y - y0 + MARGIN_MM) * PX_PER_MM)

    size = (int((x1 - x0 + 2 * MARGIN_MM) * PX_PER_MM), int((y1 - y0 + 2 * MARGIN_MM) * PX_PER_MM))
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default()
    d.rectangle([at(x0, y0), at(x1, y1)], outline="black", width=2)
    for k in cfg.get("keepouts", []):
        if k["side"] == side:
            kx0, ky0, kx1, ky1 = k["rect"]
            d.rectangle([at(kx0, ky0), at(kx1, ky1)], outline=(200, 0, 200), width=2)
    for f in fps:
        mine = f["side"] == side
        if not mine and not any(p["pth"] for p in f["pads"]):
            continue
        c = f["crt"]
        d.rectangle([at(c[0], c[1]), at(c[2], c[3])], outline=(0, 90, 200) if mine else (200,) * 3)
        for p in f["pads"]:
            if mine or p["pth"]:
                bx0, by0, bx1, by1 = p["bb"]
                d.rectangle([at(bx0, by0), at(bx1, by1)], fill=pad_colour(p["net"], power))
        if mine:
            d.text(at((c[0] + c[2]) / 2 - 1.2, (c[1] + c[3]) / 2 - 0.6), f["ref"], "black", font)
    for name, (_, edges) in nets.items():
        if name != "GND":
            colour = (255, 120, 120) if name in power else (60, 180, 255)
            for a, b in edges:
                d.line([at(*a), at(*b)], fill=colour, width=1)
    d.text((5, 3), "TOP" if side == "F" else "BOTTOM, seen from the top", "black", font)
    return im


def render(fps: list[Footprint], nets: Nets, cfg: dict[str, Any], path: str) -> None:
    power = set(cfg.get("power_nets", []))
    top, bottom = (draw_side(fps, nets, cfg, s, power) for s in ("F", "B"))
    canvas = Image.new("RGB", (top.width + bottom.width + 10, top.height), "white")
    canvas.paste(top, (0, 0))
    canvas.paste(bottom, (top.width + 10, 0))
    canvas.save(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report")
    ap.add_argument("config")
    ap.add_argument("--png")
    args = ap.parse_args()
    with open(args.report) as f:
        report = json.load(f)
    fps = report["fps"]
    with open(args.config) as f:
        cfg = json.load(f)
    nets = ratsnest(fps)
    for line in summary(nets, set(cfg.get("power_nets", []))):
        print(line)
    distances = pin_distances(fps, cfg.get("checks", []))
    if distances:
        print("pin distances (worst first): " + "; ".join(t for _, t in distances[:14]))
    problems = placement_problems(fps, report["overlaps"], cfg)
    print(f"problems ({len(problems)}): " + ("; ".join(problems) if problems else "none"))
    if args.png:
        render(fps, nets, cfg, args.png)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
