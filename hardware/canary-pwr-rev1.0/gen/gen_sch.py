# ruff: noqa: E501, N802, N806, E741
"""Emit the CANary-PWR KiCad 10 project: root + sub-sheets, label-connected.

Usage: gen_sch.py <project_dir>
"""

import copy
import math
import os
import sys
import uuid
from collections import defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import design  # noqa: E402
from sexp import Sym, dumps, find, findall, parse  # noqa: E402

NATIVE_SYSLIB = "/usr/share/kicad/symbols"
SYSLIB = (
    r"C:\Program Files\KiCad\10.0\share\kicad\symbols"
    if os.name == "nt"
    else NATIVE_SYSLIB
    if os.path.isdir(NATIVE_SYSLIB)
    else "/mnt/c/Program Files/KiCad/10.0/share/kicad/symbols"
)
PROJECT = "canary-pwr"
TODAY = date.today().isoformat()
GRID = 1.27
CHAR_W = 1.05  # mm per character at 1.27 mm text, conservative


def U():
    return str(uuid.uuid4())


def snap(v, g=GRID):
    return round(round(v / g) * g, 4)


def fmt(v):
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ----------------------------------------------------------------- library access
class Libs:
    def __init__(self, projdir):
        self.paths = {}
        for fn in os.listdir(SYSLIB):
            if fn.endswith(".kicad_sym"):
                self.paths[fn[:-10]] = os.path.join(SYSLIB, fn)
        for fn in os.listdir(os.path.join(projdir, "lib", "symbols")):
            if fn.endswith(".kicad_sym"):
                self.paths[fn[:-10]] = os.path.join(projdir, "lib", "symbols", fn)
        self.cache = {}

    def lib(self, name):
        if name not in self.cache:
            tree = parse(open(self.paths[name], encoding="utf-8").read())[0]
            self.cache[name] = {s[1]: s for s in findall(tree, "symbol")}
        return self.cache[name]

    def flat(self, lib_id):
        lib, name = lib_id.split(":", 1)
        syms = self.lib(lib)
        s = copy.deepcopy(syms[name])
        ext = find(s, "extends")
        if ext:
            parent = self.flat(f"{lib}:{ext[1]}")
            pname = parent[1].split(":", 1)[1]
            out = copy.deepcopy(parent)
            out[1] = name
            for u in findall(out, "symbol"):
                u[1] = name + u[1][len(pname) :]
            child_props = {p[1]: p for p in findall(s, "property")}
            new = []
            seen = set()
            for x in out:
                if isinstance(x, list) and x and x[0] == "property" and x[1] in child_props:
                    new.append(child_props[x[1]])
                    seen.add(x[1])
                else:
                    new.append(x)
            for k, p in child_props.items():
                if k not in seen:
                    idx = max(
                        i
                        for i, x in enumerate(new)
                        if isinstance(x, list) and x and x[0] == "property"
                    )
                    new.insert(idx + 1, p)
            out = new
            s = out
        s = [x for x in s if not (isinstance(x, list) and x and x[0] == "extends")]
        s[1] = f"{lib}:{name}" if ":" not in s[1] else s[1]
        return s

    def pins(self, lib_id):
        s = self.flat(lib_id)
        pins = []
        for u in findall(s, "symbol"):
            parts = u[1].rsplit("_", 2)
            unit, style = int(parts[-2]), int(parts[-1])
            if style not in (0, 1):
                continue
            for p in findall(u, "pin"):
                at = find(p, "at")
                hide = any(
                    isinstance(x, list) and x and x[0] == "hide" and x[1] == "yes" for x in p
                ) or "hide" in [x for x in p if isinstance(x, Sym)]
                pins.append(
                    dict(
                        num=find(p, "number")[1],
                        name=find(p, "name")[1],
                        type=str(p[1]),
                        x=float(at[1]),
                        y=float(at[2]),
                        a=float(at[3]) if len(at) > 3 else 0.0,
                        unit=unit,
                        hidden=hide,
                    )
                )
        return pins

    def bbox(self, lib_id):
        """Graphic body extent in symbol coords (y up)."""
        s = self.flat(lib_id)
        xs, ys = [], []
        for u in findall(s, "symbol"):
            for g in u:
                if not isinstance(g, list):
                    continue
                if g[0] == "rectangle":
                    for k in ("start", "end"):
                        e = find(g, k)
                        xs.append(float(e[1]))
                        ys.append(float(e[2]))
                elif g[0] in ("polyline", "bezier"):
                    for xy in findall(find(g, "pts"), "xy"):
                        xs.append(float(xy[1]))
                        ys.append(float(xy[2]))
                elif g[0] == "circle":
                    c = find(g, "center")
                    r = float(find(g, "radius")[1])
                    xs += [float(c[1]) - r, float(c[1]) + r]
                    ys += [float(c[2]) - r, float(c[2]) + r]
                elif g[0] == "pin":
                    at = find(g, "at")
                    xs.append(float(at[1]))
                    ys.append(float(at[2]))
        if not xs:
            return (-1, -1, 1, 1)
        return (min(xs), min(ys), max(xs), max(ys))


def rot(px, py, r):
    t = math.radians(r)
    return px * math.cos(t) - py * math.sin(t), px * math.sin(t) + py * math.cos(t)


# ----------------------------------------------------------------- s-expression snippets (KiCad 10 style)
def eff(ind, justify=None, hide=False):
    t = "\t" * ind
    j = f"\n{t}\t(justify {justify})" if justify else ""
    return f"{t}(effects\n{t}\t(font\n{t}\t\t(size 1.27 1.27)\n{t}\t){j}\n{t})"


def prop(name, value, x, y, ang=0, hide=False, ind=2, justify=None):
    t = "\t" * ind
    h = f"\n{t}\t(hide yes)" if hide else ""
    v = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return (
        f'{t}(property "{name}" "{v}"\n{t}\t(at {fmt(x)} {fmt(y)} {fmt(ang)})\n{t}\t(show_name no)\n'
        f"{t}\t(do_not_autoplace no){h}\n{eff(ind + 1, justify)}\n{t})"
    )


def lib_symbol_text(sym):
    """Embedded lib_symbols entry, re-indented to sit at depth 2."""
    txt = dumps(sym, 2)
    return "\t\t" + txt


def label_text(kind, name, x, y, ang):
    j = {0: "left", 90: "left", 180: "right", 270: "right"}[int(ang) % 360]
    if kind == "global":
        return (
            f'\t(global_label "{name}"\n\t\t(shape passive)\n\t\t(at {fmt(x)} {fmt(y)} {fmt(ang)})\n'
            f'\t\t(fields_autoplaced yes)\n{eff(2, j)}\n\t\t(uuid "{U()}")\n'
            + prop("Intersheetrefs", "${INTERSHEET_REFS}", x, y, 0, hide=True, ind=2, justify=j)
            + "\n\t)"
        )
    return (
        f'\t(label "{name}"\n\t\t(at {fmt(x)} {fmt(y)} {fmt(ang)})\n'
        f'\t\t(fields_autoplaced yes)\n{eff(2, j + " bottom")}\n\t\t(uuid "{U()}")\n\t)'
    )


def nc_text(x, y):
    return f'\t(no_connect\n\t\t(at {fmt(x)} {fmt(y)})\n\t\t(uuid "{U()}")\n\t)'


def text_note(s, x, y, size=1.27):
    v = s.replace('"', '\\"').replace("\n", "\\n")
    return (
        f'\t(text "{v}"\n\t\t(exclude_from_sim no)\n\t\t(at {fmt(x)} {fmt(y)} 0)\n'
        f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {size} {size})\n\t\t\t)\n\t\t\t(justify left bottom)\n\t\t)\n"
        f'\t\t(uuid "{U()}")\n\t)'
    )


def symbol_instance(
    lib_id,
    x,
    y,
    r,
    ref,
    value,
    fields,
    pins,
    path,
    unit=1,
    dnp=False,
    ref_pos=None,
    val_pos=None,
    in_bom=True,
    on_board=True,
):
    out = [
        f'\t(symbol\n\t\t(lib_id "{lib_id}")\n\t\t(at {fmt(x)} {fmt(y)} {fmt(r)})\n\t\t(unit {unit})\n'
        f"\t\t(body_style 1)\n\t\t(exclude_from_sim no)\n\t\t(in_bom {'yes' if in_bom else 'no'})\n"
        f"\t\t(on_board {'yes' if on_board else 'no'})\n\t\t(in_pos_files yes)\n\t\t(dnp {'yes' if dnp else 'no'})\n"
        f'\t\t(uuid "{U()}")'
    ]
    rp = ref_pos or (x, y - 3)
    vp = val_pos or (x, y + 3)
    hide_ref = ref.startswith("#")
    out.append(prop("Reference", ref, rp[0], rp[1], 0, hide=hide_ref))
    out.append(prop("Value", value, vp[0], vp[1], 0, hide=ref.startswith("#FLG")))
    for k, v in fields:
        out.append(prop(k, v, x, y, 0, hide=True))
    for pn in pins:
        out.append(f'\t\t(pin "{pn}"\n\t\t\t(uuid "{U()}")\n\t\t)')
    out.append(
        f'\t\t(instances\n\t\t\t(project "{PROJECT}"\n\t\t\t\t(path "{path}"\n'
        f'\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit {unit})\n\t\t\t\t)\n\t\t\t)\n\t\t)'
    )
    out.append("\t)")
    return "\n".join(out)


def sheet_file(title_uuid, paper, title, comments, lib_syms, body):
    cm = "".join(f'\t\t(comment {i} "{c}")\n' for i, c in enumerate(comments, 1))
    return (
        f'(kicad_sch\n\t(version 20260306)\n\t(generator "eeschema")\n\t(generator_version "10.0")\n'
        f'\t(uuid "{title_uuid}")\n\t(paper "{paper}")\n\t(title_block\n\t\t(title "{title}")\n'
        f'\t\t(date "{TODAY}")\n\t\t(rev "1.0")\n\t\t(company "CANary")\n{cm}\t)\n'
        f"\t(lib_symbols\n"
        + "\n".join(lib_syms)
        + "\n\t)\n"
        + "\n".join(body)
        + "\n\t(embedded_fonts no)\n)\n"
    )


# ----------------------------------------------------------------- layout
PAPER = {"A3": (420.0, 297.0), "A2": (594.0, 420.0), "A1": (841.0, 594.0)}


def part_geometry(libs, p):
    """Pin endpoints relative to the symbol origin (screen coords, y down) plus label boxes."""
    pins = [q for q in libs.pins(p["lib_id"]) if q["unit"] in (0, 1)]
    r = p["rot"]
    geo = []
    for q in pins:
        rx, ry = rot(q["x"], q["y"], r)
        out_dir = (q["a"] + r + 180) % 360
        geo.append(dict(q, sx=round(rx, 4), sy=round(-ry, 4), out=out_dir))
    return geo


def label_len(name, kind):
    n = len(name.replace("~{", "").replace("}", ""))
    return n * CHAR_W + (3.5 if kind == "global" else 1.0)


def compute_box(libs, p, geo, label_kind):
    bx0, by0, bx1, by1 = libs.bbox(p["lib_id"])
    corners = [
        rot(bx0, by0, p["rot"]),
        rot(bx1, by1, p["rot"]),
        rot(bx0, by1, p["rot"]),
        rot(bx1, by0, p["rot"]),
    ]
    xs = [c[0] for c in corners]
    ys = [-c[1] for c in corners]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    for g in geo:
        net = p["pins"].get(g["num"])
        L = 2.0 if net is None or net == "GND" else label_len(net, label_kind(net))
        if net == "GND":
            L = 4.0
        dx, dy = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[int(round(g["out"])) % 360]
        ex, ey = g["sx"] + dx * L, g["sy"] + dy * L
        x0, x1 = min(x0, g["sx"], ex), max(x1, g["sx"], ex)
        y0, y1 = min(y0, g["sy"], ey - 1.5), max(y1, g["sy"], ey + 1.5)
    # reference/value text lines
    y0 -= 3.2
    y1 += 3.2
    width_txt = max(len(p.get("ref", "U99")), len(p["value"])) * CHAR_W
    cx = (x0 + x1) / 2
    x0, x1 = min(x0, cx - width_txt / 2), max(x1, cx + width_txt / 2)
    return x0, y0, x1, y1


def pack(items, max_w, gap_x=5.08, gap_y=5.08):
    """items: list of (w, h, payload). Returns list of (x, y, payload) and total (w, h)."""
    placed, x, y, row_h, tot_w = [], 0.0, 0.0, 0.0, 0.0
    for w, h, pl in items:
        if x > 0 and x + w > max_w:
            x = 0.0
            y += row_h + gap_y
            row_h = 0.0
        placed.append((x, y, pl))
        x += w + gap_x
        row_h = max(row_h, h)
        tot_w = max(tot_w, x - gap_x)
    return placed, tot_w, y + row_h


def assign_refs(libs, parts):
    """Prefix from the library Reference field, numbered in sheet order; fixed refs are honoured and skipped."""
    fixed = {p["fixed_ref"] for p in parts if p.get("fixed_ref")} | design.RETIRED_REFS
    counters = defaultdict(int)
    for sheet in design.SHEETS:
        for p in parts:
            if p["sheet"] != sheet:
                continue
            if p.get("fixed_ref"):
                p["ref"] = p["fixed_ref"]
                continue
            sym = libs.flat(p["lib_id"])
            pre = p["ref_prefix"] or next(
                x[2] for x in findall(sym, "property") if x[1] == "Reference"
            )
            while True:
                counters[pre] += 1
                if f"{pre}{counters[pre]}" not in fixed:
                    break
            p["ref"] = f"{pre}{counters[pre]}"


def kicad_net_names(parts):
    """Design net -> the name KiCad gives it. A net on more than one sheet gets global labels and
    keeps its bare name (+12V_BUS); a net on one sheet gets local labels, which KiCad prefixes with
    the sheet path (/Holdup/VCAP). Net-class patterns must use these names."""
    sheets = defaultdict(set)
    for p in parts:
        for net in p["pins"].values():
            if net:
                sheets[net].add(p["sheet"])
    return {n: n if len(s) > 1 else f"/{next(iter(s))}/{n}" for n, s in sheets.items()}


def main(projdir):
    libs = Libs(projdir)
    parts = design.PARTS

    assign_refs(libs, parts)

    # net bookkeeping
    net_sheets = defaultdict(set)
    net_types = defaultdict(list)
    for p in parts:
        tp = {q["num"]: q["type"] for q in libs.pins(p["lib_id"])}
        for num, net in p["pins"].items():
            if net:
                net_sheets[net].add(p["sheet"])
                net_types[net].append(tp.get(num, "passive"))
    # sanity: every symbol pin mapped
    for p in parts:
        nums = {q["num"] for q in libs.pins(p["lib_id"]) if q["unit"] in (0, 1)}
        missing = nums - set(p["pins"])
        extra = set(p["pins"]) - nums
        if missing or extra:
            raise SystemExit(
                f"{p['ref']} {p['lib_id']}: unmapped pins {sorted(missing)} unknown {sorted(extra)}"
            )

    net_names = kicad_net_names(parts)

    def label_kind(net):
        return "local" if net_names[net].startswith("/") else "global"

    flag_nets = sorted(
        n for n, ts in net_types.items() if "power_in" in ts and "power_out" not in ts
    )
    flag_sheet = {n: sorted(net_sheets[n], key=design.SHEETS.index)[0] for n in flag_nets}

    root_uuid = U()
    sheet_uuids = {s: U() for s in design.SHEETS}
    sheet_file_uuids = {s: U() for s in design.SHEETS}
    pwr_n = [0]

    for sheet in design.SHEETS:
        path = f"/{root_uuid}/{sheet_uuids[sheet]}"
        sp = [p for p in parts if p["sheet"] == sheet]
        groups = []
        for p in sp:
            if not groups or groups[-1][0] != p["group"]:
                groups.append((p["group"], []))
            groups[-1][1].append(p)

        # per-part boxes, packed per group
        group_blocks = []
        for gname, gp in groups:
            items = []
            for p in gp:
                geo = part_geometry(libs, p)
                bx = compute_box(libs, p, geo, label_kind)
                items.append((bx[2] - bx[0], bx[3] - bx[1], (p, geo, bx)))
            ics = [it for it in items if len(it[2][1]) > 3]
            small = [it for it in items if len(it[2][1]) <= 3]
            ic_w = sum(it[0] for it in ics) + 5.08 * max(len(ics) - 1, 0)
            max_w = max(150.0, ic_w)
            placed, w, h = pack(ics + small, max_w)
            group_blocks.append((w, h + 8.0, (gname, placed)))
        paper = "A3"
        for cand in ("A3", "A2", "A1"):
            pw, ph = PAPER[cand]
            pl, tw, th = pack(group_blocks, pw - 40, gap_x=12.7, gap_y=10.16)
            if th <= ph - 70 and tw <= pw - 40:
                paper = cand
                break
        pw, ph = PAPER[paper]
        gplaced, _, _ = pack(group_blocks, pw - 40, gap_x=12.7, gap_y=10.16)

        body, used_lib = [], {}
        ox, oy = 20.32, 30.48
        body.append(
            text_note(f"{sheet.replace('_', ' ')}: {design.SHEET_TITLES[sheet]}", ox, 22.86, 2.54)
        )
        for gx, gy, (gname, placed) in gplaced:
            gx0, gy0 = ox + gx, oy + gy
            body.append(text_note(gname, gx0, gy0 + 2.54, 1.778))
            for px, py, (p, geo, bx) in placed:
                X = snap(gx0 + px - bx[0], 2.54)
                Y = snap(gy0 + 6.0 + py - bx[1], 2.54)
                used_lib[p["lib_id"]] = True
                fields = [
                    ("Footprint", p["fp"]),
                    ("Datasheet", p["ds"]),
                    ("Description", p["desc"]),
                ]
                if p["mpn"]:
                    fields.append(("MPN", p["mpn"]))
                if p["mfr"]:
                    fields.append(("Manufacturer", p["mfr"]))
                if p["note"]:
                    fields.append(("Note", p["note"]))
                ref_pos = (X + (bx[0] + bx[2]) / 2, Y + bx[1] + 1.6)
                val_pos = (X + (bx[0] + bx[2]) / 2, Y + bx[3] - 1.0)
                pin_nums = sorted({g["num"] for g in geo}, key=lambda n: (len(n), n))
                body.append(
                    symbol_instance(
                        p["lib_id"],
                        X,
                        Y,
                        p["rot"],
                        p["ref"],
                        p["value"],
                        fields,
                        pin_nums,
                        path,
                        dnp=p["dnp"],
                        ref_pos=ref_pos,
                        val_pos=val_pos,
                    )
                )
                done = set()
                for g in geo:
                    net = p["pins"].get(g["num"])
                    ax, ay = round(X + g["sx"], 4), round(Y + g["sy"], 4)
                    key = (ax, ay, net)
                    if key in done:
                        continue
                    done.add(key)
                    if net is None:
                        if g["type"] != "no_connect" or not g["hidden"]:
                            body.append(nc_text(ax, ay))
                        continue
                    if net == "GND":
                        pwr_n[0] += 1
                        r = (g["out"] - 270) % 360
                        used_lib["power:GND"] = True
                        body.append(
                            symbol_instance(
                                "power:GND",
                                ax,
                                ay,
                                r,
                                f"#PWR{pwr_n[0]:03d}",
                                "GND",
                                [("Footprint", ""), ("Datasheet", ""), ("Description", "")],
                                ["1"],
                                path,
                                ref_pos=(ax, ay + 5),
                                val_pos=(ax, ay + 3.8),
                            )
                        )
                        continue
                    body.append(label_text(label_kind(net), net, ax, ay, g["out"]))
        # PWR_FLAGs for nets with power inputs and no power output, placed once (first sheet using the net)
        fl = [n for n in flag_nets if flag_sheet[n] == sheet]
        fx, fy = 25.4, snap(ph - 30.48, 2.54)
        if fl:
            body.append(text_note("Power flags (nets fed by passive pins)", fx, fy - 12.7, 1.778))
        for i, n in enumerate(fl):
            x, y = fx + i * 25.4, fy
            used_lib["power:PWR_FLAG"] = True
            pwr_n[0] += 1
            body.append(
                symbol_instance(
                    "power:PWR_FLAG",
                    x,
                    y,
                    0,
                    f"#FLG{pwr_n[0]:03d}",
                    "PWR_FLAG",
                    [("Footprint", ""), ("Datasheet", "~"), ("Description", "")],
                    ["1"],
                    path,
                    ref_pos=(x, y - 5),
                    val_pos=(x, y - 4),
                )
            )
            if n == "GND":
                used_lib["power:GND"] = True
                pwr_n[0] += 1
                body.append(
                    symbol_instance(
                        "power:GND",
                        x,
                        y,
                        0,
                        f"#PWR{pwr_n[0]:03d}",
                        "GND",
                        [("Footprint", ""), ("Datasheet", ""), ("Description", "")],
                        ["1"],
                        path,
                        ref_pos=(x, y + 5),
                        val_pos=(x, y + 3.8),
                    )
                )
            else:
                body.append(label_text(label_kind(n), n, x, y, 270))
        libsyms = [lib_symbol_text(libs.flat(l)) for l in sorted(used_lib)]
        txt = sheet_file(
            sheet_file_uuids[sheet],
            paper,
            "CANary-PWR",
            [
                design.SHEET_TITLES[sheet],
                "Design record: docs/CANary-PWR-Spec.md",
                "First-pass capture, label-connected; generated 2026-10-13",
            ],
            libsyms,
            body,
        )
        open(os.path.join(projdir, f"{sheet}.kicad_sch"), "w").write(txt)
        print(f"{sheet}: {len(sp)} parts, paper {paper}")

    # root sheet
    body = []
    body.append(
        text_note(
            "CANary-PWR rev 1.0 -- supercapacitor UPS and always-on supervisor for the Pi 5 + CANary HAT",
            20.32,
            25.4,
            2.54,
        )
    )
    notes = (
        "Stack: NVMe base / Pi 5 / CANary-PWR / CANary HAT (rev 2.1 and later). PWR plugs directly on the Pi; the HAT plugs on PWR's stacking header.\n"
        "Power path: OBD 12 V -> reverse block + TVS -> charger switch (TPS1HB35) -> LTC3350 input ideal diode -> +12V_BUS (backed up by the cap bank)\n"
        "            +12V_BUS -> LM61460 5.1 V -> INA226 shunt -> LM74700 ideal diode -> Pi header pins 2/4 (Power HAT+ style, no USB-C)\n"
        "            +12V_BUS -> TPS1HB35 -> HAT out terminal -> jumper to the HAT's J2\n"
        "Every switch defaults ON: a dead or resetting supervisor degrades to an always-on Pi, never a dark rig.\n"
        "Supervisor <-> Pi: I2C1 through a Pi-powered TCA4307, GPIO6 = power fail (HIGH = OK), GPIO27 = MCU BOOT0, GPIO24 = MCU reset, Pi 3V3 sense.\n"
        "Nets are connected by labels on this first pass; global labels cross sheets."
    )
    body.append(text_note(notes, 20.32, 45.72, 1.524))
    x = 20.32
    sheet_syms = []
    for page, sheet in enumerate(design.SHEETS, start=2):
        w, h = 45.72, 20.32
        y = 76.2
        sheet_syms.append(
            f"\t(sheet\n\t\t(at {fmt(x)} {fmt(y)})\n\t\t(size {fmt(w)} {fmt(h)})\n\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n"
            f"\t\t(on_board yes)\n\t\t(dnp no)\n\t\t(fields_autoplaced yes)\n\t\t(stroke\n\t\t\t(width 0.1524)\n\t\t\t(type solid)\n\t\t)\n"
            f'\t\t(fill\n\t\t\t(color 0 0 0 0)\n\t\t)\n\t\t(uuid "{sheet_uuids[sheet]}")\n'
            + prop("Sheetname", sheet, x, y - 0.7116, 0, justify="left bottom")
            + "\n"
            + prop("Sheetfile", f"{sheet}.kicad_sch", x, y + h + 0.5846, 0, justify="left top")
            + "\n"
            f'\t\t(instances\n\t\t\t(project "{PROJECT}"\n\t\t\t\t(path "/{root_uuid}"\n\t\t\t\t\t(page "{page}")\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)'
        )
        x += w + 15.24
    body += sheet_syms
    body.append('\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)')
    txt = sheet_file(
        root_uuid,
        "A3",
        "CANary-PWR",
        [
            "Root: sub-sheets connect by global labels and power symbols",
            "Design record: docs/CANary-PWR-Spec.md",
            "First-pass capture generated 2026-10-13",
        ],
        [],
        body,
    )
    open(os.path.join(projdir, f"{PROJECT}.kicad_sch"), "w").write(txt)
    print("flags on:", flag_nets)
    return root_uuid, sheet_uuids


if __name__ == "__main__":
    main(sys.argv[1])
