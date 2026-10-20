# shellcheck shell=bash disable=SC2034  # kicad_share is for the scripts that source this
# Sourced by the KiCad tool scripts: which KiCad runs, and how to hand it a path.
#   kicad_cli ARGS...  kicad-cli             kpath PATH   PATH as that KiCad opens it
#   kicad_py ARGS...   python with pcbnew    kicad_tmp    a scratch directory that KiCad can save into
#   kicad_drc OUT.json BOARD [ARGS...]  refill, DRC (ARGS add checks) and save BOARD, or stop the script
#   kicad_quiet        filters KiCad's harmless stderr chatter (pipe through it)
#   kicad_share        KiCad's share/kicad directory (footprints/, symbols/), as a path here
# A native KiCad (kicad-cli on PATH and a python3 that imports pcbnew) is used when there is one.
# Otherwise, or when KICAD_BIN is set, it is a Windows install driven from WSL: KICAD_BIN is its bin
# directory (python.exe, kicad-cli.exe), handed Windows paths. On this laptop's WSL, Windows KiCad
# cannot overwrite a board in the WSL filesystem: `pcb drc --save-board` fails with "Cannot copy
# permissions" and pcbnew's SaveBoard hangs (2026-10-20, 755 directories too; a new file saves).
# Another setup's builds did overwrite boards there, so this is detected, not assumed: kicad_drc
# stops on the error, a Windows kicad_py that runs past KICAD_PY_TIMEOUT (600 s) stops with the
# reason, and kicad_tmp is under Windows %TEMP%, where both work.

kicad_native_py=
if [[ -z ${KICAD_BIN:-} ]] && command -v kicad-cli >/dev/null; then
    for py in python3 /usr/bin/python3; do
        if "$py" -c 'import pcbnew' >/dev/null 2>&1; then
            kicad_native_py=$py
            break
        fi
    done
fi

if [[ -n $kicad_native_py ]]; then
    # kicad-cli resolves footprints through the global library tables, which a native install gets
    # only when the GUI first runs. Without them DRC reports a lib_footprint_issues warning per
    # footprint (123 on CANary-PWR) where it otherwise reports the real mismatches.
    kicad_tables=${XDG_CONFIG_HOME:-$HOME/.config}/kicad/$(kicad-cli version | cut -d. -f1,2)
    if [[ ! -e $kicad_tables/fp-lib-table ]]; then
        echo "kicad_env.sh: no global footprint table in $kicad_tables; copy KiCad's defaults:" \
            "cp /usr/share/kicad/template/{fp,sym}-lib-table $kicad_tables/" >&2
    fi
    kicad_share=$(realpath -m "$(dirname "$(command -v kicad-cli)")/../share/kicad")
    kicad_cli() { kicad-cli "$@"; }
    kicad_py() { "$kicad_native_py" "$@"; }
    kpath() { printf '%s\n' "$1"; }
    kicad_tmp() { mktemp -d; }
else
    KICAD_BIN=${KICAD_BIN:-"/mnt/c/Program Files/KiCad/10.0/bin"}
    if [[ ! -e $KICAD_BIN/kicad-cli.exe ]]; then
        echo "no KiCad: install KiCad 10 (kicad-cli, and pcbnew for python3), or set KICAD_BIN" \
            "to a Windows install's bin directory" >&2
        exit 1
    fi
    kicad_share=$(realpath -m "$KICAD_BIN/../share/kicad")
    kicad_cli() { "$KICAD_BIN/kicad-cli.exe" "$@"; }
    kicad_py() {
        timeout "${KICAD_PY_TIMEOUT:-600}" "$KICAD_BIN/python.exe" "$@" && return
        local rc=$?
        if ((rc == 124)); then
            echo "Windows KiCad python ran past ${KICAD_PY_TIMEOUT:-600} s: its SaveBoard hangs overwriting" \
                "a board in the WSL filesystem on some setups; work under /mnt/c, or install KiCad natively" >&2
        fi
        return $rc
    }
    kpath() { wslpath -w "$1"; }
    kicad_tmp() {
        local temp
        temp=$(wslpath "$(cd /mnt/c && cmd.exe /c 'echo %TEMP%' | tr -d '\r')") &&
            mktemp -d -p "$temp" canary-kicad.XXXXXX
    }
fi

kicad_drc() {
    local out=$1 board=$2 log=${1%.json}.log
    shift 2
    if ! kicad_cli pcb drc --refill-zones --save-board --format json "$@" -o "$(kpath "$out")" \
        "$(kpath "$board")" >"$log" 2>&1; then
        echo "kicad-cli pcb drc failed on $board:" >&2
        kicad_quiet <"$log" >&2
        exit 1
    fi
}

# Exits 0 when it filtered every line, so a pipe into it keeps the producer's exit status.
kicad_quiet() { grep -v -E 'memory leak|image handler|property\.h\([0-9]+\): assert' || true; }
