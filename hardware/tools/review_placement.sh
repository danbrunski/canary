#!/bin/bash
# Score and draw a board's footprint placement from WSL, optionally trying a placement table first.
#   hardware/tools/review_placement.sh <board.kicad_pcb> <config.json> <out.png> [table.json]
# The board is only read. KICAD_PYTHON overrides the Windows KiCad 10 python used for pcbnew.
set -euo pipefail
board=$1 config=$2 png=$3 table=${4:-}
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
kpy=${KICAD_PYTHON:-"/mnt/c/Program Files/KiCad/10.0/bin/python.exe"}
report=$(mktemp --suffix=.json)
trap 'rm -f "$report"' EXIT
win() { wslpath -w "$(realpath "$1")"; }
place=()
if [[ -n $table ]]; then place=(--place "$(win "$table")"); fi
# KiCad's python is a Windows program, so it writes the report through a Windows path.
"$kpy" "$(win "$here/kicad_placement.py")" "$(win "$board")" "$(wslpath -w "$report")" "${place[@]}"
"$root/.venv/bin/python" "$here/placement_review.py" "$report" "$config" --png "$png"
