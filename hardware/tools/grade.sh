#!/bin/bash
# Grade a routing result against a KiCad project's committed rules, on a scratch copy:
#   grade.sh <project_dir> <routes.ses | routed.kicad_pcb> [workdir]
# The project's files come from git HEAD (git archive), so local edits and KiCad's open-project saves
# cannot leak into the grade. GRADE_REV grades against another commit (a result routed from an older board). An SES is added with ses_import.py; a .kicad_pcb replaces the board as is.
# Then DRC with schematic parity and refilled zones, via_in_pad.py and inner_tracks.py. A result counts
# when the only errors are unconnected items and starved thermals, and both checkers report 0.
# KICAD_BIN overrides the KiCad bin directory (default: the Windows install, driven from WSL).
set -uo pipefail
proj=$(cd "$1" && pwd); src=$(realpath "$2")
here=$(cd "$(dirname "$0")" && pwd)
bin=${KICAD_BIN:-"/mnt/c/Program Files/KiCad/10.0/bin"}
win() { wslpath -w "$1"; }
top=$(git -C "$proj" rev-parse --show-toplevel); rel=$(realpath --relative-to="$top" "$proj")
work=${3:-$(mktemp -d)}
rm -rf "$work/$rel"; mkdir -p "$work"
git -C "$top" archive "${GRADE_REV:-HEAD}" "$rel" | tar -x -C "$work"
board=$(ls "$work/$rel"/*.kicad_pcb | head -1)
if [[ $src == *.kicad_pcb ]]; then
    cp "$src" "$board"; echo "board: $(basename "$src") (no ses_import)"
else
    cp "$src" "$work/$rel/in.ses"
    "$bin/python.exe" "$(win "$here/ses_import.py")" "$(win "$board")" "$(win "$work/$rel/in.ses")" \
        2>&1 | grep -v -E "memory leak|image handler"
fi
"$bin/kicad-cli.exe" pcb drc --schematic-parity --refill-zones --save-board --severity-all \
    --format json -o "$(win "$work/$rel/drc.json")" "$(win "$board")" >/dev/null 2>&1 || true
python3 - "$work/$rel/drc.json" <<'EOF'
import collections, json, sys
r = json.load(open(sys.argv[1]))
c = collections.Counter(f"{v['severity']}:{v['type']}" for v in r["violations"])
c["error:unconnected"] = len(r.get("unconnected_items", []))
c.update(f"parity:{v['type']}" for v in r.get("schematic_parity", []))
print("DRC:", dict(sorted(c.items())))
EOF
for check in via_in_pad inner_tracks; do
    "$bin/python.exe" "$(win "$here/$check.py")" "$(win "$board")" 2>&1 | grep -v -E "memory leak|image handler"
done
echo "graded board: $board (project at ${GRADE_REV:-HEAD})"
