# ruff: noqa: E501
"""Compare KiCad's exported netlist with design.PARTS pin-by-pin, and on every field the BOM is built from. Usage: verify_netlist.py <netlist.xml> <projdir>"""

import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import design  # noqa: E402
import gen_sch  # noqa: E402

libs = gen_sch.Libs(sys.argv[2])
gen_sch.assign_refs(libs, design.PARTS)

root = ET.parse(sys.argv[1]).getroot()
got = {}  # (ref, pin) -> net
for net in root.iter("net"):
    for n in net.iter("node"):
        got[(n.get("ref"), n.get("pin"))] = net.get("name")
comps = {c.get("ref"): c for c in root.iter("comp")}


def norm(name, sheet):
    return name.split("/")[-1] if name else name


# symbol field -> design.PARTS key. PCBWay's KiCad plugin builds its BOM from these fields.
BOM_FIELDS = {
    "Description": "desc",
    "MPN": "mpn",
    "Manufacturer": "mfr",
    "Package": "pkg",
    "Note": "note",
    "Datasheet": "ds",
}

bad = 0
groups = defaultdict(set)  # intended net -> set of actual net names
for p in design.PARTS:
    if p["ref"] not in comps:
        print("MISSING COMPONENT", p["ref"])
        bad += 1
        continue
    c = comps[p["ref"]]
    if c.find("value").text != p["value"]:
        print("VALUE", p["ref"], c.find("value").text, "!=", p["value"])
        bad += 1
    fp = c.find("footprint").text if c.find("footprint") is not None else ""
    if fp != p["fp"]:
        print("FOOTPRINT", p["ref"], fp, "!=", p["fp"])
        bad += 1
    fields = {f.get("name"): f.text or "" for f in c.find("fields")}
    for name, key in BOM_FIELDS.items():
        if fields.get(name, "") != p[key]:
            print("FIELD", p["ref"], name, repr(fields.get(name, "")), "!=", repr(p[key]))
            bad += 1
    dnp = any(prop.get("name") == "dnp" for prop in c.findall("property"))
    if dnp != p["dnp"]:
        print("DNP", p["ref"], dnp, "!=", p["dnp"])
        bad += 1
    for pin, want in p["pins"].items():
        have = got.get((p["ref"], pin))
        if want is None:
            if have and not have.startswith("unconnected-"):
                print("NC pin connected", p["ref"], pin, have)
                bad += 1
            continue
        if have is None:
            print("PIN NOT IN NETLIST", p["ref"], pin, want)
            bad += 1
            continue
        groups[want].add(have)
        if norm(have, p["sheet"]) != want:
            print("NET NAME", p["ref"], pin, have, "!=", want)
            bad += 1
for want, haves in groups.items():
    if len(haves) != 1:
        print("SPLIT NET", want, haves)
        bad += 1
inv = defaultdict(set)
for want, haves in groups.items():
    for h in haves:
        inv[h].add(want)
for h, ws in inv.items():
    if len(ws) > 1:
        print("MERGED NETS", h, ws)
        bad += 1
extra = set(comps) - {p["ref"] for p in design.PARTS}
print(
    f"components in netlist: {len(comps)}  design parts: {len(design.PARTS)}  extra: {sorted(extra)}"
)
print(f"intended nets: {len(groups)}  problems: {bad}")
