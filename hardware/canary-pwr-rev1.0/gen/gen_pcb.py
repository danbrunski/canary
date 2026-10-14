# ruff: noqa: E501, N802, N806
"""Build canary-pwr.kicad_pcb on the HAT rev 2.1 template from the schematic netlist (run with KiCad's python).

Board: the HAT+ 65 x 56 outline, no ribbon slot, no overhang. Everything a person touches is mounted on an
edge and reached from the side of the stack. Power stage on top; supervisor and Pi interface on the bottom,
clear of the cooler's fan inlet.

Usage: gen_pcb.py <hat_template.kicad_pcb> <netlist.xml> <out.kicad_pcb> <project_dir_windows> <syslib_fp_dir>
"""

import os
import sys
import xml.etree.ElementTree as ET

import pcbnew

tpl_path, net_xml, out_path, projdir, sysfp = sys.argv[1:6]
MM = pcbnew.FromMM
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design  # noqa: E402
import gen_sch  # noqa: E402


def V(x, y):
    return pcbnew.VECTOR2I(MM(x), MM(y))


b = pcbnew.LoadBoard(tpl_path)
tb = b.GetTitleBlock()
tb.SetTitle("CANary-PWR")
tb.SetRevision("1.0")
tb.SetDate("2026-10-13")
tb.SetCompany("CANary")
tb.SetComment(0, "First-pass placement, unrouted. Design record: docs/CANary-PWR-Spec.md")
tb.SetComment(
    1,
    "HAT+ 65 x 56 outline from HAT rev 2.1, no ribbon slot. Edge-mounted connectors/LEDs/button/SWD; parts on both sides",
)
for i in range(2, 9):
    tb.SetComment(i, "")

# ---------------------------------------------------------------- outline
X0, X1, Y0, Y1, R = 100.0, 165.0, 44.0, 100.0, 3.0
k = R * (1 - 0.70710678)


def edge(kind, *pts):
    s = pcbnew.PCB_SHAPE(b)
    if kind == "seg":
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(*pts[0]))
        s.SetEnd(V(*pts[1]))
    else:
        s.SetShape(pcbnew.SHAPE_T_ARC)
        s.SetArcGeometry(V(*pts[0]), V(*pts[1]), V(*pts[2]))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(MM(0.1))
    b.Add(s)


edge("seg", (X0 + R, Y0), (X1 - R, Y0))
edge("arc", (X1 - R, Y0), (X1 - k, Y0 + k), (X1, Y0 + R))
edge("seg", (X1, Y0 + R), (X1, Y1 - R))
edge("arc", (X1, Y1 - R), (X1 - k, Y1 - k), (X1 - R, Y1))
edge("seg", (X1 - R, Y1), (X0 + R, Y1))
edge("arc", (X0 + R, Y1), (X0 + k, Y1 - k), (X0, Y1 - R))
edge("seg", (X0, Y1 - R), (X0, Y0 + R))
edge("arc", (X0, Y0 + R), (X0 + k, Y0 + k), (X0 + R, Y0))

# Active Cooler fan inlet, ASSUMED position: 30 x 30 fan block at the USB end, header side (product brief p.5).
FAN = (136.5, 53.0, 162.5, 79.0)
ra = pcbnew.ZONE(b)
ra.SetIsRuleArea(True)
ra.SetZoneName("Fan inlet keep-out (ASSUMED position - verify on the Pi)")
ra.SetLayer(pcbnew.B_Cu)
o = ra.Outline()
o.NewOutline()
for px, py in [(FAN[0], FAN[1]), (FAN[2], FAN[1]), (FAN[2], FAN[3]), (FAN[0], FAN[3])]:
    o.Append(MM(px), MM(py))
ra.SetDoNotAllowFootprints(True)
ra.SetDoNotAllowTracks(False)
ra.SetDoNotAllowVias(False)
ra.SetDoNotAllowZoneFills(False)
ra.SetDoNotAllowPads(True)
b.Add(ra)
for txt, x, y, layer, size in [
    (
        "Fan inlet keep-out on B.Cu: position ASSUMED from the Active Cooler brief, verify",
        101.0,
        43.0,
        pcbnew.Dwgs_User,
        0.8,
    ),
    ("CANary-PWR Rev 1.0 2026", 132.5, 80.0, pcbnew.B_SilkS, 1.0),
]:
    t = pcbnew.PCB_TEXT(b)
    t.SetText(txt)
    t.SetPosition(V(x, y))
    t.SetLayer(layer)
    t.SetTextSize(V(size, size))
    t.SetTextThickness(MM(size * 0.15))
    if layer == pcbnew.B_SilkS:
        t.SetMirrored(True)
    else:
        t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
    b.Add(t)

# ---------------------------------------------------------------- netlist
root = ET.parse(net_xml).getroot()
comps = []
for c in root.iter("comp"):
    sp = c.find("sheetpath")
    comps.append(
        dict(
            ref=c.get("ref"),
            value=c.find("value").text,
            fp=c.find("footprint").text,
            path=sp.get("tstamps") + c.find("tstamps").text,
            sheet=sp.get("names").strip("/"),
            dnp=any(p.get("name") == "dnp" for p in c.iter("property")),
            fields={
                f.get("name"): (f.text or "")
                for f in c.iter("field")
                if f.get("name") != "Footprint"
            },
        )
    )


def net_name(name):
    """The XML netlist unescapes '/' in auto-named nets; the schematic keeps '{slash}'."""
    if name.startswith("unconnected-("):
        return name.replace("/", "{slash}")
    return name


pad_net, nets = {}, {}
for n in root.iter("net"):
    for node in n.iter("node"):
        pad_net[(node.get("ref"), node.get("pin"))] = net_name(n.get("name"))
    nets[net_name(n.get("name"))] = None
for name in sorted(nets):
    ni = pcbnew.NETINFO_ITEM(b, name)
    b.Add(ni)
    nets[name] = ni

libs = gen_sch.Libs(projdir)
gen_sch.assign_refs(libs, design.PARTS)
group_of = {p["ref"]: (p["sheet"], p["group"]) for p in design.PARTS}


def load_fp(fpid):
    lib, name = fpid.split(":", 1)
    path = (
        os.path.join(projdir, "lib", "footprints.pretty")
        if lib == "canary-pwr"
        else os.path.join(sysfp, lib + ".pretty")
    )
    fp = pcbnew.FootprintLoad(path, name)
    if fp is None:
        raise SystemExit(f"cannot load {fpid} from {path}")
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def to_back(fp):
    try:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    except Exception:
        fp.Flip(fp.GetPosition(), False)


def bbox(fp):
    bb = fp.GetBoundingBox(False, False)
    return (
        pcbnew.ToMM(bb.GetX()),
        pcbnew.ToMM(bb.GetY()),
        pcbnew.ToMM(bb.GetRight()),
        pcbnew.ToMM(bb.GetBottom()),
    )


def move_bbox_to(fp, x, y):
    bx0, by0, _, _ = bbox(fp)
    pos = fp.GetPosition()
    fp.SetPosition(V(x + pcbnew.ToMM(pos.x) - bx0, y + pcbnew.ToMM(pos.y) - by0))


# ---------------------------------------------------------------- keep-outs per side
MH_POS = [(103.5, 47.5), (161.5, 47.5), (103.5, 96.5), (161.5, 96.5)]
FID_POS = [(161.5, 53.5), (161.5, 91.0), (114.0, 52.0)]  # top
FID_POS_B = [(133.0, 53.0), (161.5, 91.0), (114.0, 52.0)]  # bottom: clear of the fan keep-out
HOLES = [(x - 3.6, y - 3.6, x + 3.6, y + 3.6) for x, y in MH_POS]
FIDS = [(x - 1.6, y - 1.6, x + 1.6, y + 1.6) for x, y in FID_POS]
FIDS_B = [(x - 1.6, y - 1.6, x + 1.6, y + 1.6) for x, y in FID_POS_B]
J1_STRIP = (100.0, 44.0, 165.0, 51.2)  # J1's socket on B.Cu and its pin field on F.Cu
OBST = {"F": HOLES + FIDS + [J1_STRIP], "B": HOLES + FIDS_B + [J1_STRIP, FAN]}
AREA = {"F": (100.6, 51.4, 164.4, 99.4), "B": (100.6, 51.4, 164.4, 99.4)}
GAP = 0.8


def hit(side, r):
    for o in OBST[side]:
        if r[0] < o[2] and r[2] > o[0] and r[1] < o[3] and r[3] > o[1]:
            return o
    return None


cursor = {s: [AREA[s][0], AREA[s][1], 0.0] for s in AREA}


def place(side, fp):
    x0, y0, x1, y1 = AREA[side]
    cx, cy, rh = cursor[side]
    bx0, by0, bx1, by1 = bbox(fp)
    w, h = bx1 - bx0, by1 - by0
    for _ in range(2000):
        if cx + w > x1:
            cx, cy, rh = x0, cy + rh + GAP, 0.0
        if cy + h > y1:
            return False
        o = hit(side, (cx, cy, cx + w, cy + h))
        if not o:
            break
        cx = o[2] + GAP
    else:
        return False
    move_bbox_to(fp, cx, cy)
    cursor[side] = [cx + w + GAP, cy, max(rh, h)]
    OBST[side].append((cx, cy, cx + w, cy + h))
    return True


def orient(fp, outward):
    """Rotate so pin rows run along the edge and the body side farthest from the pads faces `outward`."""
    best = None
    for r in (0, 90, 180, 270):
        fp.SetOrientationDegrees(r)
        bx0, by0, bx1, by1 = bbox(fp)
        pads = [p for p in fp.Pads() if p.GetNumber()]
        xs = [pcbnew.ToMM(p.GetPosition().x) for p in pads]
        ys = [pcbnew.ToMM(p.GetPosition().y) for p in pads]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        along = (max(ys) - min(ys)) if outward in ("-x", "+x") else (max(xs) - min(xs))
        across = (max(xs) - min(xs)) if outward in ("-x", "+x") else (max(ys) - min(ys))
        reach = {"-x": cx - bx0, "+x": bx1 - cx, "-y": cy - by0, "+y": by1 - cy}[outward]
        score = (along + 0.01 >= across, reach)
        if best is None or score > best[0]:
            best = (score, r)
    fp.SetOrientationDegrees(best[1])
    return best[1]


# edge-mounted parts: (value, edge)
EDGE = {
    "Vehicle in": "+y",
    "HAT out": "+y",
    "Green": "+y",
    "Red": "+y",
    "Cap bank": "-x",
    "SWD": "-x",
    "EVQ-P7A01P": "-x",
}
EDGE_ORDER = ["Vehicle in", "HAT out", "Green", "Red", "Cap bank", "SWD", "EVQ-P7A01P"]
BOTTOM_SHEETS = {"Supervisor", "Pi_Interface"}
BOTTOM_GROUPS = {
    ("Input", "Battery sense"),
    ("Holdup", "Cell shunts and the cap-bank connector"),
    ("Holdup", "Bank discharge (button or I2C, parked only)"),
}
TOP_ONLY = ("CP_Elec", "XAL7070", "TO-252", "D_SMB", "HTSSOP-16", "QFN-38", "IC_LM61460", "VSON-8")

edge_parts, top, bottom = [], [], []
for c in comps:
    fp = load_fp(c["fp"])
    fp.SetReference(c["ref"])
    fp.SetValue(c["value"])
    fp.SetPath(pcbnew.KIID_PATH(c["path"]))
    b.Add(fp)
    for k, v in c["fields"].items():  # the symbol's own fields, hidden, so parity sees them
        fp.SetField(k, v)
        fld = fp.GetField(k)
        if fld is not None:
            fld.SetVisible(False)
    if c["dnp"]:
        try:
            fp.SetDNP(True)
        except Exception:
            pass
    for pad in fp.Pads():
        net = pad_net.get((c["ref"], pad.GetNumber()))
        if net:
            pad.SetNet(nets[net])
    if c["ref"] == "J1":
        to_back(fp)
        fp.SetOrientationDegrees(-90)
        fp.SetPosition(V(108.37, 48.77))
        want = {
            "1": (108.37, 48.77),
            "2": (108.37, 46.23),
            "39": (156.63, 48.77),
            "40": (156.63, 46.23),
        }
        for p in fp.Pads():
            if p.GetNumber() in want:
                got = (
                    round(pcbnew.ToMM(p.GetPosition().x), 2),
                    round(pcbnew.ToMM(p.GetPosition().y), 2),
                )
                assert got == want[p.GetNumber()], (
                    f"J1 pad {p.GetNumber()} at {got}, HAT has {want[p.GetNumber()]}"
                )
        assert fp.IsFlipped()
        print("J1 matches the HAT's J1 (pads 1/2/39/40, B.Cu)")
        continue
    if c["value"] in EDGE:
        edge_parts.append((fp, EDGE[c["value"]]))
        continue
    grp = group_of.get(c["ref"], (c["sheet"], ""))
    on_bottom = (grp[0] in BOTTOM_SHEETS or grp in BOTTOM_GROUPS) and not any(
        t in c["fp"] for t in TOP_ONLY
    )
    (bottom if on_bottom else top).append(fp)

# ---------------------------------------------------------------- edge parts
edge_parts.sort(key=lambda it: EDGE_ORDER.index(it[0].GetValue()))
bx = 107.4  # bottom edge runs between the MH3 and MH4 keep-outs
ly = 51.4  # left edge runs between the MH1 and MH3 keep-outs
for fp, side in edge_parts:
    r = orient(fp, side)
    x0, y0, x1, y1 = bbox(fp)
    w, h = x1 - x0, y1 - y0
    if side == "+y":
        move_bbox_to(fp, bx, Y1 - h)
        bx += w + 1.2
        assert bx <= 158.0, f"bottom edge overfull at {fp.GetReference()}"
    else:
        move_bbox_to(fp, X0, ly)
        ly += h + 1.2
        assert ly <= 93.0, f"left edge overfull at {fp.GetReference()}"
    fb = bbox(fp)
    rect = (fb[0] - 0.3, fb[1] - 0.3, fb[2] + 0.3, fb[3] + 0.3)
    OBST["F"].append(rect)
    if any(p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH for p in fp.Pads()):
        OBST["B"].append(rect)  # through-hole pins come out the bottom
    print(f"edge {fp.GetReference():4} {fp.GetValue():12} rot {r:3}  facing {side}")

# ---------------------------------------------------------------- pack
overflow = []
for side, items in (("F", top), ("B", bottom)):
    if side == "B":
        for fp in items:
            to_back(fp)
    items.sort(key=lambda f: -(bbox(f)[3] - bbox(f)[1]))
    for fp in items:
        if place(side, fp):
            continue
        name = str(fp.GetFPID().GetLibItemName())
        other = "B" if side == "F" else "F"
        if not any(t in name for t in TOP_ONLY) and not fp.GetReference().startswith("J"):
            to_back(fp)  # flip to the other side and try there
            if place(other, fp):
                overflow.append(f"{fp.GetReference()}->{other}")
                continue
        move_bbox_to(fp, 172.0, 50.0 + 4 * len(overflow))
        overflow.append(f"{fp.GetReference()} PARKED")


def add_lib_fp(lib, name, ref, x, y, back=False):
    fp = pcbnew.FootprintLoad(os.path.join(sysfp, lib + ".pretty"), name)
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    fp.SetReference(ref)
    fp.SetPosition(V(x, y))
    fp.SetBoardOnly(True)  # mounting holes and fiducials have no schematic symbol
    b.Add(fp)  # flip only once the board owns it: KiCad needs the board to mirror the layers
    if back:
        to_back(fp)


for i, (x, y) in enumerate(MH_POS, 1):
    add_lib_fp("MountingHole", "MountingHole_2.7mm_M2.5", f"MH{i}", x, y)
for i, (x, y) in enumerate(FID_POS, 1):
    add_lib_fp("Fiducial", "Fiducial_1mm_Mask2mm", f"FID{i}", x, y)
for i, (x, y) in enumerate(FID_POS_B, 4):
    add_lib_fp("Fiducial", "Fiducial_1mm_Mask2mm", f"FID{i}", x, y, back=True)

# ---------------------------------------------------------------- planes
BOARD = [(X0, Y0), (X1, Y0), (X1, Y1), (X0, Y1)]


def zone(layer, net, pts, prio=0):
    z = pcbnew.ZONE(b)
    z.SetLayer(layer)
    o = z.Outline()
    o.NewOutline()
    for px, py in pts:
        o.Append(MM(px), MM(py))
    z.SetNet(nets[net])
    z.SetLocalClearance(MM(0.3))
    z.SetMinThickness(MM(0.2))
    z.SetAssignedPriority(prio)
    b.Add(z)


for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu):
    zone(layer, "GND", BOARD)
zone(pcbnew.In2_Cu, "+12V_BUS", [(101, 52), (164, 52), (164, 76), (101, 76)], prio=1)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())

FINE = (
    "VSON-8_3.3x3.3mm_P0.65mm_NexFET",
    "TSSOP-10_3x3mm_P0.5mm",
    "QFN-38_UHF_LIT",
    "IC_LM61460AFSQRJRRQ1",
    "SOIC8_DGK_TEX",
    "SOT-23-6",
)
fine = sorted(
    f.GetReference() for f in b.GetFootprints() if str(f.GetFPID().GetLibItemName()) in FINE
)
rules = ["(version 1)"]
for ref in fine:
    rules.append(
        f"(rule \"{ref} land pattern\"\n\t(constraint clearance (min 0.13mm))\n\t(condition \"A.insideCourtyard('{ref}') && B.insideCourtyard('{ref}')\"))"
    )
open(os.path.join(projdir, "canary-pwr.kicad_dru"), "w").write("\n".join(rules) + "\n")
pcbnew.SaveBoard(
    out_path, b, True
)  # True = skip settings: else it overwrites the .kicad_pro with defaults
nf = sum(
    1
    for f in b.GetFootprints()
    if not f.IsFlipped() and not f.GetReference().startswith(("MH", "FID"))
)
nb = sum(
    1 for f in b.GetFootprints() if f.IsFlipped() and not f.GetReference().startswith(("MH", "FID"))
)
print(f"top {nf} parts, bottom {nb} parts | moved or parked: {overflow or 'none'}")
