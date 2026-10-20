# ruff: noqa: E501
"""Write canary-capbank.kicad_pro, the footprint library table and the schematic.

The project's settings start from CANary-PWR's .kicad_pro (rule severities, DRC minimums), with this
board's sheet and net classes. Usage: gen_proj.py <projdir> <canary-pwr.kicad_pro>
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bank  # noqa: E402
import gen_sch  # noqa: E402

projdir, pwr_pro = sys.argv[1], sys.argv[2]
root_uuid = gen_sch.main(projdir)

pro = json.load(open(pwr_pro))
pro["meta"] = {"filename": "canary-capbank.kicad_pro", "version": 3}
pro["sheets"] = [[root_uuid, "canary-capbank"]]
pro["schematic"]["top_level_sheets"] = [
    {"filename": "canary-capbank.kicad_sch", "name": "canary-capbank", "uuid": root_uuid}
]
pro["schematic"]["used_designators"] = ""
pro["text_variables"] = {}
base = next(c for c in pro["net_settings"]["classes"] if c["name"] == "Default")
base.update(clearance=0.2, track_width=0.3, via_diameter=0.6, via_drill=0.3)
high = copy.deepcopy(base)
# Every stack node carries the string's current between two cells (4 A, 7.25 A peak), so those joins are
# pours; the class width is for the short taps to J1, which carry only the shunt current (<= 0.5 A).
high.update(
    name="Power_High", clearance=0.25, track_width=0.5, via_diameter=0.8, via_drill=0.4, priority=1
)
pro["net_settings"]["classes"] = [base, high]
STACK = ("VCAP", "GND", "BANK_TAP1", "BANK_TAP2", "BANK_TAP3")
nets = {n for p in bank.PARTS for n in p["pins"].values()}
missing = sorted(set(STACK) - nets)
if missing:
    raise SystemExit(f"net classes name nets the design does not have: {missing}")
# Global labels and the GND power symbol keep bare names, so the patterns are the names themselves.
pro["net_settings"]["netclass_patterns"] = [{"netclass": "Power_High", "pattern": n} for n in STACK]
pro["net_settings"]["netclass_assignments"] = None
json.dump(pro, open(os.path.join(projdir, "canary-capbank.kicad_pro"), "w"), indent=2)

open(os.path.join(projdir, "fp-lib-table"), "w").write(
    "(fp_lib_table\n\t(version 7)\n"
    '\t(lib (name "canary-capbank") (type "KiCad") (uri "${KIPRJMOD}/lib/footprints.pretty") (options "") (descr "Cap bank footprints: the cells laid flat"))\n'
    ")\n"
)
print("project files written; root sheet", root_uuid)
