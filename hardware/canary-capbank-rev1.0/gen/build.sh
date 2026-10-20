#!/bin/bash
# Regenerate the cap bank from gen/: project + schematic, cell footprint, netlist, board, ERC, BOM, then DRC.
#   gen/build.sh            (from anywhere; WSL with the Windows KiCad 10 install)
# Overwrites canary-capbank.kicad_{pro,sch,pcb}: run it only while the board has no hand edits.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
proj=$(dirname "$here")
bin="/mnt/c/Program Files/KiCad/10.0/bin"
w() { wslpath -w "$1"; }
py=/home/thebo/code/canary/.venv/bin/python
[[ -x $py ]] || py=python3
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

$py "$here/footprints.py" "$proj/lib/footprints.pretty"
$py "$here/gen_proj.py" "$proj" "$proj/../canary-pwr-rev1.0/canary-pwr.kicad_pro"
"$bin/kicad-cli.exe" sch export netlist --format kicadxml -o "$(w "$tmp/net.xml")" "$(w "$proj/canary-capbank.kicad_sch")" >/dev/null
"$bin/python.exe" "$(w "$here/gen_pcb.py")" "$(w "$tmp/net.xml")" "$(w "$proj/canary-capbank.kicad_pcb")" "$(w "$proj")" \
    "C:\\Program Files\\KiCad\\10.0\\share\\kicad\\footprints" 2>&1 | grep -v -E "memory leak|image handler|assert"
"$bin/kicad-cli.exe" sch erc --severity-all --exit-code-violations -o "$(w "$tmp/erc.rpt")" "$(w "$proj/canary-capbank.kicad_sch")" >/dev/null \
    && echo "ERC clean" || { grep -E "^\[|;" "$tmp/erc.rpt"; exit 1; }
"$bin/kicad-cli.exe" sch export bom --fields "Reference,QUANTITY,Value,Manufacturer,MPN,Footprint,Description" \
    --labels "Refs,Qty,Value,Manufacturer,MPN,Footprint,Description" --group-by "Value,MPN,Footprint" \
    -o "$(w "$proj/BOM.csv")" "$(w "$proj/canary-capbank.kicad_sch")" >/dev/null && echo "BOM.csv written"
$py "$here/offboard.py" "$proj/BOM-offboard.csv"
"$bin/kicad-cli.exe" pcb drc --schematic-parity --refill-zones --save-board --severity-all --format json \
    -o "$(w "$tmp/drc.json")" "$(w "$proj/canary-capbank.kicad_pcb")" >/dev/null || true
$py - "$tmp/drc.json" <<'PY'
import collections, json, sys
r = json.load(open(sys.argv[1]))
c = collections.Counter(f"{v['severity']}:{v['type']}" for v in r["violations"])
print("DRC:", dict(sorted(c.items())) or "clean", "| unconnected", len(r["unconnected_items"]), "| parity", len(r.get("schematic_parity", [])))
for v in r["violations"] + r["unconnected_items"] + r.get("schematic_parity", []):
    print("  ", v["severity"], v["type"], " || ".join(f"{i['description'][:70]} @({i['pos']['x']:.2f},{i['pos']['y']:.2f})" for i in v["items"]))
PY
