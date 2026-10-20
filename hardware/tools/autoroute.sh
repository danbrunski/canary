#!/bin/bash
# Autoroute a KiCad board with freerouting-rs, keeping hand-laid copper:
#   autoroute.sh <project_dir> [passes] [-- dsn_export.py options]
# Works on <project_dir>/<name>.kicad_pcb in place, so point it at a scratch copy. Steps:
#   1. refill zones (kicad-cli), so dsn_export.py sees the pours' real fills
#   2. dsn_export.py: DSN with pours as fixed copper, routable keepouts dropped, widths set
#   3. freerouting-rs, headless
#   4. ses_import.py: add the router's tracks and vias
#   5. refill and DRC with schematic parity; prints the violation counts
#   6. via_in_pad.py: exits 1 if any via sits in a pad
#   7. inner_tracks.py: exits 1 if any track cuts an inner plane (a layer of type power)
# KiCad is the native install when there is one, else KICAD_BIN's Windows install (kicad_env.sh).
# A refill that fails, or cannot save, stops the run.
# The router is freerouting-rs's `freerouting`, found as its KiCad plugin finds it: FREEROUTING_RS_BIN
# (a directory), then PATH, then ~/.cargo/bin. Freerouting (Java) is retired (operator, 2026-10-20).
# FREEROUTING_ARGS adds router options, e.g. --router.scoring.start_ripup_costs=1500.
set -euo pipefail
if [[ ${2:-} == *.jar ]]; then
    echo "autoroute.sh: Freerouting (Java) is retired (operator, 2026-10-20); it runs freerouting-rs," \
        "so drop the jar: autoroute.sh <project_dir> [passes]" >&2
    exit 2
fi
proj=$(cd "$1" && pwd); passes=${2:-30}
shift $(($# < 2 ? $# : 2))
[ "${1:-}" = "--" ] && shift
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=hardware/tools/kicad_env.sh
source "$here/kicad_env.sh"
router=
for candidate in ${FREEROUTING_RS_BIN:+"$FREEROUTING_RS_BIN/freerouting"} "$(command -v freerouting || true)" \
    "$HOME/.cargo/bin/freerouting"; do
    if [[ -n $candidate && -f $candidate && -x $candidate ]]; then router=$candidate; break; fi
done
if [[ -z $router ]]; then
    echo "autoroute.sh: no freerouting-rs binary: set FREEROUTING_RS_BIN to the directory holding" \
        "\`freerouting\`, or put it on PATH or in ~/.cargo/bin" >&2
    exit 1
fi
echo "router: $router ($("$router" --version 2>&1 | head -1))"
board=$(ls "$proj"/*.kicad_pcb | head -1)
stem=${board%.kicad_pcb}

kicad_drc "$stem.pre-drc.json" "$board"
kicad_py "$(kpath "$here/dsn_export.py")" "$(kpath "$board")" "$(kpath "$stem.dsn")" "$@" 2>&1 | kicad_quiet
read -ra router_args <<<"${FREEROUTING_ARGS:-}"
(cd "$proj" && "$router" -de "$(basename "$stem").dsn" -do "$(basename "$stem").ses" \
    --max-passes "$passes" "${router_args[@]}" >"$stem.freerouting.log" 2>&1)
tail -3 "$stem.freerouting.log"
kicad_py "$(kpath "$here/ses_import.py")" "$(kpath "$board")" "$(kpath "$stem.ses")" 2>&1 | kicad_quiet
kicad_drc "$stem.drc.json" "$board" --schematic-parity --severity-all
python3 - "$stem.drc.json" <<'EOF'
import collections, json, sys
r = json.load(open(sys.argv[1]))
c = collections.Counter(f"{v['severity']}:{v['type']}" for v in r["violations"])
c["error:unconnected"] = len(r.get("unconnected_items", []))
c.update(f"parity:{v['type']}" for v in r.get("schematic_parity", []))
print("DRC:", dict(sorted(c.items())))
EOF
# Vias in pads need plugging at the fab and are not allowed (via_in_pad.py); KiCad's DRC misses them.
if ! kicad_py "$(kpath "$here/via_in_pad.py")" "$(kpath "$board")" 2>&1 | kicad_quiet; then
    echo "autoroute: REFUSED, vias in pads (see above)"
    exit 1
fi
# DRC misses a track across a plane layer, and the routers have put signals there.
if ! kicad_py "$(kpath "$here/inner_tracks.py")" "$(kpath "$board")" 2>&1 | kicad_quiet; then
    echo "autoroute: REFUSED, tracks on plane layers (see above)"
    exit 1
fi
