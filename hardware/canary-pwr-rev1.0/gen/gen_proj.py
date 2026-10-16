# ruff: noqa: E501
"""Write canary-pwr.kicad_pro, the library tables, and the schematic. Usage: gen_proj.py <projdir> <hat_pro>"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design  # noqa: E402
import gen_sch  # noqa: E402

projdir, hat_pro = sys.argv[1], sys.argv[2]
root_uuid, sheet_uuids = gen_sch.main(projdir)

pro = json.load(open(hat_pro))
pro["meta"] = {"filename": "canary-pwr.kicad_pro", "version": 3}
pro["sheets"] = [[root_uuid, "canary-pwr"]] + [[u, s] for s, u in sheet_uuids.items()]
# KiCad 10 roots the hierarchy at top_level_sheets. Copied from the HAT it kept the HAT's root UUID,
# so KiCad re-keyed every instance path on each open and offered that as a fix (2026-10-15).
pro["schematic"]["top_level_sheets"] = [
    {"filename": "canary-pwr.kicad_sch", "name": "canary-pwr", "uuid": root_uuid}
]
pro["schematic"]["used_designators"] = ""  # the HAT's list otherwise
pro["text_variables"] = {}
base = next(c for c in pro["net_settings"]["classes"] if c["name"] == "Default")


def nc(name, track, clearance=0.2, via_d=0.6, via_drill=0.3):
    c = copy.deepcopy(base)
    c.update(
        name=name,
        track_width=track,
        clearance=clearance,
        via_diameter=via_d,
        via_drill=via_drill,
        priority=1,
    )
    return c


pro["net_settings"]["classes"] = [
    base,
    nc("Power_High", 1.5, 0.25, 0.8, 0.4),
    nc("Power", 0.5),
    nc("Switch_Node", 1.5, 0.25),
    nc("CAN", 0.3),
    nc("GND", 0.5),
]
# Design net names, written as the exact names KiCad gives them. Bare patterns once missed every
# single-sheet net (KiCad anchors patterns, so "VCAP" never matched "/Holdup/VCAP"): VCAP,
# +12V_RAW and +5V_SHUNT sat at 0.2 mm with DRC passing (2026-10-15).
CLASSES = {
    "Power_High": (
        "+12V_RAW",
        "+12V_PROT",
        "+12V_BUS",
        "VCAP",
        "+5V_BUCK",
        "+5V_SHUNT",
        "+5V_PI",
        "LTC_VOUTSP",
        "LTC_ICAP",
    ),
    "Power": (
        "+12V_HAT",
        "+3V3_AON",
        "AON_IN",
        "PI_3V3",
        "LTC_INTVCC",
        "BANK_POS_S",
        "BANK_NEG_S",
        "BANK_TAP1",
        "BANK_TAP2",
        "BANK_TAP3",
        "LTC_CAP1",
        "LTC_CAP2",
        "LTC_CAP3",
        "LTC_CAP4",
        "LTC_CAPFB",
        "LTC_CAPRTN",
    ),
    "Switch_Node": ("LTC_SW", "LM_SW"),
    "CAN": ("CANH", "CANL"),
    "GND": ("GND",),
}
names = gen_sch.kicad_net_names(design.PARTS)
classed = [n for ns in CLASSES.values() for n in ns]
unknown = sorted(set(classed) - set(names))
if unknown:
    raise SystemExit(f"net classes name nets the design does not have: {unknown}")
rails = sorted(n for n in names if n.startswith("+") and n not in classed)
if rails:
    raise SystemExit(f"supply rails without a net class: {rails}")
pro["net_settings"]["netclass_patterns"] = [
    {"netclass": c, "pattern": names[n]} for c, ns in CLASSES.items() for n in ns
]
pro["net_settings"]["netclass_assignments"] = None
json.dump(pro, open(os.path.join(projdir, "canary-pwr.kicad_pro"), "w"), indent=2)

open(os.path.join(projdir, "sym-lib-table"), "w").write(
    "(sym_lib_table\n\t(version 7)\n"
    '\t(lib (name "canary-pwr") (type "KiCad") (uri "${KIPRJMOD}/lib/symbols/canary-pwr.kicad_sym") (options "") (descr "CANary-PWR project symbols"))\n'
    '\t(lib (name "LTC3350EUHF-PBF") (type "KiCad") (uri "${KIPRJMOD}/lib/symbols/LTC3350EUHF-PBF.kicad_sym") (options "") (descr "Ultra Librarian, pin map checked against the datasheet"))\n'
    '\t(lib (name "LM61460AFSQRJRRQ1") (type "KiCad") (uri "${KIPRJMOD}/lib/symbols/LM61460AFSQRJRRQ1.kicad_sym") (options "") (descr "Ultra Librarian, pin map checked against the datasheet"))\n'
    ")\n"
)
open(os.path.join(projdir, "fp-lib-table"), "w").write(
    "(fp_lib_table\n\t(version 7)\n"
    '\t(lib (name "canary-pwr") (type "KiCad") (uri "${KIPRJMOD}/lib/footprints.pretty") (options "") (descr "CANary-PWR footprints (vendor, checked against datasheets)"))\n'
    ")\n"
)
print("project files written")
