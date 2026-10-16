#!/bin/bash
# Autoroute a KiCad board with Freerouting, keeping hand-laid copper:
#   autoroute.sh <project_dir> <freerouting.jar> [passes] [-- dsn_export.py options]
# Works on <project_dir>/<name>.kicad_pcb in place, so point it at a scratch copy. Steps:
#   1. refill zones (kicad-cli), so dsn_export.py sees the pours' real fills
#   2. dsn_export.py: DSN with pours as fixed copper, routable keepouts dropped, widths set
#   3. Freerouting, headless
#   4. ses_import.py: add the router's tracks and vias
#   5. refill and DRC with schematic parity; prints the violation counts
# KICAD_BIN overrides the KiCad bin directory (default: the Windows install, driven from WSL).
# FREEROUTING_ARGS adds router options, e.g. --router.scoring.start_ripup_costs=1500.
set -euo pipefail
proj=$(cd "$1" && pwd); jar=$2; passes=${3:-30}
shift $(($# < 3 ? $# : 3))
[ "${1:-}" = "--" ] && shift
here=$(cd "$(dirname "$0")" && pwd)
bin=${KICAD_BIN:-"/mnt/c/Program Files/KiCad/10.0/bin"}
win() { wslpath -w "$1"; }
board=$(ls "$proj"/*.kicad_pcb | head -1)
stem=${board%.kicad_pcb}

"$bin/kicad-cli.exe" pcb drc --refill-zones --save-board -o "$(win "$stem.pre-drc.json")" \
    --format json "$(win "$board")" >/dev/null 2>&1 || true
"$bin/python.exe" "$(win "$here/dsn_export.py")" "$(win "$board")" "$(win "$stem.dsn")" "$@" \
    2>&1 | grep -v "memory leak"
(cd "$proj" && java -jar "$jar" -de "$(basename "$stem").dsn" -do "$(basename "$stem").ses" \
    -mp "$passes" --gui.enabled=false ${FREEROUTING_ARGS:-} >"$stem.freerouting.log" 2>&1)
grep -E "auto-routing was completed|unrouted|Routing completed" "$stem.freerouting.log" | tail -3 || true
"$bin/python.exe" "$(win "$here/ses_import.py")" "$(win "$board")" "$(win "$stem.ses")" \
    2>&1 | grep -v "memory leak"
"$bin/kicad-cli.exe" pcb drc --schematic-parity --refill-zones --save-board --severity-all \
    --format json -o "$(win "$stem.drc.json")" "$(win "$board")" >/dev/null 2>&1 || true
python3 - "$stem.drc.json" <<'EOF'
import collections, json, sys
r = json.load(open(sys.argv[1]))
c = collections.Counter(f"{v['severity']}:{v['type']}" for v in r["violations"])
c["error:unconnected"] = len(r.get("unconnected_items", []))
c.update(f"parity:{v['type']}" for v in r.get("schematic_parity", []))
print("DRC:", dict(sorted(c.items())))
EOF
