#!/bin/bash
# Score and draw a board's footprint placement, optionally trying a placement table first.
#   hardware/tools/review_placement.sh <board.kicad_pcb> <config.json> <out.png> [table.json]
# The board is only read. KiCad is the native install when there is one, else KICAD_BIN's
# Windows install (kicad_env.sh).
set -euo pipefail
board=$1 config=$2 png=$3 table=${4:-}
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
# shellcheck source=hardware/tools/kicad_env.sh
source "$here/kicad_env.sh"
report=$(mktemp --suffix=.json)
trap 'rm -f "$report"' EXIT
place=()
if [[ -n $table ]]; then place=(--place "$(kpath "$(realpath "$table")")"); fi
kicad_py "$(kpath "$here/kicad_placement.py")" "$(kpath "$(realpath "$board")")" "$(kpath "$report")" \
    "${place[@]}" 2>&1 | kicad_quiet
"$root/.venv/bin/python" "$here/placement_review.py" "$report" "$config" --png "$png"
