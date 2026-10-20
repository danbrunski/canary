#!/bin/bash
# Regenerate the cap bank from gen/: project + schematic, cell footprint, netlist, board, ERC, BOM, then DRC.
#   gen/build.sh            (from anywhere; KiCad 10 from hardware/tools/kicad_env.sh)
# Overwrites canary-capbank.kicad_{pro,sch,pcb}: run it only while the board has no hand edits.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
proj=$(dirname "$here")
root=$(cd "$proj/../.." && pwd)
# shellcheck source=hardware/tools/kicad_env.sh
source "$root/hardware/tools/kicad_env.sh"
py=$root/.venv/bin/python
[[ -x $py ]] || py=python3
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

$py "$here/footprints.py" "$proj/lib/footprints.pretty"
$py "$here/gen_proj.py" "$proj" "$proj/../canary-pwr-rev1.0/canary-pwr.kicad_pro"
kicad_cli sch export netlist --format kicadxml -o "$(kpath "$tmp/net.xml")" "$(kpath "$proj/canary-capbank.kicad_sch")" >/dev/null
kicad_py "$(kpath "$here/gen_pcb.py")" "$(kpath "$tmp/net.xml")" "$(kpath "$proj/canary-capbank.kicad_pcb")" "$(kpath "$proj")" \
    "$(kpath "$kicad_share/footprints")" 2>&1 | kicad_quiet
kicad_cli sch erc --severity-all --exit-code-violations -o "$(kpath "$tmp/erc.rpt")" "$(kpath "$proj/canary-capbank.kicad_sch")" >/dev/null \
    && echo "ERC clean" || { grep -E "^\[|;" "$tmp/erc.rpt"; exit 1; }
kicad_cli sch export bom --fields "Reference,QUANTITY,Value,Manufacturer,MPN,Footprint,Description" \
    --labels "Refs,Qty,Value,Manufacturer,MPN,Footprint,Description" --group-by "Value,MPN,Footprint" \
    -o "$(kpath "$proj/BOM.csv")" "$(kpath "$proj/canary-capbank.kicad_sch")" >/dev/null && echo "BOM.csv written"
$py "$here/offboard.py" "$proj/BOM-offboard.csv"
kicad_drc "$tmp/drc.json" "$proj/canary-capbank.kicad_pcb" --schematic-parity --severity-all
$py - "$tmp/drc.json" <<'PY'
import collections, json, sys
r = json.load(open(sys.argv[1]))
c = collections.Counter(f"{v['severity']}:{v['type']}" for v in r["violations"])
print("DRC:", dict(sorted(c.items())) or "clean", "| unconnected", len(r["unconnected_items"]), "| parity", len(r.get("schematic_parity", [])))
for v in r["violations"] + r["unconnected_items"] + r.get("schematic_parity", []):
    print("  ", v["severity"], v["type"], " || ".join(f"{i['description'][:70]} @({i['pos']['x']:.2f},{i['pos']['y']:.2f})" for i in v["items"]))
PY
